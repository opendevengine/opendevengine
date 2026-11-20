"""Durable implementation sessions over frozen schema 10.

Request methods are bounded SQLite operations; model execution happens only in the
dedicated runtime worker (``run_session``). State, events, tools and checkpoints are
fenced by the live job lease and session/job identity. Publication is separate and is not
performed here; this service only records the publication policy snapshot.
"""
import base64
import hashlib
import json
import re
import sqlite3
import threading
import time

from opendevengine.execution.runner import RunnerError, validate_limits as validate_runner_limits
from opendevengine.github.errors import GitHubError
from opendevengine.indexing.safety import excluded
from opendevengine.platform.clock import now_ms, new_id
from opendevengine.platform.errors import PlatformError
from .errors import SessionError
from .prompt import INSTRUCTIONS_VERSION, alias_for, build_prompt, context_aliases
from .redaction import event_payload, redact_text
from .workspace import canonical, sha256

ACTIVE = ('queued', 'provisioning', 'running', 'validating', 'waiting_for_user', 'stopping')
TERMINAL = ('completed', 'blocked', 'failed', 'cancelled', 'interrupted')
# Active->active edges. Any active state may enter a terminal state; terminal is immutable.
EDGES = {
    'queued': {'provisioning'},
    'provisioning': {'running', 'stopping'},
    'running': {'validating', 'waiting_for_user', 'stopping'},
    'waiting_for_user': {'running', 'stopping'},
    'validating': {'stopping'},
    'stopping': set(),
}
AGENT_LIMITS = {'wall_seconds': (1, 3600, 600), 'max_turns': (1, 40, 12),
                'max_budget_usd': (0, 10, 1.0), 'max_output_bytes': (1024, 1024 * 1024, 524288)}
SERVICE_LIMITS = {'artifact_bytes': 32 * 1024 * 1024, 'max_events': 5000, 'question_wait_seconds': 900,
                  'validation_timeout_seconds': 300, 'max_validations': 20}
REVOKED_REASONS = ('access_revoked', 'repository_purged', 'policy_changed')
# Reasons rooted in the pinned profile version; a retry rechecks them on that version.
PROFILE_REASONS = frozenset({'execution_test_required', 'execution_test_failed', 'execution_test_interrupted',
                             'execution_test_pending', 'execution_image_unpinned', 'execution_profile_unavailable',
                             'invalid_profile_limits', 'execution_adapter_unsupported', 'not_found'})
SOURCE_EVENT_TYPES = ('agent.summary', 'tool.started', 'tool.finished', 'tool.denied', 'checkpoint',
                      'question.asked', 'validation.started', 'validation.finished', 'error')
MATERIAL_RE = re.compile(r'(?i)\b(?:force[- ]?push|push (?:to|the)|git push|merge (?:it|this|the|into)|deploy|'
                         r'open (?:a |the )?(?:draft )?(?:pr|pull request)|publish|network access|enable network|'
                         r'grant|permission|credential|token|another repo(?:sitory)?|other repo(?:sitory)?|'
                         r'also (?:change|edit|modify|update) (?:the )?[\w.-]+/[\w.-]+)\b')
_KEY_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$')
_IMAGE_ID_RE = re.compile(r'^sha256:[0-9a-f]{64}$')
_SHA_RE = re.compile(r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')
DESTINATION = {'kind': 'remote', 'adapter': 'claude_code', 'auth_mode': 'cli_login',
               'disclosure': 'The approved plan and repository source read by tools are sent to the '
                             'Claude service through the installed Claude Code CLI login.'}


def _ident(value, field='id'):
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or any(ord(c) < 33 for c in value):
        raise SessionError('validation', details=[field])
    return value


def _loads(value, default):
    try:
        return json.loads(value) if value else default
    except ValueError:
        return default


def agent_limits(value):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - set(AGENT_LIMITS):
        raise SessionError('invalid_profile_limits')
    out = {}
    for key, (low, high, default) in AGENT_LIMITS.items():
        item = value.get(key, default)
        if isinstance(item, bool) or not isinstance(item, (int, float)) or item != item:
            raise SessionError('invalid_profile_limits')
        if key == 'max_budget_usd':
            if not low < item <= high:
                raise SessionError('invalid_profile_limits')
        elif not float(item).is_integer() or not low <= item <= high:
            raise SessionError('invalid_profile_limits')
        out[key] = item if key == 'max_budget_usd' else int(item)
    return out


class SessionService:
    def __init__(self, platform, index, github, providers, plans, execution_profiles, *,
                 runner_factory=None, adapter_factory=None, clock=now_ms,
                 lease_ms=30000, poll_interval=0.25, concurrency=1, data_dir=None, broker_root=None):
        self.platform, self.store, self.jobs = platform, platform.store, platform.jobs
        self.index, self.github, self.providers = index, github, providers
        self.plans, self.execution_profiles, self.clock = plans, execution_profiles, clock
        if runner_factory is None:
            def runner_factory(instance_id):
                from opendevengine.execution.runner import DockerRunner
                return DockerRunner(instance_id)
        if adapter_factory is None:
            def adapter_factory():
                from opendevengine.execution.claude import ClaudeAdapter
                return ClaudeAdapter()
        self.runner_factory, self.adapter_factory = runner_factory, adapter_factory
        if type(concurrency) is not int or not 1 <= concurrency <= 4:
            raise ValueError('Invalid runtime concurrency.')
        self.lease_ms, self.poll_interval, self.concurrency = lease_ms, poll_interval, concurrency
        self.broker_root = broker_root
        self._active, self._active_lock = {}, threading.Lock()
        self._preflight_cache = {}
        self._shutdown = threading.Event()
        from .runtime import Runtime
        self.runtime = Runtime(self, data_dir=data_dir)
        if github is not None:
            github.register_purge_hook('sessions', self.purge_repository)
            github.register_eligibility_listener(self._eligibility_changed)

    # ------------------------------------------------------------- lifecycle
    def start(self):
        return self.runtime.start()

    def close(self):
        return self.runtime.close()

    def recover(self):
        return self.runtime.recover()

    def runtime_status(self):
        return self.runtime.status()

    def run_session(self, job, context):
        from .attempt import Attempt
        return Attempt(self, job, context).run()

    def instance_id(self):
        with self.store.read() as db:
            row = db.execute('SELECT id FROM runtime_instance WHERE singleton=1').fetchone()
        if row:
            return row[0]
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO runtime_instance VALUES (1,?,?)', (new_id('rt'), self.clock()))
            return db.execute('SELECT id FROM runtime_instance WHERE singleton=1').fetchone()[0]

    # --------------------------------------------------------------- helpers
    def _row(self, db, session_id):
        row = db.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
        if row is None:
            raise SessionError('not_found')
        return dict(row)

    def append_event(self, db, session_id, kind, payload):
        """Assign the next per-session sequence inside the caller's transaction."""
        seq = db.execute('SELECT coalesce(max(seq),0)+1 FROM session_events WHERE session_id=?', (session_id,)).fetchone()[0]
        db.execute('INSERT INTO session_events (session_id,seq,type,payload_json,created_at) VALUES (?,?,?,?,?)',
                   (session_id, seq, kind, event_payload(payload), self.clock()))
        return seq

    def transition(self, db, row, state, *, phase=None, **fields):
        current = row['state']
        if current in TERMINAL:
            raise SessionError('conflict', details=['terminal'])
        if state not in TERMINAL and state not in EDGES[current]:
            raise SessionError('conflict', details=[current + '->' + state])
        now = self.clock()
        values = dict(fields, state=state, updated_at=now, phase=phase or (state if state not in ('running',) else row['phase']))
        if state in TERMINAL:
            values['finished_at'] = now
            # Never dispatched: rejected. Dispatched but no model acknowledgement observed:
            # remains delivered with an explicit unknown-acknowledgement reason.
            db.execute("UPDATE session_messages SET delivery='rejected',reason='terminal_before_delivery',updated_at=? "
                       "WHERE session_id=? AND delivery='queued'", (now, row['id']))
            db.execute("UPDATE session_messages SET reason='ack_unknown',updated_at=? "
                       "WHERE session_id=? AND delivery='delivered'", (now, row['id']))
            db.execute("UPDATE session_questions SET state='cancelled',updated_at=? WHERE session_id=? AND state='open'",
                       (now, row['id']))
        db.execute('UPDATE sessions SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', (*values.values(), row['id']))
        payload = {'state': state, 'previous': current, 'phase': values['phase']}
        for key in ('error_code', 'stop_reason'):
            if fields.get(key):
                payload[key] = fields[key]
        self.append_event(db, row['id'], 'session.state', payload)
        row.update(values)
        return row

    def settings(self):
        with self.store.read() as db:
            row = db.execute('SELECT max_active_sessions FROM execution_settings WHERE singleton=1').fetchone()
        return {'max_active_sessions': row[0] if row else 1}

    def write_access(self, repository_id):
        """Raise SessionError unless the writable repository is currently usable."""
        try:
            state = self.index.get_state(repository_id)
        except Exception:
            raise SessionError('repository_unavailable') from None
        if not state.get('eligible') or state.get('purged') or state.get('purging'):
            raise SessionError('repository_unavailable')
        if state.get('kind') != 'github':
            raise SessionError('local_write_unsupported')
        try:
            repo = self.github.get_repository(repository_id)
            self.github.require_access(repository_id, 'contents_read')
        except GitHubError as exc:
            raise SessionError('repository_unavailable' if exc.code == 'repository_unavailable' else 'capability_missing') from None
        if not repo['implementation_enabled']:
            raise SessionError('implementation_not_permitted')
        return repo

    def write_access_fast(self, repository_id):
        """Per-tool authority check (database only, no index scans or network)."""
        repo = self.github.get_repository(repository_id)
        self.github.require_access(repository_id, 'contents_read')
        if not repo['implementation_enabled']:
            raise SessionError('implementation_not_permitted')
        with self.store.read() as db:
            row = db.execute('SELECT index_enabled,metadata_json FROM index_sources WHERE id=?', (repository_id,)).fetchone()
        meta = _loads(row[1], {}) if row else {'purged': True}
        if row is None or meta.get('purged') or meta.get('purging'):
            raise SessionError('repository_unavailable')

    def read_access_fast(self, repository_id):
        """Read-only related context: current contents_read grant and not purged."""
        self.github.get_repository(repository_id)
        self.github.require_access(repository_id, 'contents_read')
        with self.store.read() as db:
            row = db.execute('SELECT index_enabled,metadata_json FROM index_sources WHERE id=?', (repository_id,)).fetchone()
        meta = _loads(row[1], {}) if row else {'purged': True}
        if row is None or meta.get('purged') or meta.get('purging'):
            raise SessionError('repository_unavailable')

    def read_context(self, repository_id):
        """Preflight check for an optional read-only related repository."""
        try:
            state = self.index.get_state(repository_id)
        except Exception:
            raise SessionError('read_context_unavailable') from None
        if state.get('kind') != 'github':
            raise SessionError('read_context_unsupported_kind')
        if not state.get('eligible') or state.get('purged') or state.get('purging'):
            raise SessionError('read_context_unavailable')
        try:
            self.read_access_fast(repository_id)
        except (SessionError, GitHubError, PlatformError):
            raise SessionError('read_context_unavailable') from None

    def live_exclusions(self, repository_id):
        with self.store.read() as db:
            row = db.execute('SELECT exclusions_json FROM index_sources WHERE id=?', (repository_id,)).fetchone()
        return _loads(row[0], []) if row else None

    @staticmethod
    def exclusions_hash(patterns):
        return sha256(canonical(sorted(patterns)).encode()) if patterns is not None else None

    def repository_policy(self, row):
        """Live policy for one session repository row: None when usable, else a reason."""
        try:
            if row['role'] == 'write':
                self.write_access_fast(row['repository_id'])
            else:
                self.read_access_fast(row['repository_id'])
        except (SessionError, GitHubError, PlatformError):
            return 'access_revoked'
        pinned = _loads(row['source_manifest_json'], {}).get('exclusions_sha256')
        if pinned is None or self.exclusions_hash(self.live_exclusions(row['repository_id'])) != pinned:
            return 'exclusions_changed'
        return None

    def source_policy(self, session_id):
        """Whether any session source-bearing projection may be shown NOW.

        Every repository bound to the session must still be accessible and its
        exclusion policy must equal the policy pinned at launch; otherwise all
        source-bearing projections (diff, tool/agent/question/validation events,
        question prompts, validation commands/output, message content) are hidden.
        """
        with self.store.read() as db:
            row = self._row(db, session_id)
            if _loads(row['outcome_json'], {}).get('purged_at') is not None:
                return {'available': False, 'reason': 'purged'}
            rows = [dict(r) for r in db.execute('SELECT repository_id,role,source_manifest_json FROM session_repositories '
                                                'WHERE session_id=?', (session_id,))]
        if not rows:
            return {'available': False, 'reason': 'repository_unavailable'}
        for row in rows:
            reason = self.repository_policy(row)
            if reason:
                return {'available': False, 'reason': reason}
        return {'available': True, 'reason': None}

    # ------------------------------------------------------------- preflight
    def resolve_version(self, version_id):
        """Pinned immutable profile version with fresh revocable permission."""
        resolver = getattr(self.execution_profiles, 'require_version_usable', None)
        if not callable(resolver):
            raise SessionError('execution_profile_unavailable')
        return resolver(version_id)

    def _resolve_profile(self, profile_id, version_id=None):
        reasons = []
        try:
            version = (self.resolve_version(version_id) if version_id is not None
                       else self.execution_profiles.require_usable(profile_id))
        except Exception as exc:
            code = getattr(exc, 'code', None)
            return None, None, [code if isinstance(code, str) and code else 'execution_profile_unavailable']
        if not isinstance(version, dict):
            return None, None, ['execution_profile_unavailable']
        config = version.get('config') if isinstance(version.get('config'), dict) else {}
        check = version.get('check') if isinstance(version.get('check'), dict) else {}
        if version.get('profile_deleted'):
            reasons.append('execution_profile_deleted')
        if version.get('adapter') != 'claude_code' or version.get('auth_mode') != 'cli_login':
            reasons.append('execution_adapter_unsupported')
        if check.get('status') != 'passed':
            reasons.append('execution_test_required')
        if not isinstance(check.get('image_id'), str) or not _IMAGE_ID_RE.match(check['image_id']):
            reasons.append('execution_image_unpinned')
        if config.get('allow_external_source') is not True:
            reasons.append('execution_external_source_denied')
        limits = None
        try:
            limits = {'runner': validate_runner_limits(config.get('runner_limits') or {}),
                      'agent': agent_limits(config.get('agent_limits')), 'service': dict(SERVICE_LIMITS)}
        except RunnerError:
            reasons.append('invalid_profile_limits')
        except SessionError as exc:
            reasons.append(exc.code)
        capabilities = check.get('capabilities')
        snapshot = {'profile_id': version.get('profile_id'), 'profile_version_id': version.get('id'),
                    'version': version.get('version'), 'adapter': version.get('adapter'),
                    'auth_mode': version.get('auth_mode'), 'image': config.get('image'),
                    'image_id': check.get('image_id'), 'cli_version': check.get('cli_version'),
                    'capabilities': capabilities if isinstance(capabilities, (dict, list)) else {},
                    'allow_external_source': config.get('allow_external_source') is True,
                    'agent_model': config.get('agent_model'), 'checked_at': check.get('checked_at')}
        return snapshot, limits, reasons

    def _preflight(self, content):
        reasons, warnings, private = [], [], {}
        execution = None
        if not isinstance(content, dict):
            return {'blocking_reasons': ['plan_unavailable'], 'warnings': [], 'execution': None}, private
        repos = content.get('repositories') or []
        writes = [r for r in repos if r.get('role') == 'write']
        if len(writes) != 1:
            reasons.append('exactly_one_write_target_required')
            if len(writes) > 1:
                reasons.append('multi_repository_write_unsupported')
        for read in [r for r in repos if r.get('role') == 'read']:
            try:
                self.read_context(read['repository_id'])
            except SessionError as exc:
                reasons.append(exc.code)
            if not isinstance(read.get('revision'), str) or not _SHA_RE.match(read['revision']):
                reasons.append('read_context_revision_not_git')
        if not content.get('citations'):
            reasons.append('citations_required')
        if any(q.get('blocking') and not q.get('answer') for q in content.get('open_questions') or []):
            reasons.append('blocking_questions')
        if len(content.get('validation') or []) > SERVICE_LIMITS['max_validations']:
            reasons.append('too_many_validations')
        elif not content.get('validation'):
            warnings.append('no_validation_declared')
        repo = None
        if len(writes) == 1:
            write = writes[0]
            try:
                repo = self.write_access(write['repository_id'])
            except SessionError as exc:
                reasons.append(exc.code)
            if not isinstance(write.get('revision'), str) or not _SHA_RE.match(write['revision']):
                reasons.append('base_revision_not_git')
            private['write'] = write
            private['repo'] = repo
        ctx = content.get('execution_context') or {}
        profile_id = ctx.get('execution_profile_id')
        snapshot = limits = None
        if not profile_id:
            reasons.append('execution_not_configured')
        else:
            snapshot, limits, profile_reasons = self._resolve_profile(profile_id)
            reasons.extend(profile_reasons)
        try:
            if self.providers.settings().get('local_only', True):
                reasons.append('execution_local_only')
        except Exception:
            reasons.append('execution_local_only')
        runtime = self.runtime.status()
        if runtime['status'] == 'runtime_in_use':
            reasons.append('runtime_in_use')
        if repo is not None and len(writes) == 1:
            _, error = build_prompt(content, plan_version_id='pending', content_hash='0' * 64,
                                    repository_name=repo['full_name'], base_sha=writes[0].get('revision', ''),
                                    contexts=context_aliases(content))
            if error:
                reasons.append(error)
        if snapshot is not None:
            execution = {'profile_id': snapshot['profile_id'], 'profile_version_id': snapshot['profile_version_id'],
                         'image_id': snapshot['image_id'], 'cli_version': snapshot['cli_version'],
                         'limits': limits, 'capabilities': snapshot['capabilities'],
                         'destination': dict(DESTINATION)}
        private.update(snapshot=snapshot, limits=limits)
        return {'blocking_reasons': sorted(set(reasons)), 'warnings': sorted(set(warnings)), 'execution': execution}, private

    def preflight(self, plan_content):
        """Read-only, bounded and cached briefly. Never probes Docker or models."""
        key = sha256(canonical(plan_content).encode()) if isinstance(plan_content, dict) else None
        now = time.monotonic()
        cached = self._preflight_cache.get(key) if key else None
        if cached and cached[0] > now:
            return json.loads(cached[1])
        result, _ = self._preflight(plan_content)
        if key:
            if len(self._preflight_cache) > 64:
                self._preflight_cache.clear()
            self._preflight_cache[key] = (now + 5.0, json.dumps(result))
        return result

    # ----------------------------------------------------------------- launch
    def _by_key(self, db, key):
        row = db.execute('SELECT id,state,launch_hash FROM sessions WHERE idempotency_key=?', (key,)).fetchone()
        return dict(row) if row else None

    def launch(self, plan_version_id, content_hash, idempotency_key, acknowledged_base=None):
        _ident(plan_version_id, 'plan_version_id')
        _ident(content_hash, 'content_hash')
        if not isinstance(idempotency_key, str) or not _KEY_RE.match(idempotency_key):
            raise SessionError('validation', details=['idempotency_key'])
        acknowledged_base = {} if acknowledged_base is None else acknowledged_base
        if not isinstance(acknowledged_base, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not _SHA_RE.fullmatch(v) for k, v in acknowledged_base.items()):
            raise SessionError('validation', details=['acknowledged_base'])
        launch_hash = sha256(canonical({'kind': 'launch', 'plan_version_id': plan_version_id,
                                        'content_hash': content_hash}).encode())
        with self.store.read() as db:
            saved = db.execute('SELECT * FROM launch_base_observations WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            existing = db.execute('SELECT instructions_json FROM sessions WHERE idempotency_key=?', (idempotency_key,)).fetchone()
        if saved:
            if saved['launch_hash'] != launch_hash:
                raise SessionError('idempotency_conflict')
            observation = json.loads(saved['observation_json'])
            allowed = {observation['repository_id']: observation['pinned_revision']}
            if acknowledged_base and acknowledged_base != allowed:
                raise SessionError('validation', details=['acknowledged_base'])
            if observation['status'] == 'advanced' and not acknowledged_base:
                raise SessionError('base_advanced', details=[observation])
        if existing and _loads(existing['instructions_json'], {}).get('acknowledged_base', {}) != acknowledged_base:
            raise SessionError('idempotency_conflict')
        return self._create(plan_version_id, content_hash, idempotency_key, launch_hash, None, acknowledged_base)

    def retry(self, session_id, idempotency_key, acknowledged_base=None):
        self._require_retained(session_id)
        _ident(session_id, 'session_id')
        if not isinstance(idempotency_key, str) or not _KEY_RE.match(idempotency_key):
            raise SessionError('validation', details=['idempotency_key'])
        with self.store.read() as db:
            pred = self._row(db, session_id)
            existing = db.execute('SELECT * FROM sessions WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            successor = db.execute('SELECT idempotency_key FROM sessions WHERE predecessor_id=?', (session_id,)).fetchone()
        launch_hash = sha256(canonical({'kind': 'retry', 'predecessor_id': session_id,
                                        'plan_version_id': pred['plan_version_id'],
                                        'content_hash': pred['content_hash']}).encode())
        if acknowledged_base is None:
            acknowledged_base = _loads(pred['instructions_json'], {}).get('acknowledged_base', {})
        if not isinstance(acknowledged_base, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not _SHA_RE.fullmatch(v) for k, v in acknowledged_base.items()):
            raise SessionError('validation', details=['acknowledged_base'])
        with self.store.read() as db:
            saved = db.execute('SELECT * FROM launch_base_observations WHERE idempotency_key=?', (idempotency_key,)).fetchone()
        if saved:
            if saved['launch_hash'] != launch_hash:
                raise SessionError('idempotency_conflict')
            observation = json.loads(saved['observation_json'])
            if acknowledged_base and acknowledged_base != {observation['repository_id']: observation['pinned_revision']}:
                raise SessionError('validation', details=['acknowledged_base'])
            if observation['status'] == 'advanced' and not acknowledged_base:
                raise SessionError('base_advanced', details=[observation])
        if existing:
            if existing['launch_hash'] != launch_hash or _loads(existing['instructions_json'], {}).get('acknowledged_base', {}) != acknowledged_base:
                raise SessionError('idempotency_conflict')
            return {'session_id': existing['id'], 'state': existing['state']}
        if successor:
            raise SessionError('retry_exists')
        if pred['state'] not in TERMINAL:
            raise SessionError('session_active')
        return self._create(pred['plan_version_id'], pred['content_hash'], idempotency_key, launch_hash, pred, acknowledged_base)

    def _launch_observation(self, key, launch_hash, write, repo):
        # Persist the first decision before refusing creation. Replays never contact
        # GitHub again, including after application restart.
        with self.store.read() as db:
            saved = db.execute('SELECT * FROM launch_base_observations WHERE idempotency_key=?', (key,)).fetchone()
        if saved:
            if saved['launch_hash'] != launch_hash:
                raise SessionError('idempotency_conflict')
            return json.loads(saved['observation_json'])
        result = {}
        done = threading.Event()
        def observe():
            try:
                token = self.github.mint_installation_token(write['repository_id'])['token']
                head = self.github.client.default_branch_sha(token, repo['owner'], repo['name'], write['branch'])
                if isinstance(head, str) and _SHA_RE.fullmatch(head):
                    result['head'] = head
            except Exception:
                pass  # Optional freshness observation; pinned acquisition still applies.
            finally:
                done.set()
        threading.Thread(target=observe, name='launch-head', daemon=True).start()
        done.wait(2.0)
        head = result.get('head') if done.is_set() else None
        observation = {'repository_id': write['repository_id'], 'pinned_revision': write['revision'],
                       'current_head': head, 'status': 'unavailable' if head is None else
                       'current' if head == write['revision'] else 'advanced'}
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO launch_base_observations VALUES (?,?,?)',
                       (key, launch_hash, canonical(observation)))
            saved = db.execute('SELECT * FROM launch_base_observations WHERE idempotency_key=?', (key,)).fetchone()
        if saved['launch_hash'] != launch_hash:
            raise SessionError('idempotency_conflict')
        return json.loads(saved['observation_json'])

    def _create(self, plan_version_id, content_hash, key, launch_hash, predecessor, acknowledged_base=None):
        with self.store.read() as db:
            existing = self._by_key(db, key)
            pv = db.execute('SELECT plan_id FROM plan_versions WHERE id=?', (plan_version_id,)).fetchone()
        if existing:
            if existing['launch_hash'] != launch_hash:
                raise SessionError('idempotency_conflict')
            return {'session_id': existing['id'], 'state': existing['state']}
        if pv is None:
            raise SessionError('not_found')
        try:
            plan = self.plans.get(pv['plan_id'], plan_version_id)
        except PlatformError:
            raise SessionError('not_found') from None
        version = plan['version']
        if version['content_hash'] != content_hash:
            raise SessionError('plan_hash_mismatch')
        readiness = version['readiness']
        if version['content'] is None or (predecessor is None and readiness['state'] == 'superseded'):
            raise SessionError('plan_not_ready', details=readiness['blocking_reasons'] or [readiness['state']])
        content = version['content']
        result, private = self._preflight(content)
        blocking = result['blocking_reasons']
        if predecessor is not None:
            # A retry repeats the predecessor's pinned intent: its profile VERSION is
            # rechecked for fresh permission instead of adopting the active version.
            blocking = [r for r in blocking if r not in PROFILE_REASONS]
            # Supersession is expected for an immutable retry.  All current
            # access, implementation, exclusion and pinned-profile checks are
            # still performed by _preflight and _resolve_profile.
            ready_reasons = [r for r in readiness['blocking_reasons']
                             if r not in PROFILE_REASONS and r != 'superseded']
            if ready_reasons:
                raise SessionError('plan_not_ready', details=ready_reasons)
        elif readiness['state'] != 'ready':
            raise SessionError('plan_not_ready', details=readiness['blocking_reasons'] or [readiness['state']])
        if blocking:
            raise SessionError('plan_not_ready', details=blocking)
        write, repo = private['write'], private['repo']
        acknowledged_base = {} if acknowledged_base is None else acknowledged_base
        if any(k != write['repository_id'] or v != write['revision'] for k, v in acknowledged_base.items()):
            raise SessionError('validation', details=['acknowledged_base'])
        observation = self._launch_observation(key, launch_hash, write, repo)
        if observation['status'] == 'advanced' and acknowledged_base.get(write['repository_id']) != write['revision']:
            raise SessionError('base_advanced', details=[observation])
        prompt, _ = build_prompt(content, plan_version_id=plan_version_id, content_hash=content_hash,
                                 repository_name=repo['full_name'], base_sha=write['revision'],
                                 contexts=context_aliases(content))
        if predecessor is not None:
            snapshot = _loads(predecessor['profile_snapshot_json'], {})
            limits = _loads(predecessor['limits_json'], {})
            instructions = _loads(predecessor['instructions_json'], {})
            publication = _loads(predecessor['publication_json'], {})
            _, _, pinned_reasons = self._resolve_profile(None, snapshot.get('profile_version_id'))
            if pinned_reasons:
                raise SessionError('plan_not_ready', details=pinned_reasons)
            current = self.resolve_version(snapshot['profile_version_id'])
            if (current.get('check') or {}).get('image_id') != snapshot.get('image_id'):
                raise SessionError('execution_profile_changed')
            if instructions.get('prompt_sha256') != sha256(prompt.encode()):
                raise SessionError('plan_changed')
            publication = dict(publication, state='not_started')
        else:
            if readiness['state'] != 'ready':
                raise SessionError('plan_not_ready', details=readiness['blocking_reasons'])
            snapshot, limits = private['snapshot'], private['limits']
            instructions = {'version': INSTRUCTIONS_VERSION, 'prompt_sha256': sha256(prompt.encode()),
                            'skills': [], 'plugins': [], 'memory': [], 'warnings': result['warnings']}
            policy = content['publication']['policy']
            publication = {'policy': policy, 'requires_explicit_action': policy == 'manual_draft',
                           'requires_full_validation': policy == 'on_success_draft', 'state': 'not_started',
                           'authorized_by_launch': policy == 'on_success_draft'}
        instructions = dict(instructions, base_observation=observation, acknowledged_base=acknowledged_base)
        try:
            return self._insert_session(plan_version_id, content_hash, key, launch_hash, predecessor, pv, content,
                                        snapshot, limits, instructions, publication)
        except sqlite3.IntegrityError:
            # A referenced snapshot/profile version vanished or a concurrent launch won.
            with self.store.read() as db:
                again = self._by_key(db, key)
            if again and again['launch_hash'] == launch_hash:
                return {'session_id': again['id'], 'state': again['state']}
            raise SessionError('idempotency_conflict' if again else 'conflict') from None

    def _insert_session(self, plan_version_id, content_hash, key, launch_hash, predecessor, pv, content,
                        snapshot, limits, instructions, publication):
        sid = new_id('ses')
        now = self.clock()
        with self.store.transaction() as db:
            again = self._by_key(db, key)
            if again:
                if again['launch_hash'] != launch_hash:
                    raise SessionError('idempotency_conflict')
                return {'session_id': again['id'], 'state': again['state']}
            current = db.execute('SELECT p.latest_version_id,p.deleted_at,v.content_hash,v.deleted_at AS vdel '
                                 'FROM plan_versions v JOIN plans p ON p.id=v.plan_id WHERE v.id=?', (plan_version_id,)).fetchone()
            if (current is None or (predecessor is None and current['latest_version_id'] != plan_version_id)
                    or current['deleted_at'] is not None
                    or current['vdel'] is not None or current['content_hash'] != content_hash):
                raise SessionError('plan_not_ready', details=['superseded'])
            if predecessor is not None:
                if db.execute('SELECT 1 FROM sessions WHERE predecessor_id=?', (predecessor['id'],)).fetchone():
                    raise SessionError('retry_exists')
            unresolved = db.execute("SELECT count(*) FROM session_runners r JOIN sessions s ON s.id=r.session_id "
                                    "WHERE r.state='containment_failed' OR (r.state IN ('provisioning','running','terminating') "
                                    "AND s.state IN ('completed','blocked','failed','cancelled','interrupted'))").fetchone()[0]
            if unresolved:
                raise SessionError('containment_unresolved')
            active = db.execute('SELECT count(*) FROM sessions WHERE state IN (%s)' % ','.join('?' * len(ACTIVE)), ACTIVE).fetchone()[0]
            row = db.execute('SELECT max_active_sessions FROM execution_settings WHERE singleton=1').fetchone()
            if active >= (row[0] if row else 1):
                raise SessionError('session_active')
            job = self.jobs.enqueue_in_transaction(db, 'session.run', {'session_id': sid}, effect='external',
                                                   max_attempts=1, idempotency_key='session.run:' + sid)
            db.execute('INSERT INTO sessions (id,plan_id,plan_version_id,content_hash,profile_version_id,idempotency_key,'
                       'launch_hash,predecessor_id,attempt,job_id,state,phase,profile_snapshot_json,limits_json,'
                       'instructions_json,usage_json,publication_json,outcome_json,created_at,updated_at) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (sid, pv['plan_id'], plan_version_id, content_hash, snapshot['profile_version_id'], key,
                        launch_hash, predecessor['id'] if predecessor else None,
                        predecessor['attempt'] + 1 if predecessor else 1, job.id, 'queued', 'queued',
                        canonical(snapshot), canonical(limits), canonical(instructions), '{}',
                        canonical(publication), '{}', now, now))
            for item in content['repositories']:
                row = db.execute('SELECT exclusions_json FROM index_sources WHERE id=?', (item['repository_id'],)).fetchone()
                patterns = _loads(row[0], []) if row else []
                pinned = {'exclusions': sorted(patterns), 'exclusions_sha256': self.exclusions_hash(patterns),
                          'alias': alias_for(item['repository_id']) if item['role'] == 'read' else None}
                db.execute('INSERT INTO session_repositories (session_id,repository_id,role,snapshot_id,base_sha,'
                           'base_branch,session_branch,source_manifest_json) VALUES (?,?,?,?,?,?,?,?)',
                           (sid, item['repository_id'], item['role'], item['snapshot_id'], item['revision'],
                            item['branch'], 'opendevengine/' + sid if item['role'] == 'write' else None, canonical(pinned)))
            self.append_event(db, sid, 'session.state', {'state': 'queued', 'previous': None, 'attempt':
                                                         predecessor['attempt'] + 1 if predecessor else 1,
                                                         'predecessor_id': predecessor['id'] if predecessor else None})
        return {'session_id': sid, 'state': 'queued'}

    # ------------------------------------------------------------------ reads
    def _live_source(self, session_id):
        """(available, write repository id) under the full live source policy."""
        policy = self.source_policy(session_id)
        with self.store.read() as db:
            row = db.execute("SELECT repository_id FROM session_repositories WHERE session_id=? AND role='write'",
                             (session_id,)).fetchone()
        return policy['available'], row[0] if row else None

    def _public(self, db, row, *, live, reason=None):
        repos = [dict(r) for r in db.execute('SELECT * FROM session_repositories WHERE session_id=? ORDER BY role DESC,repository_id', (row['id'],))]
        runner = db.execute('SELECT state FROM session_runners WHERE session_id=?', (row['id'],)).fetchone()
        question = db.execute("SELECT id,prompt,created_at FROM session_questions WHERE session_id=? AND state='open'", (row['id'],)).fetchone()
        latest = db.execute('SELECT coalesce(max(seq),0) FROM session_events WHERE session_id=?', (row['id'],)).fetchone()[0]
        successor = db.execute('SELECT id FROM sessions WHERE predecessor_id=?', (row['id'],)).fetchone()
        tombstone = _loads(row['outcome_json'], {})
        if tombstone.get('purged_at') is not None:
            return {**{k: row[k] for k in ('id', 'plan_id', 'plan_version_id', 'state', 'created_at', 'updated_at', 'started_at', 'finished_at')},
                    'purged': True, 'purged_at': tombstone['purged_at'], 'source_available': False,
                    'repositories': [{k: r[k] for k in ('repository_id', 'role', 'base_sha')} for r in repos],
                    'publication': tombstone.get('publication', {})}
        snapshot = _loads(row['profile_snapshot_json'], {})
        caps = snapshot.get('capabilities') if isinstance(snapshot.get('capabilities'), dict) else {}
        public_repos = []
        for r in repos:
            source = _loads(r['source_manifest_json'], {})
            public_repos.append({'repository_id': r['repository_id'], 'role': r['role'], 'snapshot_id': r['snapshot_id'],
                                 'base_sha': r['base_sha'], 'base_branch': r['base_branch'],
                                 'session_branch': r['session_branch'], 'current_diff_hash': r['current_diff_hash'],
                                 'provisioned': bool(source.get('counts')), 'context_alias': source.get('alias'),
                                 'mount': ('/workspace' if r['role'] == 'write' else
                                           '/context/' + source['alias'] if source.get('alias') else None),
                                 'acquisition': source.get('counts'), 'head_sha_at_start': source.get('head_sha')})
        runner_state = runner[0] if runner else None
        return {
            'id': row['id'], 'plan_id': row['plan_id'], 'plan_version_id': row['plan_version_id'],
            'content_hash': row['content_hash'], 'attempt': row['attempt'], 'predecessor_id': row['predecessor_id'],
            'successor_id': successor[0] if successor else None, 'state': row['state'], 'phase': row['phase'],
            'stop_requested': bool(row['stop_requested']), 'stop_reason': row['stop_reason'], 'error_code': row['error_code'],
            'repositories': public_repos, 'limits': _loads(row['limits_json'], {}),
            'execution': {k: snapshot.get(k) for k in ('profile_id', 'profile_version_id', 'version', 'adapter', 'auth_mode',
                                                       'image', 'image_id', 'cli_version', 'allow_external_source', 'agent_model',
                                                       'checked_at')}
                         | {'destination': dict(DESTINATION)},
            'instructions': _loads(row['instructions_json'], {}), 'usage': _loads(row['usage_json'], {}),
            'publication': _loads(row['publication_json'], {}), 'outcome': _loads(row['outcome_json'], {}),
            'latest_seq': latest,
            # UI must hide already-rendered source when this flips to false.
            'source_available': bool(live), 'source_unavailable_reason': None if live else (reason or 'source_unavailable'),
            'pending_question': ({'id': question['id'], 'prompt': question['prompt'] if live else None,
                                  'redacted': not live or question['prompt'] is None, 'created_at': question['created_at']}
                                 if question else None),
            'capabilities': {'ask_user': bool(caps.get('ask_user')), 'follow_up': True,
                             'follow_up_channel': 'broker_tool_result_annotation',
                             'follow_up_acknowledgement': 'model_marker_observed', 'resume': False,
                             'adapter_checkpoint': False, 'artifact_checkpoints': True},
            'runner': {'state': runner_state, 'containment': self._containment(runner_state)},
            'created_at': row['created_at'], 'updated_at': row['updated_at'],
            'started_at': row['started_at'], 'finished_at': row['finished_at']}

    @staticmethod
    def _containment(runner_state):
        return {None: 'not_started', 'provisioning': 'pending', 'running': 'pending', 'terminating': 'pending',
                'terminated': 'confirmed', 'containment_failed': 'failed'}.get(runner_state, 'unknown')

    def get(self, session_id):
        _ident(session_id, 'session_id')
        with self.store.read() as db:
            self._row(db, session_id)
        policy = self.source_policy(session_id)
        with self.store.read() as db:
            return self._public(db, self._row(db, session_id), live=policy['available'], reason=policy['reason'])

    def list_sessions(self, limit=50, cursor=None):
        from opendevengine.ask.common import page
        try:
            with self.store.read() as db:
                rows, nxt = page(db, 'sessions', limit, cursor)
        except ValueError:
            raise SessionError('validation') from None
        items = []
        for row in rows:
            policy = self.source_policy(row['id'])
            with self.store.read() as db:
                items.append(self._public(db, self._row(db, row['id']), live=policy['available'], reason=policy['reason']))
        return {'items': items, 'next_cursor': nxt}

    def events(self, session_id, after=0, limit=200):
        self._require_retained(session_id)
        _ident(session_id, 'session_id')
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise SessionError('validation')
        live, _ = self._live_source(session_id)
        with self.store.read() as db:
            self._row(db, session_id)
            rows = db.execute('SELECT seq,type,payload_json,created_at FROM session_events WHERE session_id=? AND seq>? '
                              'ORDER BY seq LIMIT ?', (session_id, after, limit + 1)).fetchall()
        more = len(rows) > limit
        items = []
        for r in rows[:limit]:
            payload = _loads(r['payload_json'], {})
            if not live and r['type'] in SOURCE_EVENT_TYPES:
                payload = {'redacted': True}
            items.append({'session_id': session_id, 'seq': r['seq'], 'created_at': r['created_at'],
                          'type': r['type'], 'payload': payload})
        return {'items': items, 'next_seq': items[-1]['seq'] if items else after, 'has_more': more,
                'source_available': bool(live)}

    def artifacts(self, session_id):
        self._require_retained(session_id)
        _ident(session_id, 'session_id')
        policy = self.source_policy(session_id)
        with self.store.read() as db:
            self._row(db, session_id)
            rows = db.execute('SELECT id,kind,sha256,byte_count,content IS NULL AS redacted,created_at FROM session_artifacts '
                              'WHERE session_id=? ORDER BY created_at,id', (session_id,)).fetchall()
        live = policy['available']
        return {'items': [{'id': r['id'], 'kind': r['kind'],
                           'sha256': None if r['redacted'] or not live else r['sha256'],
                           'byte_count': None if not live else r['byte_count'],
                           'redacted': bool(r['redacted']) or not live, 'created_at': r['created_at']}
                          for r in rows], 'source_available': bool(live),
                'source_unavailable_reason': None if live else policy['reason']}

    def _latest_artifact(self, db, session_id, kind):
        return db.execute('SELECT * FROM session_artifacts WHERE session_id=? AND kind=? ORDER BY created_at DESC,rowid DESC LIMIT 1',
                          (session_id, kind)).fetchone()

    def diff(self, session_id):
        self._require_retained(session_id)
        _ident(session_id, 'session_id')
        live, repository_id = self._live_source(session_id)
        with self.store.read() as db:
            row = self._row(db, session_id)
            artifact = self._latest_artifact(db, session_id, 'patch')
            repo = db.execute("SELECT base_sha,current_diff_hash FROM session_repositories WHERE session_id=? AND role='write'",
                              (session_id,)).fetchone()
            source = db.execute('SELECT exclusions_json FROM index_sources WHERE id=?', (repository_id,)).fetchone() if repository_id else None
        outcome = _loads(row['outcome_json'], {})
        base = {'session_id': session_id, 'repository_id': repository_id, 'base_sha': repo['base_sha'] if repo else None,
                'state': row['state'], 'edits_since_last_checkpoint_may_be_lost': bool(outcome.get('edits_since_last_checkpoint_may_be_lost'))}
        if artifact is None:
            return dict(base, available=False, reason='no_checkpoint', files=[], rejected=[])
        if not live:
            raise SessionError('source_unavailable')
        if artifact['content'] is None:
            return dict(base, available=False, reason='redacted', files=[], rejected=[])
        patch = json.loads(bytes(artifact['content']).decode('utf-8'))
        exclusions = _loads(source[0], []) if source else []
        files, hidden = [], 0
        for item in patch['files']:
            if excluded(item['path'], exclusions):
                hidden += 1  # exclusion changed after capture: never show the content now
                continue
            files.append(dict(item, diff=redact_text(item['diff'], None)))
        return dict(base, available=True, artifact_id=artifact['id'], diff_hash=artifact['sha256'],
                    files=files, rejected=patch['rejected'], hidden_by_current_exclusions=hidden,
                    stats=patch['stats'], captured_at=artifact['created_at'])

    def publication_snapshot(self, session_id):
        """Trusted internal export of the latest verified capture, never a UI diff.

        No runner calls, source acquisition or file reconstruction happen here.
        Terminal state and artifact identity make the captured bytes immutable;
        live source authority is checked again immediately before returning them.
        """
        from .publication import verify_capture

        self._require_retained(session_id)
        _ident(session_id, 'session_id')
        with self.store.read() as db:
            row = dict(self._row(db, session_id))
            repos = db.execute("SELECT * FROM session_repositories WHERE session_id=? AND role='write'",
                               (session_id,)).fetchall()
            runner = db.execute('SELECT state FROM session_runners WHERE session_id=?', (session_id,)).fetchone()
            checkpoint = self._latest_artifact(db, session_id, 'checkpoint')
            patch = self._latest_artifact(db, session_id, 'patch')
        if not self.source_policy(session_id)['available']:
            raise SessionError('source_unavailable')
        if row['state'] not in TERMINAL:
            raise SessionError('session_not_terminal')
        outcome = _loads(row['outcome_json'], {})
        if (runner is None or runner['state'] != 'terminated'
                or outcome.get('containment', {}).get('confirmed') is not True):
            raise SessionError('containment_unverified')
        if (outcome.get('source_redacted') or outcome.get('artifacts_stale')
                or outcome.get('edits_since_last_checkpoint_may_be_lost')):
            raise SessionError('stale_artifacts')
        if len(repos) != 1:
            raise SessionError('session_snapshot_invalid')
        repo = dict(repos[0])
        cp, pt = verify_capture(repo, checkpoint, patch, outcome, _loads(row['limits_json'], {}))
        policy = _loads(row['publication_json'], {}).get('policy')
        if policy not in ('manual_draft', 'on_success_draft'):
            raise SessionError('session_snapshot_invalid')
        validations = self.validations(session_id)
        if not self.source_policy(session_id)['available'] or not validations['source_available']:
            raise SessionError('source_unavailable')
        # These are service-owned, redacted projections, never private runner state.
        return {'session_id': session_id, 'plan_id': row['plan_id'], 'plan_version_id': row['plan_version_id'],
                'repository_id': repo['repository_id'], 'base_sha': repo['base_sha'],
                'base_branch': repo['base_branch'], 'session_branch': repo['session_branch'],
                'diff_hash': patch['sha256'], 'checkpoint_artifact_id': checkpoint['id'],
                'patch_artifact_id': patch['id'], 'checkpoint': cp, 'patch': pt,
                'omitted': sorted(r['path'] for r in cp['rejected']),
                'policy': policy, 'outcome': outcome, 'validations': validations, 'source_available': True}

    def validations(self, session_id):
        self._require_retained(session_id)
        _ident(session_id, 'session_id')
        live, _ = self._live_source(session_id)
        with self.store.read() as db:
            self._row(db, session_id)
            rows = [dict(r) for r in db.execute('SELECT v.*,a.content AS log,a.byte_count AS log_bytes FROM validation_runs v '
                                                'LEFT JOIN session_artifacts a ON a.id=v.artifact_id WHERE v.session_id=? '
                                                'ORDER BY v.ordinal', (session_id,))]
        items = []
        for r in rows:
            log = None
            if live and r['log'] is not None:
                log = redact_text(bytes(r['log']).decode('utf-8', 'replace'), None)
            items.append({'id': r['id'], 'ordinal': r['ordinal'],
                          'command': _loads(r['command_json'], {}) if live else {'redacted': True},
                          'source': r['source'], 'status': r['status'], 'required': bool(r['required']),
                          'exit_code': r['exit_code'], 'artifact_id': r['artifact_id'],
                          'environment': _loads(r['environment_json'], {}), 'output': log,
                          'output_redacted': r['artifact_id'] is not None and log is None,
                          'started_at': r['started_at'], 'finished_at': r['finished_at']})
        summary = {k: sum(1 for i in items if i['status'] == k) for k in ('passed', 'failed', 'timeout', 'error', 'not_run', 'unavailable')}
        return {'items': items, 'summary': summary, 'source_available': bool(live),
                'fully_validated': bool(items) and summary['passed'] == len(items)}

    # --------------------------------------------------------------- messages
    def send_message(self, session_id, content, client_request_id, question_id=None):
        _ident(session_id, 'session_id')
        _ident(client_request_id, 'client_request_id')
        if not isinstance(content, str) or not content.strip() or len(content) > 8000 or '\x00' in content:
            raise SessionError('validation', details=['content'])
        if question_id is not None:
            _ident(question_id, 'question_id')
        now = self.clock()
        with self.store.transaction() as db:
            row = self._row(db, session_id)
            if _loads(row['outcome_json'], {}).get('purged_at') is not None:
                raise SessionError('purged')
            prior = db.execute('SELECT * FROM session_messages WHERE session_id=? AND client_request_id=?',
                               (session_id, client_request_id)).fetchone()
            if prior:
                if (prior['content'], prior['question_id']) != (content, question_id):
                    raise SessionError('idempotency_conflict')
                return self._message(prior)
            delivery, reason = 'queued', None
            question = None
            if question_id is not None:
                question = db.execute('SELECT state FROM session_questions WHERE id=? AND session_id=?',
                                      (question_id, session_id)).fetchone()
                if question is None:
                    raise SessionError('not_found', details=['question_id'])
            if row['state'] in TERMINAL:
                delivery, reason = 'rejected', 'session_ended'
            elif row['stop_requested'] or row['state'] == 'stopping':
                delivery, reason = 'rejected', 'stop_requested'
            elif MATERIAL_RE.search(content):
                delivery, reason = 'rejected', 'plan_amendment_required'
            elif question is not None and question['state'] != 'open':
                delivery, reason = 'rejected', 'question_not_open'
            # Generic updates stay queued until the session broker dispatches them in a
            # successful tool result; acknowledgement requires an observed model marker.
            mid = new_id('smsg')
            db.execute('INSERT INTO session_messages (id,session_id,client_request_id,content,question_id,delivery,reason,'
                       'created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                       (mid, session_id, client_request_id, content, question_id, delivery, reason, now, now))
            self.append_event(db, session_id, 'message.delivery', {'message_id': mid, 'delivery': delivery,
                                                                  'reason': reason, 'question_id': question_id})
            return self._message(db.execute('SELECT * FROM session_messages WHERE id=?', (mid,)).fetchone())

    def list_messages(self, session_id, limit=50, cursor=None):
        """Delivery history in (created_at,id) order; content hidden when source is unavailable."""
        from opendevengine.ask.common import page
        self._require_retained(session_id)
        _ident(session_id, 'session_id')
        with self.store.read() as db:
            self._row(db, session_id)
        live, _ = self._live_source(session_id)
        try:
            with self.store.read() as db:
                rows, nxt = page(db, 'session_messages', limit, cursor, 'session_id=?', (session_id,))
        except ValueError:
            raise SessionError('validation') from None
        items = []
        for row in rows:
            item = self._message(row)
            item['content'] = row['content'] if live and row['content'] is not None else None
            item['content_redacted'] = item['content'] is None
            items.append(item)
        return {'items': items, 'next_cursor': nxt, 'source_available': bool(live)}

    @staticmethod
    def _message(row):
        return {'id': row['id'], 'session_id': row['session_id'], 'client_request_id': row['client_request_id'],
                'question_id': row['question_id'], 'delivery': row['delivery'], 'reason': row['reason'],
                'created_at': row['created_at'], 'updated_at': row['updated_at']}

    # ------------------------------------------------------------------- stop
    def stop(self, session_id, *, reason='user_requested'):
        _ident(session_id, 'session_id')
        cancel_job = None
        with self.store.transaction() as db:
            row = self._row(db, session_id)
            if row['state'] not in TERMINAL:
                # A revocation/purge durably supersedes a user stop so every later
                # source-bearing write is fenced by the stronger reason.
                stronger = reason in REVOKED_REASONS and row['stop_reason'] != 'repository_purged' and row['stop_reason'] != reason
                if not row['stop_requested'] or stronger:
                    db.execute('UPDATE sessions SET stop_requested=1,stop_reason=?,updated_at=? WHERE id=?',
                               (reason, self.clock(), session_id))
                    row.update(stop_requested=1, stop_reason=reason)
                    self.append_event(db, session_id, 'stop.requested', {'reason': reason})
                job = db.execute('SELECT state FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
                if row['state'] == 'queued' and job is not None and job['state'] == 'queued':
                    final = 'cancelled' if reason == 'user_requested' else 'blocked'
                    self.transition(db, row, final, stop_reason=reason, error_code=None if final == 'cancelled' else reason)
                    cancel_job = row['job_id']
                elif row['state'] != 'stopping':
                    self.transition(db, row, 'stopping', phase='stopping')
            view = self._stop_view(db, row)
        if cancel_job:
            try:
                self.jobs.cancel(cancel_job)
            except PlatformError:
                pass  # the worker re-checks the durable stop flag before any work
        with self._active_lock:
            attempt = self._active.get(session_id)
        if attempt is not None:
            attempt.terminate('stop_requested' if reason == 'user_requested' else 'access_revoked', kill=True,
                              override=reason in REVOKED_REASONS)
        return view

    def _stop_view(self, db, row):
        runner = db.execute('SELECT state FROM session_runners WHERE session_id=?', (row['id'],)).fetchone()
        return {'session_id': row['id'], 'state': row['state'], 'stop_requested': bool(row['stop_requested']),
                'stop_reason': row['stop_reason'], 'containment': self._containment(runner[0] if runner else None)}

    # ------------------------------------------------------- access and purge
    def _sessions_for(self, repository_id, *, active_only):
        with self.store.read() as db:
            sql = 'SELECT s.id FROM sessions s JOIN session_repositories r ON r.session_id=s.id WHERE r.repository_id=?'
            params = [repository_id]
            if active_only:
                sql += ' AND s.state IN (%s)' % ','.join('?' * len(ACTIVE))
                params += ACTIVE
            return [r[0] for r in db.execute(sql, params)]

    def _eligibility_changed(self, repository_id, eligible):
        if eligible:
            return
        for sid in self._sessions_for(repository_id, active_only=True):
            try:
                self.stop(sid, reason='access_revoked')
            except PlatformError:
                pass

    def _require_retained(self, session_id):
        _ident(session_id, 'session_id')
        with self.store.read() as db:
            if _loads(self._row(db, session_id)['outcome_json'], {}).get('purged_at') is not None:
                raise SessionError('purged')

    def purge_session(self, session_id):
        """Durable content tombstone; remote publication identity remains reconcilable."""
        _ident(session_id, 'session_id')
        with self.store.transaction() as db:
            row = self._row(db, session_id)
            if row['state'] not in TERMINAL:
                raise SessionError('session_active')
            pub = db.execute('SELECT * FROM publications WHERE session_id=?', (session_id,)).fetchone()
            if pub and pub['state'] not in ('published', 'blocked', 'failed', 'no_changes'):
                raise SessionError('publication_in_progress')
            old = _loads(row['outcome_json'], {})
            stamp = old.get('purged_at', self.clock())
            identity = ({k: pub[k] for k in ('id', 'state', 'pull_request_number', 'pull_request_url')} if pub else {})
            tombstone = canonical({'purged_at': stamp, 'publication': identity})
            db.execute("UPDATE sessions SET profile_snapshot_json='{}',limits_json='{}',instructions_json='{}',"
                       "usage_json='{}',publication_json='{}',outcome_json=?,error_code=NULL,stop_reason=NULL WHERE id=?",
                       (tombstone, session_id))
            db.execute("UPDATE session_artifacts SET content=NULL,byte_count=0,sha256='redacted' WHERE session_id=?", (session_id,))
            db.execute('UPDATE session_events SET payload_json=? WHERE session_id=?', ('{"redacted":true}', session_id))
            db.execute('UPDATE session_messages SET content=NULL WHERE session_id=?', (session_id,))
            db.execute('UPDATE session_questions SET prompt=NULL WHERE session_id=?', (session_id,))
            db.execute("UPDATE session_repositories SET base_manifest_json='{}',source_manifest_json='{}',current_diff_hash=NULL,"
                       "base_branch=NULL,session_branch=NULL WHERE session_id=?", (session_id,))
            db.execute("UPDATE validation_runs SET command_json='{}',environment_json='{}' WHERE session_id=?", (session_id,))
            db.execute("UPDATE session_runners SET private_json='{}' WHERE session_id=?", (session_id,))
            if pub:
                db.execute("UPDATE publications SET title='[deleted]',body='',commit_message='[deleted]',commit_name='[deleted]',"
                           "commit_email='deleted@example.invalid' WHERE session_id=?", (session_id,))
                db.execute("UPDATE publication_events SET payload_json='{}' WHERE publication_id=?", (pub['id'],))
        # Retry cleanup even for an existing tombstone after interruption or I/O failure.
        from .purge import remove_session_files
        path = self.runtime.leadership.path
        if path is not None:
            remove_session_files(path.parent, session_id)
        return {'session_id': session_id, 'purged_at': stamp}

    def purge_repository(self, repository_id):
        """Redact session-derived source for a purged repository; keep minimal history."""
        for sid in self._sessions_for(repository_id, active_only=True):
            try:
                self.stop(sid, reason='repository_purged')
            except PlatformError:
                pass
        sids = self._sessions_for(repository_id, active_only=False)
        if not sids:
            return {'repository_id': repository_id, 'sessions': 0}
        marks = ','.join('?' * len(sids))
        types = ','.join('?' * len(SOURCE_EVENT_TYPES))
        now = self.clock()
        with self.store.transaction() as db:
            db.execute("UPDATE session_artifacts SET content=NULL,byte_count=0,sha256='redacted' WHERE session_id IN (%s)" % marks, sids)
            db.execute('UPDATE session_events SET payload_json=? WHERE session_id IN (%s) AND type IN (%s)' % (marks, types),
                       ('{"redacted":true}', *sids, *SOURCE_EVENT_TYPES))
            db.execute('UPDATE session_messages SET content=NULL,updated_at=? WHERE session_id IN (%s)' % marks, (now, *sids))
            db.execute('UPDATE session_questions SET prompt=NULL,updated_at=? WHERE session_id IN (%s)' % marks, (now, *sids))
            db.execute("UPDATE session_repositories SET base_manifest_json='{}',source_manifest_json='{}',current_diff_hash=NULL "
                       'WHERE session_id IN (%s)' % marks, sids)
            db.execute("UPDATE validation_runs SET command_json='{\"redacted\":true}' WHERE session_id IN (%s)" % marks, sids)
            for sid in sids:
                outcome = _loads(db.execute('SELECT outcome_json FROM sessions WHERE id=?', (sid,)).fetchone()[0], {})
                outcome['source_redacted'] = True
                db.execute('UPDATE sessions SET outcome_json=? WHERE id=?', (canonical(outcome), sid))
        return {'repository_id': repository_id, 'sessions': len(sids)}

    # ------------------------------------------------------------ attempt reg
    def _register(self, attempt):
        with self._active_lock:
            self._active[attempt.sid] = attempt

    def _unregister(self, attempt):
        with self._active_lock:
            if self._active.get(attempt.sid) is attempt:
                del self._active[attempt.sid]

    def active_ids(self):
        with self._active_lock:
            return set(self._active)
