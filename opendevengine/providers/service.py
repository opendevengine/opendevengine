"""Provider profiles, model roles, policy enforcement and inference calls (opendevengine.m1/1).

Boundaries enforced here:
- Profiles are named; every adapter/endpoint/model/config/credential change creates an
  immutable ``provider_profile_versions`` row. Role assignments reference profiles and
  resolve the active version per call; results pin ``profile_version_id``.
- Effective local-only is the persisted workspace setting OR the per-call flag. Remote
  role assignment, connection tests and inference are rejected before any request.
- Remote source transmission also requires the profile's ``allow_external_source`` opt-in.
- Credentials are stored only through SecretService, resolved per call and never
  returned, persisted in config, logged or chained into raised errors.
- No SQL lock or transaction is held during network calls. No provider fallback.
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
from functools import wraps
import re
import sqlite3

from opendevengine.platform.clock import new_id, now_ms
from opendevengine.platform.errors import PlatformError, StorageBusy

from .adapters import ADAPTER_IMPLEMENTATIONS
from .claude_cli import ENDPOINT as CLAUDE_CLI_ENDPOINT, ClaudeCliTransport
from .errors import (ContextOverflow, CredentialUnavailable, DestinationNotAllowed, InvalidOutput,
                     ModelNotFound, PersistenceRequired, ProfileDeleted, ProfileNameConflict,
                     ProfileNotFound, ProviderAuth, ProviderCancelled, ProviderError,
                     ProviderRateLimited, ProviderRejected, ProviderTimeout, ProviderUnavailable,
                     ProviderValidationError, RoleUnassigned, SourceSharingNotAllowed,
                     UnsupportedCapability, UpstreamRedirect)
from .transport import (ResponseTooLarge, TransportCancelled, TransportRequest, TransportTimeout,
                        UrllibTransport)
from .validation import (CAPABILITIES, ROLES, canonical_json, destination_for, fail,
                         validate_flag, validate_messages, validate_purpose, validate_role,
                         validate_schema, validate_secret_value, validate_spec, validate_subject,
                         validate_texts)

MAX_ACTIVE_PROFILES = 100
CHAT_RESPONSE_LIMIT = 4 * 1024 * 1024
EMBED_RESPONSE_CAP = 64 * 1024 * 1024
CHARS_PER_TOKEN_ESTIMATE = 4
DEFAULT_RETRY_MS = 60_000
MAX_RETRY_MS = 3_600_000
CONTEXT_MARKERS = ('context_length', 'context length', 'maximum context', 'context window',
                   'too many tokens', 'exceeds the context',
                   # Groq: one request above the tier's tokens-per-minute cap (413).
                   'request too large', 'reduce your message size')

SYNTHETIC_CHAT = [{'role': 'user', 'content': 'Connection test. Reply with the single word: ok'}]
SYNTHETIC_SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}},
                    'required': ['ok'], 'additionalProperties': False}
SYNTHETIC_STRUCTURED = [{'role': 'user',
                         'content': 'Connection test. Return the JSON object {"ok": true}.'}]
SYNTHETIC_EMBED = ['OpenDevEngine synthetic connection test sentence.']
STOP_TEST_CODES = {'provider_auth', 'credential_unavailable', 'upstream_redirect', 'cancelled'}

_REQUIRED_ROLE_CAPABILITY = {'ask': 'chat', 'planning': 'chat', 'summary': 'chat',
                             'embedding': 'embeddings'}


def _reject_constant(_):
    raise ValueError('non-finite')


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('non-finite')
    return number


def _storage_boundary(method):
    @wraps(method)
    def call(*args, **kwargs):
        failed = False
        try:
            return method(*args, **kwargs)
        except (sqlite3.Error, StorageBusy):
            failed = True
        if failed:
            raise ProviderUnavailable()
    return call


def _loads(data):
    """Parse JSON; returns (ok, value) without retaining decoder exceptions/documents."""
    try:
        if isinstance(data, bytes):
            data = data.decode('utf-8')
        return True, json.loads(data, parse_constant=_reject_constant, parse_float=_finite_float)
    except (UnicodeDecodeError, ValueError, RecursionError):
        pass
    return False, None


def _body_mentions_context(body):
    ok, payload = _loads(body)
    if not ok:
        return False
    error = payload.get('error') if isinstance(payload, dict) else None
    texts = []
    if isinstance(error, str):
        texts.append(error)
    elif isinstance(error, dict):
        texts.extend(str(error.get(k, '')) for k in ('code', 'message', 'type'))
    lowered = ' '.join(texts).lower()
    return any(marker in lowered for marker in CONTEXT_MARKERS)


class ProviderService:
    def __init__(self, store, secrets, *, transport=None, cli_transport=None, clock=now_ms):
        self.store, self.secrets, self.clock = store, secrets, clock
        self.transport = transport if transport is not None else UrllibTransport()
        self.cli_transport = cli_transport if cli_transport is not None else ClaudeCliTransport()

    # ----- row helpers -------------------------------------------------------------------

    def _profile_row(self, conn, profile_id):
        if not isinstance(profile_id, str):
            raise ProfileNotFound()
        row = conn.execute('SELECT * FROM provider_profiles WHERE id=?', (profile_id,)).fetchone()
        if row is None:
            raise ProfileNotFound()
        return row

    def _version_row(self, conn, version_id):
        if not isinstance(version_id, str):
            raise ProfileNotFound()
        row = conn.execute('SELECT * FROM provider_profile_versions WHERE id=?', (version_id,)).fetchone()
        if row is None:
            raise ProfileNotFound()
        return row

    def _credential_state(self, conn, ref_id):
        if ref_id is None:
            return 'none'
        row = conn.execute('SELECT revoked_at FROM secret_refs WHERE id=?', (ref_id,)).fetchone()
        return 'configured' if row is not None and row['revoked_at'] is None else 'revoked'

    def _check(self, conn, version_id):
        row = conn.execute('SELECT * FROM provider_version_checks WHERE version_id=?', (version_id,)).fetchone()
        if row is None:
            return None
        return {'status': row['status'], 'verified_capabilities': json.loads(row['verified_capabilities']),
                'error_code': row['error_code'], 'checked_at': row['checked_at']}

    def _version_public(self, conn, row):
        profile = conn.execute('SELECT deleted_at FROM provider_profiles WHERE id=?', (row['profile_id'],)).fetchone()
        return {'id': row['id'], 'profile_id': row['profile_id'], 'version': row['version'],
                'adapter': row['adapter'], 'endpoint': row['endpoint'], 'model': row['model'],
                'config': json.loads(row['config']), 'created_at': row['created_at'],
                'credential_configured': self._credential_state(conn, row['secret_ref']) == 'configured',
                'destination': destination_for(row['endpoint']),
                'profile_deleted': profile is not None and profile['deleted_at'] is not None,
                'check': self._check(conn, row['id'])}

    def _profile_public(self, conn, row):
        version = self._version_row(conn, row['active_version_id'])
        return {'id': row['id'], 'name': row['name'], 'active_version_id': row['active_version_id'],
                'version': self._version_public(conn, version), 'created_at': row['created_at'],
                'updated_at': row['updated_at'], 'deleted_at': row['deleted_at']}

    def _persisted_local_only(self, conn):
        row = conn.execute("SELECT value FROM provider_settings WHERE key='local_only'").fetchone()
        if row is None:
            return True
        ok, value = _loads(row['value'])
        # Anything other than an explicit false fails closed.
        return not (ok and value is False)

    # ----- credentials --------------------------------------------------------------------

    def _require_credential_storage(self):
        if not getattr(self.store, 'persistent', False):
            raise PersistenceRequired()
        try:
            available = self.secrets.status().available
        except PlatformError:
            available = False
        if not available:
            raise PersistenceRequired()

    def _create_secret(self, profile_id, secret_value):
        self._require_credential_storage()
        failed = False
        try:
            ref = self.secrets.create('provider_api_key', f'provider:{profile_id}', secret_value)
        except (PlatformError, ValueError):
            failed = True
        if failed:
            raise PersistenceRequired('Unable to store the provider credential securely.')
        return ref.id

    @staticmethod
    def _mark_owned_refs_revoked(conn, profile_id, now, *, ref_id=None):
        # Server-authorized metadata update: revoke only references owned by
        # this profile, in the same transaction as rotation/deletion. Vault cleanup
        # after commit is best effort; failure cannot restore credential usability.
        conn.execute(
            'UPDATE secret_refs SET revoked_at=? WHERE revoked_at IS NULL '
            'AND id IN (SELECT secret_ref FROM provider_profile_versions WHERE profile_id=?) '
            'AND (? IS NULL OR id=?)', (now, profile_id, ref_id, ref_id))

    def _revoke_quietly(self, ref_id):
        try:
            self.secrets.revoke(ref_id)
        except PlatformError:
            pass

    def _resolve_secret(self, ref_id):
        if ref_id is None:
            return None
        failed = False
        try:
            return self.secrets.resolve(ref_id).reveal()
        except PlatformError:
            failed = True
        if failed:
            raise CredentialUnavailable()

    # ----- profiles -----------------------------------------------------------------------

    @staticmethod
    def _validate_adapter_endpoint(spec, secret_value=None):
        if spec['adapter'] == 'openai_compatible' and not spec['endpoint'].endswith('/v1'):
            raise fail('endpoint')
        if spec['adapter'] == 'claude_cli':
            # The CLI uses the user's own login; its destination is Anthropic, shown as remote.
            if spec['endpoint'] != CLAUDE_CLI_ENDPOINT:
                raise fail('endpoint')
            if not re.fullmatch(r'(sonnet|opus|haiku|claude-[a-z0-9][a-z0-9.-]{0,80})', spec['model']):
                raise fail('model')
            if 'embeddings' in spec['config']['capabilities']:
                raise fail('config.capabilities')
            if secret_value is not None:
                raise fail('secret_value')

    @_storage_boundary
    def create_profile(self, spec, *, secret_value=None):
        spec = validate_spec(spec)
        secret_value = validate_secret_value(secret_value)
        self._validate_adapter_endpoint(spec, secret_value)
        profile_id, version_id = new_id('prof'), new_id('pver')
        ref_id = self._create_secret(profile_id, secret_value) if secret_value else None
        conflict = False
        try:
            with self.store.transaction() as conn:
                active = conn.execute('SELECT count(*) FROM provider_profiles WHERE deleted_at IS NULL').fetchone()[0]
                if active >= MAX_ACTIVE_PROFILES:
                    raise ProviderValidationError(detail={'field': 'profiles', 'limit': MAX_ACTIVE_PROFILES})
                now = self.clock()
                conn.execute('INSERT INTO provider_profiles VALUES (?,?,?,NULL,?,?)',
                             (profile_id, spec['name'], version_id, now, now))
                conn.execute('INSERT INTO provider_profile_versions VALUES (?,?,1,?,?,?,?,?,?)',
                             (version_id, profile_id, spec['adapter'], spec['endpoint'], spec['model'],
                              ref_id, canonical_json(spec['config']), now))
                result = self._profile_public(conn, self._profile_row(conn, profile_id))
        except sqlite3.IntegrityError:
            conflict = True
        except BaseException:
            if ref_id:
                self._revoke_quietly(ref_id)
            raise
        if conflict:
            if ref_id:
                self._revoke_quietly(ref_id)
            raise ProfileNameConflict()
        return result

    @_storage_boundary
    def update_profile(self, profile_id, spec, *, secret_value=None):
        changes = validate_spec(spec, partial=True)
        secret_value = validate_secret_value(secret_value)
        with self.store.read() as conn:
            if self._profile_row(conn, profile_id)['deleted_at'] is not None:
                raise ProfileDeleted()
        new_ref = self._create_secret(profile_id, secret_value) if secret_value else None
        old_ref = None
        conflict = False
        try:
            with self.store.transaction() as conn:
                profile = self._profile_row(conn, profile_id)
                if profile['deleted_at'] is not None:
                    raise ProfileDeleted()
                current = self._version_row(conn, profile['active_version_id'])
                merged = {'adapter': changes.get('adapter', current['adapter']),
                          'endpoint': changes.get('endpoint', current['endpoint']),
                          'model': changes.get('model', current['model']),
                          'config': changes.get('config', json.loads(current['config']))}
                self._validate_adapter_endpoint(merged, secret_value)
                now = self.clock()
                version_changed = new_ref is not None or any(
                    merged[key] != (json.loads(current['config']) if key == 'config' else current[key])
                    for key in merged)
                active_version_id = profile['active_version_id']
                if version_changed:
                    active_version_id = new_id('pver')
                    number = conn.execute('SELECT max(version) FROM provider_profile_versions WHERE profile_id=?',
                                          (profile_id,)).fetchone()[0] + 1
                    ref = new_ref if new_ref is not None else current['secret_ref']
                    if new_ref is not None:
                        old_ref = current['secret_ref']
                        if old_ref is not None:
                            self._mark_owned_refs_revoked(conn, profile_id, now, ref_id=old_ref)
                    conn.execute('INSERT INTO provider_profile_versions VALUES (?,?,?,?,?,?,?,?,?)',
                                 (active_version_id, profile_id, number, merged['adapter'], merged['endpoint'],
                                  merged['model'], ref, canonical_json(merged['config']), now))
                if version_changed or ('name' in changes and changes['name'] != profile['name']):
                    conn.execute('UPDATE provider_profiles SET name=?, active_version_id=?, updated_at=? WHERE id=?',
                                 (changes.get('name', profile['name']), active_version_id, now, profile_id))
                result = self._profile_public(conn, self._profile_row(conn, profile_id))
        except sqlite3.IntegrityError:
            conflict = True
        except BaseException:
            if new_ref:
                self._revoke_quietly(new_ref)
            raise
        if conflict:
            if new_ref:
                self._revoke_quietly(new_ref)
            raise ProfileNameConflict()
        if old_ref:
            # Rotation: previous versions become historical and can no longer be called.
            self._revoke_quietly(old_ref)
        return result

    @_storage_boundary
    def delete_profile(self, profile_id):
        with self.store.transaction() as conn:
            profile = self._profile_row(conn, profile_id)
            now = self.clock()
            if profile['deleted_at'] is None:
                conn.execute('UPDATE provider_profiles SET deleted_at=?, updated_at=? WHERE id=?',
                             (now, now, profile_id))
            conn.execute('DELETE FROM model_role_assignments WHERE profile_id=?', (profile_id,))
            refs = [r[0] for r in conn.execute(
                'SELECT DISTINCT secret_ref FROM provider_profile_versions WHERE profile_id=? AND secret_ref IS NOT NULL',
                (profile_id,))]
            self._mark_owned_refs_revoked(conn, profile_id, now)
        for ref_id in refs:
            self._revoke_quietly(ref_id)
        with self.store.read() as conn:
            return self._profile_public(conn, self._profile_row(conn, profile_id))

    @_storage_boundary
    def list_profiles(self):
        with self.store.read() as conn:
            rows = conn.execute('SELECT * FROM provider_profiles WHERE deleted_at IS NULL ORDER BY name, id LIMIT ?',
                                (MAX_ACTIVE_PROFILES,)).fetchall()
            return [self._profile_public(conn, row) for row in rows]

    @_storage_boundary
    def get_version(self, version_id):
        with self.store.read() as conn:
            return self._version_public(conn, self._version_row(conn, version_id))

    # ----- roles and settings -------------------------------------------------------------

    @_storage_boundary
    def assign_role(self, role, profile_id):
        role = validate_role(role)
        return self.assign_roles({role: profile_id})[role]

    @_storage_boundary
    def assign_roles(self, assignments):
        """Apply several role assignments in one transaction: all of them or none."""
        if not isinstance(assignments, dict) or not 1 <= len(assignments) <= len(ROLES):
            raise fail('assignments')
        assignments = {validate_role(role): profile_id for role, profile_id in assignments.items()}
        with self.store.transaction() as conn:
            for role, profile_id in assignments.items():
                self._assign(conn, role, profile_id)
            return self._roles(conn)

    def _assign(self, conn, role, profile_id):
        if profile_id is None:
            conn.execute('DELETE FROM model_role_assignments WHERE role=?', (role,))
        else:
            profile = self._profile_row(conn, profile_id)
            if profile['deleted_at'] is not None:
                raise ProfileDeleted()
            version = self._version_row(conn, profile['active_version_id'])
            if _REQUIRED_ROLE_CAPABILITY[role] not in json.loads(version['config'])['capabilities']:
                raise UnsupportedCapability(detail={'capability': _REQUIRED_ROLE_CAPABILITY[role]})
            if self._persisted_local_only(conn) and destination_for(version['endpoint'])['kind'] != 'local':
                raise DestinationNotAllowed()
            conn.execute('INSERT INTO model_role_assignments VALUES (?,?,?) '
                         'ON CONFLICT(role) DO UPDATE SET profile_id=excluded.profile_id, updated_at=excluded.updated_at',
                         (role, profile_id, self.clock()))

    def _roles(self, conn):
        local_only = self._persisted_local_only(conn)
        assigned = {r['role']: r['profile_id'] for r in conn.execute('SELECT role, profile_id FROM model_role_assignments')}
        result = {}
        for role in ROLES:
            if role not in assigned:
                result[role] = {'status': 'unassigned', 'profile_id': None, 'profile_name': None,
                                'version_id': None, 'model': None, 'destination': None}
                continue
            profile = self._profile_row(conn, assigned[role])
            version = self._version_row(conn, profile['active_version_id'])
            destination = destination_for(version['endpoint'])
            check = self._check(conn, version['id'])
            if _REQUIRED_ROLE_CAPABILITY[role] not in json.loads(version['config'])['capabilities']:
                status = 'unsupported'
            elif local_only and destination['kind'] != 'local':
                status = 'blocked'
            elif self._credential_state(conn, version['secret_ref']) == 'revoked':
                status = 'credential_unavailable'
            elif check is None:
                status = 'untested'
            elif _REQUIRED_ROLE_CAPABILITY[role] in check['verified_capabilities']:
                status = 'ready'
            else:
                status = 'failed'
            result[role] = {'status': status, 'profile_id': profile['id'], 'profile_name': profile['name'],
                            'version_id': version['id'], 'model': version['model'], 'destination': destination}
        return result

    @_storage_boundary
    def roles(self):
        with self.store.read() as conn:
            return self._roles(conn)

    @_storage_boundary
    def settings(self):
        with self.store.read() as conn:
            return {'local_only': self._persisted_local_only(conn)}

    @_storage_boundary
    def update_settings(self, *, local_only):
        validate_flag(local_only, 'local_only')
        with self.store.transaction() as conn:
            conn.execute("INSERT INTO provider_settings VALUES ('local_only',?,?) "
                         'ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at',
                         (canonical_json(local_only), self.clock()))
            return {'local_only': self._persisted_local_only(conn)}

    @_storage_boundary
    def status_summary(self):
        with self.store.read() as conn:
            roles = self._roles(conn)
            return {'roles': {role: {k: entry[k] for k in ('status', 'profile_id', 'version_id', 'destination')}
                              for role, entry in roles.items()},
                    'local_only': self._persisted_local_only(conn)}

    @_storage_boundary
    def embedding_namespace(self, version_id):
        with self.store.read() as conn:
            row = self._version_row(conn, version_id)
        return self._namespace(row)

    def _namespace(self, row):
        config = json.loads(row['config'])
        if 'embeddings' not in config['capabilities']:
            raise UnsupportedCapability(detail={'capability': 'embeddings'})
        identity = {'adapter': row['adapter'], 'endpoint': row['endpoint'], 'model': row['model'],
                    'dimensions': config['embedding_dimensions']}
        return {'key': hashlib.sha256(canonical_json(identity).encode('utf-8')).hexdigest(), **identity}

    # ----- inference ----------------------------------------------------------------------

    def _call_target(self, conn, version_row):
        profile = self._profile_row(conn, version_row['profile_id'])
        if profile['deleted_at'] is not None:
            raise ProfileDeleted()
        return {'id': version_row['id'], 'adapter': version_row['adapter'], 'endpoint': version_row['endpoint'],
                'model': version_row['model'], 'secret_ref': version_row['secret_ref'],
                'config': json.loads(version_row['config']), 'destination': destination_for(version_row['endpoint']),
                'persisted_local_only': self._persisted_local_only(conn)}

    def _enforce_policy(self, target, *, local_only, contains_source):
        remote = target['destination']['kind'] != 'local'
        # Plain http is only ever valid to a local destination. A saved http profile whose host is no
        # longer trusted (--ollama-url removed or changed) must not send source or secrets in clear text.
        if remote and target['endpoint'].lower().startswith('http:'):
            raise DestinationNotAllowed()
        if remote and (target['persisted_local_only'] or local_only):
            raise DestinationNotAllowed()
        if remote and contains_source and not target['config']['allow_external_source']:
            raise SourceSharingNotAllowed()

    def _retry_at(self, headers):
        value = next((v for k, v in headers.items() if isinstance(k, str) and k.lower() == 'retry-after'), None)
        delay = None
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if text.isascii() and text.isdecimal():
                delay = MAX_RETRY_MS if len(text) > 10 else int(text) * 1000
            else:
                try:
                    when = parsedate_to_datetime(text)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    delay = int(when.timestamp() * 1000) - self.clock()
                except (TypeError, ValueError, IndexError, OverflowError):
                    delay = None
        if delay is None:
            delay = DEFAULT_RETRY_MS
        return self.clock() + max(1000, min(delay, MAX_RETRY_MS))

    def _classify(self, response):
        status = response.status
        if 200 <= status < 300:
            return None
        if 300 <= status < 400:
            return UpstreamRedirect()
        if status in (401, 403):
            return ProviderAuth()
        if status == 404:
            return ModelNotFound()
        if status == 429:
            return ProviderRateLimited(detail={'retry_at': self._retry_at(response.headers)})
        if status in (408, 504):
            return ProviderTimeout()
        if status in (400, 413, 422):
            return ContextOverflow() if _body_mentions_context(response.body) else ProviderRejected()
        if status >= 500:
            return ProviderUnavailable()
        return ProviderRejected()

    def _send(self, target, path, body, secret, *, cancel_event, max_bytes):
        adapter = ADAPTER_IMPLEMENTATIONS[target['adapter']]
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
                   **adapter.auth_headers(secret)}
        request = TransportRequest('POST', target['endpoint'] + path, headers,
                                   canonical_json(body).encode('utf-8'),
                                   float(target['config']['timeout_seconds']), max_bytes)
        outcome = response = None
        transport = self.cli_transport if adapter.name == 'claude_cli' else self.transport
        try:
            response = transport.send(request, cancel_event=cancel_event)
        except TransportTimeout:
            outcome = ProviderTimeout
        except TransportCancelled:
            outcome = ProviderCancelled
        except ResponseTooLarge:
            outcome = InvalidOutput
        except Exception:
            outcome = ProviderUnavailable
        if outcome is not None:
            raise outcome()
        if cancel_event is not None and cancel_event.is_set():
            raise ProviderCancelled()
        if (not hasattr(response, 'status') or type(response.status) is not int
                or not 100 <= response.status <= 599
                or not isinstance(getattr(response, 'body', None), bytes)
                or not isinstance(getattr(response, 'headers', None), dict)):
            raise ProviderUnavailable()
        if len(response.body) > max_bytes:
            raise InvalidOutput()
        error = self._classify(response)
        if error is not None:
            raise error
        ok, payload = _loads(response.body)
        if not ok or not isinstance(payload, dict):
            raise InvalidOutput()
        return payload

    def _cost(self, config, usage, kind):
        input_price = config.get('input_price_per_million')
        output_price = config.get('output_price_per_million')
        tokens_in, tokens_out = usage['input_tokens'], usage['output_tokens']
        if kind == 'embed':
            if input_price is None or tokens_in is None:
                return None
            return tokens_in * input_price / 1_000_000
        if None in (input_price, output_price, tokens_in, tokens_out):
            return None
        return (tokens_in * input_price + tokens_out * output_price) / 1_000_000

    def _record_usage(self, target, purpose, subject, usage, kind, outcome):
        usage = usage or {'input_tokens': None, 'output_tokens': None}
        cost = self._cost(target['config'], usage, kind) if outcome == 'success' else None
        record = {'input_tokens': usage['input_tokens'], 'output_tokens': usage['output_tokens'],
                  'estimated_cost': cost, 'cost_status': 'unknown' if cost is None else 'estimated',
                  'currency': 'USD'}
        try:
            with self.store.transaction() as conn:
                conn.execute('INSERT INTO provider_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                             (new_id('use'), target['id'], purpose, record['input_tokens'],
                              record['output_tokens'], cost, record['cost_status'], subject[0], subject[1],
                              outcome, 'USD', self.clock()))
        except (PlatformError, sqlite3.Error):
            # Usage accounting must not turn a completed provider call into a lost result.
            pass
        return record

    def _execute(self, target, kind, body, secret, *, purpose, subject, cancel_event, max_bytes, parse):
        """Send once (no hidden retries/fallback); record usage for every sent attempt."""
        if cancel_event is not None and cancel_event.is_set():
            raise ProviderCancelled()
        adapter = ADAPTER_IMPLEMENTATIONS[target['adapter']]
        path = adapter.chat_path if kind == 'chat' else adapter.embed_path
        try:
            payload = self._send(target, path, body, secret, cancel_event=cancel_event, max_bytes=max_bytes)
            value, usage = parse(payload)
        except ProviderError as error:
            self._record_usage(target, purpose, subject, None, kind, error.code)
            raise
        return value, self._record_usage(target, purpose, subject, usage, kind, 'success')

    @staticmethod
    def _cancel_event(value):
        if value is not None and not callable(getattr(value, 'is_set', None)):
            raise fail('cancel_event')
        return value

    def _generate_with(self, target, messages, total_chars, schema, secret, *, purpose, subject, cancel_event):
        config = target['config']
        if total_chars > config['context_limit'] * CHARS_PER_TOKEN_ESTIMATE:
            raise ContextOverflow()
        adapter = ADAPTER_IMPLEMENTATIONS[target['adapter']]
        body = adapter.chat_body(target['model'], messages, schema=schema,
                                 max_tokens=config['max_output_tokens'], context_limit=config['context_limit'])

        def parse(payload):
            text, usage = adapter.parse_chat(payload)
            parsed = None
            if schema is not None:
                ok, parsed = _loads(text.strip())
                if not ok:
                    raise InvalidOutput()
            return (text, parsed), usage

        (text, parsed), usage = self._execute(target, 'chat', body, secret, purpose=purpose, subject=subject,
                                              cancel_event=cancel_event, max_bytes=CHAT_RESPONSE_LIMIT, parse=parse)
        return {'text': text, 'json': parsed, 'profile_version_id': target['id'], 'model': target['model'],
                'destination': target['destination'], 'usage': usage}

    @_storage_boundary
    def generate(self, role, messages, *, schema=None, contains_source=True, local_only=False,
                 cancel_event=None, purpose=None, subject=None):
        role = validate_role(role)
        messages, total_chars = validate_messages(messages)
        schema = validate_schema(schema)
        validate_flag(contains_source, 'contains_source')
        validate_flag(local_only, 'local_only')
        cancel_event = self._cancel_event(cancel_event)
        purpose = validate_purpose(purpose if purpose is not None else role)
        subject = validate_subject(subject)
        with self.store.read() as conn:
            assignment = conn.execute('SELECT profile_id FROM model_role_assignments WHERE role=?', (role,)).fetchone()
            if assignment is None:
                raise RoleUnassigned()
            profile = self._profile_row(conn, assignment['profile_id'])
            target = self._call_target(conn, self._version_row(conn, profile['active_version_id']))
        capabilities = target['config']['capabilities']
        if 'chat' not in capabilities:
            raise UnsupportedCapability(detail={'capability': 'chat'})
        if schema is not None and 'structured_output' not in capabilities:
            raise UnsupportedCapability(detail={'capability': 'structured_output'})
        self._enforce_policy(target, local_only=local_only, contains_source=contains_source)
        secret = self._resolve_secret(target['secret_ref'])
        return self._generate_with(target, messages, total_chars, schema, secret, purpose=purpose,
                                   subject=subject, cancel_event=cancel_event)

    def _embed_with(self, target, texts, dimensions, secret, *, purpose, subject, cancel_event):
        config = target['config']
        if max(len(t) for t in texts) > config['context_limit'] * CHARS_PER_TOKEN_ESTIMATE:
            raise ContextOverflow()
        adapter = ADAPTER_IMPLEMENTATIONS[target['adapter']]
        body = adapter.embed_body(target['model'], texts)
        max_bytes = min(EMBED_RESPONSE_CAP, 1024 * 1024 + len(texts) * (dimensions or 16384) * 32)
        return self._execute(target, 'embed', body, secret, purpose=purpose, subject=subject,
                             cancel_event=cancel_event, max_bytes=max_bytes,
                             parse=lambda payload: adapter.parse_embed(payload, len(texts), dimensions))

    @_storage_boundary
    def embed(self, version_id, texts, *, contains_source=True, local_only=False, cancel_event=None,
              purpose='embedding', subject=None):
        texts, _ = validate_texts(texts)
        validate_flag(contains_source, 'contains_source')
        validate_flag(local_only, 'local_only')
        cancel_event = self._cancel_event(cancel_event)
        purpose = validate_purpose(purpose)
        subject = validate_subject(subject)
        with self.store.read() as conn:
            row = self._version_row(conn, version_id)
            target = self._call_target(conn, row)
        if 'embeddings' not in target['config']['capabilities']:
            raise UnsupportedCapability(detail={'capability': 'embeddings'})
        namespace = self._namespace(row)
        self._enforce_policy(target, local_only=local_only, contains_source=contains_source)
        secret = self._resolve_secret(target['secret_ref'])
        vectors, usage = self._embed_with(target, texts, namespace['dimensions'], secret, purpose=purpose,
                                          subject=subject, cancel_event=cancel_event)
        return {'vectors': vectors, 'profile_version_id': target['id'], 'namespace': namespace,
                'model': target['model'], 'destination': target['destination'], 'usage': usage}

    # ----- connection tests ---------------------------------------------------------------

    @_storage_boundary
    def test_profile(self, version_id, *, capabilities=None):
        with self.store.read() as conn:
            target = self._call_target(conn, self._version_row(conn, version_id))
            previous = self._check(conn, version_id)
        if capabilities is None:
            requested = list(target['config']['capabilities'])
        else:
            if (not isinstance(capabilities, list) or not capabilities or any(not isinstance(c, str) or c not in CAPABILITIES for c in capabilities)
                    or len(set(capabilities)) != len(capabilities)):
                raise fail('capabilities')
            requested = sorted(capabilities)
        # Synthetic content only: contains_source=False; persisted local-only still applies.
        self._enforce_policy(target, local_only=False, contains_source=False)
        results, observed = {}, None
        stop_code = None
        secret_failed = None
        try:
            secret = self._resolve_secret(target['secret_ref'])
        except CredentialUnavailable as error:
            secret, secret_failed = None, error.code
        for capability in requested:
            if secret_failed or stop_code:
                results[capability] = {'status': 'skipped' if stop_code else 'failed',
                                       'error_code': stop_code or secret_failed}
                continue
            try:
                observed = self._test_capability(target, capability, secret) or observed
            except ProviderError as error:
                results[capability] = {'status': 'failed', 'error_code': error.code}
                if error.code in STOP_TEST_CODES:
                    stop_code = error.code
            else:
                results[capability] = {'status': 'verified', 'error_code': None}
        if stop_code or secret_failed:
            # A global failure invalidates the entire check, including successes
            # earlier in this run, as well as verification from previous runs.
            for capability, result in results.items():
                if result['status'] == 'verified':
                    results[capability] = {'status': 'failed',
                                           'error_code': stop_code or secret_failed}
        verified_now = {c for c, r in results.items() if r['status'] == 'verified'}
        failed_now = {c for c, r in results.items() if r['status'] != 'verified'}
        if stop_code or secret_failed:
            failed_now.update(CAPABILITIES)
        verified = sorted((set(previous['verified_capabilities']) if previous else set()) - failed_now | verified_now)
        status = 'passed' if len(verified_now) == len(requested) else ('partial' if verified_now else 'failed')
        error_code = next((r['error_code'] for r in results.values() if r['error_code']), None)
        checked_at = self.clock()
        with self.store.transaction() as conn:
            conn.execute('INSERT INTO provider_version_checks VALUES (?,?,?,?,?) ON CONFLICT(version_id) DO UPDATE '
                         'SET status=excluded.status, verified_capabilities=excluded.verified_capabilities, '
                         'error_code=excluded.error_code, checked_at=excluded.checked_at',
                         (version_id, status, canonical_json(verified), error_code, checked_at))
        result = {'version_id': version_id, 'status': status, 'capabilities': results,
                  'verified_capabilities': verified, 'error_code': error_code, 'checked_at': checked_at,
                  'destination': target['destination']}
        if observed is not None:
            result['observed_embedding_dimensions'] = observed
        return result

    def _test_capability(self, target, capability, secret):
        subject = (None, None)
        if capability == 'chat':
            result = self._generate_with(target, SYNTHETIC_CHAT, len(SYNTHETIC_CHAT[0]['content']), None, secret,
                                         purpose='connection_test', subject=subject, cancel_event=None)
            if not result['text'].strip():
                raise InvalidOutput()
            return None
        if capability == 'structured_output':
            result = self._generate_with(target, SYNTHETIC_STRUCTURED, len(SYNTHETIC_STRUCTURED[0]['content']),
                                         SYNTHETIC_SCHEMA, secret, purpose='connection_test', subject=subject,
                                         cancel_event=None)
            parsed = result['json']
            if not isinstance(parsed, dict) or type(parsed.get('ok')) is not bool:
                raise InvalidOutput()
            return None
        dimensions = target['config'].get('embedding_dimensions')
        vectors, _ = self._embed_with(target, SYNTHETIC_EMBED, dimensions, secret, purpose='connection_test',
                                      subject=subject, cancel_event=None)
        return len(vectors[0])
