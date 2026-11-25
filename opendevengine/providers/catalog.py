"""AI providers: the user's Claude, Codex and Local providers, and which one does each job.

A façade over provider profiles, role assignments and one managed execution profile. Keys
live in the vault: the provider row holds the master copy, and every profile it creates gets
its own copy, so removing or rotating never leaves one job holding a stale reference.
"""
import json

from opendevengine.platform.clock import new_id, now_ms
from opendevengine.execution.profiles import AGENT_MODEL
from opendevengine.platform.errors import PlatformError

from .claude_cli import ENDPOINT as CLAUDE_CLI_ENDPOINT
from .discovery import probe_embedding_dimensions
from .errors import ProfileNameConflict
from .validation import set_trusted_endpoints, validate_endpoint

KINDS = ('claude', 'codex', 'local')
JOBS = ('plans', 'coding', 'search', 'summary')
LOCAL_ADAPTERS = ('ollama', 'openai_compatible')
ANTHROPIC_V1 = 'https://api.anthropic.com/v1'
OPENAI_V1 = 'https://api.openai.com/v1'
CODING_IMAGE = 'python:3.12-slim'
NAMES = {'claude': 'Claude', 'codex': 'Codex', 'local': 'Local'}
CHAT = ['chat', 'structured_output']
# Cloud models get room for real plans; local ones keep the `--model` defaults (slow CPUs).
REMOTE_CHAT = {'timeout_seconds': 300, 'context_limit': 32768, 'max_output_tokens': 8192, 'allow_external_source': True}
LOCAL_CHAT = {'timeout_seconds': 600}


class AIProviderError(PlatformError):
    """Content-free failure with a stable code; the HTTP layer maps it to a status."""

    def __init__(self, code):
        self.code = code
        self.http_status = 404 if code == 'not_found' else 400
        super().__init__('AI provider operation failed.')


def _model(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or value != value.strip() or any(
            ord(c) < 32 or ord(c) == 127 for c in value):
        raise AIProviderError('validation')
    return value


class AIProviders:
    def __init__(self, store, secrets, providers, execution_profiles=None, *, clock=now_ms):
        self.store, self.secrets, self.providers = store, secrets, providers
        self.execution_profiles, self.clock = execution_profiles, clock
        self.refresh_trust()

    # ----- trust -------------------------------------------------------------------------

    def refresh_trust(self):
        """Every saved Local provider's exact address counts as the user's own machine."""
        with self.store.read() as db:
            set_trusted_endpoints([row[0] for row in db.execute("SELECT endpoint FROM ai_providers WHERE kind='local'")])

    # ----- providers ---------------------------------------------------------------------

    def _row(self, db, provider_id):
        row = db.execute('SELECT * FROM ai_providers WHERE id=?', (provider_id,)).fetchone() if isinstance(
            provider_id, str) else None
        if row is None:
            raise AIProviderError('not_found')
        return row

    @staticmethod
    def _public(row):
        return {'id': row['id'], 'kind': row['kind'], 'name': row['name'], 'endpoint': row['endpoint'],
                'adapter': row['adapter'], 'auth': row['auth'], 'has_key': row['secret_ref'] is not None}

    def list(self):
        with self.store.read() as db:
            return [self._public(r) for r in db.execute('SELECT * FROM ai_providers ORDER BY created_at, id')]

    def _endpoint(self, kind, endpoint, adapter):
        if kind == 'claude':
            if endpoint is not None:
                raise AIProviderError('validation')
            return None
        if kind == 'codex' and endpoint is None:
            return OPENAI_V1
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise AIProviderError('validation')
        try:
            # A Local address the user typed is theirs; check it without trusting it globally yet.
            value = validate_endpoint(endpoint.strip(), trusted=[endpoint.strip()] if kind == 'local' else [])
        except PlatformError:
            raise AIProviderError('validation') from None
        if (kind == 'codex' or adapter == 'openai_compatible') and not value.endswith('/v1'):
            raise AIProviderError('validation')
        return value

    def add(self, kind, *, endpoint=None, adapter=None, api_key=None, use_login=False, consent=False):
        if kind not in KINDS or type(use_login) is not bool or type(consent) is not bool:
            raise AIProviderError('validation')
        if api_key is not None and (not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 512):
            raise AIProviderError('validation')
        if kind == 'local':
            adapter = adapter or 'ollama'
            if adapter not in LOCAL_ADAPTERS or use_login or (api_key is not None and adapter == 'ollama'):
                raise AIProviderError('validation')
        elif adapter is not None:
            raise AIProviderError('validation')
        if kind == 'claude' and (api_key is None) == (not use_login):
            raise AIProviderError('validation')
        if kind == 'codex' and (api_key is None or use_login):
            raise AIProviderError('validation')
        endpoint = self._endpoint(kind, endpoint, adapter)
        if kind != 'local' and consent is not True:
            raise AIProviderError('consent_required')
        provider_id, now = new_id('aip'), self.clock()
        ref = self.secrets.create('provider_api_key', f'ai-provider:{provider_id}', api_key).id if api_key else None
        try:
            if kind != 'local':
                # One confirmation when adding: this provider may receive source for any job.
                self.providers.update_settings(local_only=False)
            auth = 'api_key' if api_key else 'cli_login' if use_login else 'none'
            with self.store.transaction() as db:
                db.execute('INSERT INTO ai_providers(id,kind,name,endpoint,adapter,auth,secret_ref,created_at,updated_at) '
                           'VALUES(?,?,?,?,?,?,?,?,?)', (provider_id, kind, NAMES[kind], endpoint, adapter, auth, ref, now, now))
                row = self._row(db, provider_id)
        except Exception:
            if ref:
                try:
                    self.secrets.revoke(ref)
                except Exception:
                    pass
            raise
        self.refresh_trust()
        return self._public(row)

    def _key(self, row):
        return self.secrets.resolve(row['secret_ref']).reveal() if row['secret_ref'] else None

    def _links(self, db, provider_id, kind):
        return db.execute('SELECT profile_id, model FROM ai_provider_profiles WHERE provider_id=? AND kind=?',
                          (provider_id, kind)).fetchall()

    def _managed(self, db):
        return db.execute('SELECT id, active_version_id FROM execution_profiles WHERE managed=1 AND deleted_at IS NULL'
                          ).fetchone()

    def update(self, provider_id, *, api_key=None, endpoint=None):
        with self.store.read() as db:
            row = self._row(db, provider_id)
        if api_key is None and endpoint is None:
            raise AIProviderError('validation')
        if api_key is not None and (row['auth'] != 'api_key' or not isinstance(api_key, str) or not api_key.strip()
                                    or len(api_key) > 512):
            raise AIProviderError('validation')
        changes = {}
        if endpoint is not None:
            if row['kind'] == 'claude':
                raise AIProviderError('validation')
            changes['endpoint'] = self._endpoint(row['kind'], endpoint, row['adapter'])
        new_ref = self.secrets.create('provider_api_key', f'ai-provider:{provider_id}', api_key).id if api_key else None
        try:
            with self.store.transaction() as db:
                db.execute('UPDATE ai_providers SET endpoint=coalesce(?,endpoint), secret_ref=coalesce(?,secret_ref), '
                           'updated_at=? WHERE id=?', (changes.get('endpoint'), new_ref, self.clock(), provider_id))
                links = self._links(db, provider_id, 'provider')
                coding = self._links(db, provider_id, 'execution')
                managed = self._managed(db)
        except Exception:
            if new_ref:
                try:
                    self.secrets.revoke(new_ref)
                except Exception:
                    pass
            raise
        if new_ref:
            self.secrets.revoke(row['secret_ref'])
        self.refresh_trust()
        for link in links:
            if not self._live_provider_profile(link['profile_id']):
                continue
            self.providers.update_profile(link['profile_id'], changes, secret_value=api_key)
        if api_key and managed and any(link['profile_id'] == managed['id'] for link in coding):
            self._version_execution(managed['id'], {'secret_ref': self.secrets.create(
                'provider_api_key', f'execution:{managed["id"]}', api_key).id})
        with self.store.read() as db:
            return self._public(self._row(db, provider_id))

    def remove(self, provider_id):
        with self.store.transaction() as db:
            row = self._row(db, provider_id)
            links = self._links(db, provider_id, 'provider')
            coding = [link['profile_id'] for link in self._links(db, provider_id, 'execution')]
            for table in ('ai_job_choices', 'ai_provider_profiles'):
                db.execute(f'DELETE FROM {table} WHERE provider_id=?', (provider_id,))
            db.execute('DELETE FROM ai_providers WHERE id=?', (provider_id,))
        self.refresh_trust()
        for link in links:
            if self._live_provider_profile(link['profile_id']):
                self.providers.delete_profile(link['profile_id'])
        for profile_id in coding:
            self._retire_execution(profile_id)
        if row['secret_ref']:
            self.secrets.revoke(row['secret_ref'])
        self._summary_follows_plans()

    # ----- profiles behind a job ---------------------------------------------------------

    def _live_provider_profile(self, profile_id):
        return any(p['id'] == profile_id for p in self.providers.list_profiles())

    def _chat_spec(self, row, model):
        kind, key = row['kind'], self._key(row)
        if kind == 'local':
            caps = CHAT if row['adapter'] == 'ollama' else ['chat']
            return {'adapter': row['adapter'], 'endpoint': row['endpoint'], 'model': model,
                    'config': {'capabilities': caps, **LOCAL_CHAT}}, key
        if kind == 'claude' and key is None:
            return {'adapter': 'claude_cli', 'endpoint': CLAUDE_CLI_ENDPOINT, 'model': model,
                    'config': {'capabilities': CHAT, **REMOTE_CHAT}}, None
        # Anthropic's OpenAI-compatible endpoint; plans fall back when structured output is refused.
        endpoint = ANTHROPIC_V1 if kind == 'claude' else row['endpoint']
        caps = ['chat'] if kind == 'claude' else CHAT
        return {'adapter': 'openai_compatible', 'endpoint': endpoint, 'model': model,
                'config': {'capabilities': caps, **REMOTE_CHAT}}, key

    def _profile_for(self, row, model, purpose):
        """Reuse the provider's live profile for this model and purpose, or create it."""
        link_model = model if purpose == 'chat' else f'{purpose}:{model}'
        with self.store.read() as db:
            for link in self._links(db, row['id'], 'provider'):
                if link['model'] == link_model and self._live_provider_profile(link['profile_id']):
                    return link['profile_id']
        if purpose == 'embedding':
            dims = probe_embedding_dimensions(row['endpoint'], model, transport=self.providers.transport)
            spec, key = {'adapter': 'ollama', 'endpoint': row['endpoint'], 'model': model,
                         'config': {'capabilities': ['embeddings'], 'embedding_dimensions': dims,
                                    'timeout_seconds': 180}}, None
        else:
            spec, key = self._chat_spec(row, model)
        name = f"{row['name']} · {model}"[:90]
        for attempt in range(3):
            try:
                profile = self.providers.create_profile({**spec, 'name': name}, secret_value=key)
                break
            except ProfileNameConflict:
                name = f"{row['name']} · {model}"[:80] + ' · ' + new_id('n')[-6:]
        else:
            raise AIProviderError('validation')
        with self.store.transaction() as db:
            db.execute("INSERT INTO ai_provider_profiles VALUES(?,'provider',?,?)", (row['id'], profile['id'], link_model))
        return profile['id']

    def _version_execution(self, profile_id, changes):
        with self.store.read() as db:
            old = db.execute('SELECT secret_ref FROM execution_profile_versions v JOIN execution_profiles p '
                             'ON p.active_version_id=v.id WHERE p.id=?', (profile_id,)).fetchone()['secret_ref']
        self.execution_profiles.update(profile_id, changes)
        if old and changes.get('secret_ref', old) != old:
            self.secrets.revoke(old)
        # A queued test of a superseded version would block testing the new one; cancel it.
        with self.store.read() as db:
            stale = [r[0] for r in db.execute(
                "SELECT c.job_id FROM execution_profile_checks c JOIN execution_profile_versions v ON v.id=c.version_id "
                "JOIN jobs j ON j.id=c.job_id WHERE v.profile_id=? AND j.state='queued'", (profile_id,))]
        for job_id in stale:
            self.execution_profiles.jobs.cancel(job_id)
        self.execution_profiles.request_test(profile_id)

    def _retire_execution(self, profile_id):
        self.execution_profiles.delete(profile_id)
        with self.store.read() as db:
            refs = [r[0] for r in db.execute('SELECT DISTINCT secret_ref FROM execution_profile_versions '
                                             'WHERE profile_id=? AND secret_ref IS NOT NULL', (profile_id,))]
        for ref in refs:
            try:
                self.secrets.revoke(ref)
            except PlatformError:
                pass

    def _choose_coding(self, row, model):
        if row['kind'] != 'claude':
            raise AIProviderError('coding_unsupported')
        if self.execution_profiles is None:
            raise AIProviderError('coding_unavailable')
        if not AGENT_MODEL.fullmatch(model):  # check before anything changes
            raise AIProviderError('validation')
        key = self._key(row)
        with self.store.read() as db:
            managed = self._managed(db)
            owner = db.execute("SELECT provider_id FROM ai_provider_profiles WHERE kind='execution' AND profile_id=?",
                               (managed['id'],)).fetchone() if managed else None
            current = (db.execute('SELECT auth_mode FROM execution_profile_versions WHERE id=?',
                                  (managed['active_version_id'],)).fetchone()['auth_mode'] if managed else None)
        same_owner = owner is not None and owner['provider_id'] == row['id']
        if managed and same_owner and current == ('api_key' if key else 'cli_login'):
            self._version_execution(managed['id'], {'agent_model': model})
            profile_id = managed['id']
        else:
            # Create the new setup first; the working one is retired only once it exists.
            spec = {'name': 'Claude coding · ' + new_id('c')[-6:], 'adapter': 'claude_code',
                    'auth_mode': 'api_key' if key else 'cli_login', 'image': CODING_IMAGE,
                    'allow_external_source': True, 'agent_model': model}
            if key:
                spec['secret_ref'] = self.secrets.create('provider_api_key', 'execution:managed', key).id
            try:
                profile_id = self.execution_profiles.create(spec, managed=True)['id']
            except BaseException:
                if key:
                    self.secrets.revoke(spec['secret_ref'])
                raise
            if managed:
                self._retire_execution(managed['id'])
            self.execution_profiles.request_test(profile_id)
        with self.store.transaction() as db:
            db.execute("DELETE FROM ai_provider_profiles WHERE kind='execution'")
            db.execute("INSERT INTO ai_provider_profiles VALUES(?,'execution',?,?)", (row['id'], profile_id, model))

    # ----- jobs --------------------------------------------------------------------------

    def _choices(self, db):
        return {r['job']: r for r in db.execute('SELECT * FROM ai_job_choices')}

    def _remember(self, job, provider_id, model):
        with self.store.transaction() as db:
            if provider_id is None:
                db.execute('DELETE FROM ai_job_choices WHERE job=?', (job,))
            else:
                db.execute('INSERT INTO ai_job_choices VALUES(?,?,?,?) ON CONFLICT(job) DO UPDATE SET '
                           'provider_id=excluded.provider_id, model=excluded.model, updated_at=excluded.updated_at',
                           (job, provider_id, model, self.clock()))

    def choose(self, job, provider_id, model=None):
        if job not in JOBS:
            raise AIProviderError('not_found')
        with self.store.read() as db:
            separate_summary = 'summary' in self._choices(db)
            row = None if provider_id is None else self._row(db, provider_id)
            managed = self._managed(db)
        if row is None:
            if job == 'coding':
                if managed:
                    self._retire_execution(managed['id'])
            else:
                roles = {'plans': ['ask', 'planning'] + ([] if separate_summary else ['summary']),
                         'summary': ['summary'], 'search': ['embedding']}[job]
                self.providers.assign_roles({role: None for role in roles})
            self._remember(job, None, None)
            if job == 'summary':
                self._summary_follows_plans()
            return self.jobs()
        model = _model(model)
        if job == 'coding':
            self._choose_coding(row, model)
        elif job == 'search':
            if row['kind'] != 'local' or row['adapter'] != 'ollama':
                raise AIProviderError('search_unsupported')
            try:
                profile_id = self._profile_for(row, model, 'embedding')
            except ValueError:
                raise AIProviderError('search_unsupported') from None
            self.providers.assign_roles({'embedding': profile_id})
        else:
            profile_id = self._profile_for(row, model, 'chat')
            roles = ['summary'] if job == 'summary' else ['ask', 'planning'] + ([] if separate_summary else ['summary'])
            self.providers.assign_roles({role: profile_id for role in roles})
        self._remember(job, row['id'], model)
        return self.jobs()

    def _summary_follows_plans(self):
        """Without its own choice, repository summaries use the Plans & answers model."""
        with self.store.read() as db:
            if 'summary' in self._choices(db):
                return
        self.providers.assign_roles({'summary': self.providers.roles()['planning']['profile_id']})

    def test_job(self, job):
        """Run the existing synthetic check for the profile behind a job."""
        if job == 'coding':
            with self.store.read() as db:
                managed = self._managed(db)
            if managed is None:
                raise AIProviderError('not_found')
            self.execution_profiles.request_test(managed['id'])
            return self.jobs()
        role = {'plans': 'planning', 'summary': 'summary', 'search': 'embedding'}.get(job)
        if role is None:
            raise AIProviderError('not_found')
        version_id = self.providers.roles()[role]['version_id']
        if version_id is None:
            raise AIProviderError('not_found')
        self.providers.test_profile(version_id, capabilities=['embeddings'] if role == 'embedding' else ['chat'])
        return self.jobs()

    def jobs(self):
        roles = self.providers.roles()
        local_only = self.providers.settings().get('local_only') is not False
        with self.store.read() as db:
            choices = self._choices(db)
            managed = self._managed(db)
            coding_link = db.execute("SELECT provider_id, model FROM ai_provider_profiles WHERE kind='execution'"
                                     ).fetchone()

        def role_job(job, role):
            choice, state = choices.get(job), roles[role]
            return {'provider_id': choice['provider_id'] if choice else None,
                    'model': choice['model'] if choice else state['model'], 'status': state['status']}

        result = {'plans': role_job('plans', 'planning'), 'search': role_job('search', 'embedding'),
                  'summary': role_job('summary' if 'summary' in choices else 'plans', 'summary')}
        if managed is None or self.execution_profiles is None:
            result['coding'] = {'provider_id': None, 'model': None, 'status': 'unassigned'}
        else:
            check = self.execution_profiles.get(managed['id'])['check']
            status = 'blocked' if local_only else (check['status'] if check else 'untested')
            result['coding'] = {'provider_id': coding_link['provider_id'] if coding_link else None,
                                'model': coding_link['model'] if coding_link else None, 'status': status,
                                'error_code': check.get('error_code') if check else None}
        return result
