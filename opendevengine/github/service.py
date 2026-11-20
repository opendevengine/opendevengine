"""GitHub App connections, durable catalog discovery and access boundaries."""
import base64
import hashlib
import json
import re
import secrets as random
import threading
from urllib.parse import urlsplit
from .client import GitHubClient, jwt, private_key as validate_private_key, positive_id, component, permissions as validate_permissions
from .errors import GitHubError
from opendevengine.platform.clock import now_ms, new_id
from opendevengine.platform.errors import SecretRevoked, SecretMissing, SecretStoreUnavailable


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


class _RefreshCancelled(Exception):
    pass


class GitHubService:
    def __init__(self, store, jobs, secrets, *, transport=None, clock=now_ms):
        self.store, self.jobs, self.secrets, self.clock = store, jobs, secrets, clock
        self.client = GitHubClient(transport=transport, clock=clock)
        self.listeners, self.purge_hooks = [], {}
        self.manifest_states, self.manifest_lock = {}, threading.Lock()

    def _one(self, table, ident):
        with self.store.read() as db:
            row = db.execute('SELECT * FROM ' + table + ' WHERE id=?', (ident,)).fetchone()
            if row is None:
                raise GitHubError('not_found')
            return dict(row)

    def _public_connection(self, value):
        return {k: bool(v) if k == 'index_new' else v for k, v in value.items() if k != 'private_key_ref'}

    def _app_token(self, connection):
        try:
            pem = self.secrets.resolve(connection['private_key_ref']).reveal()
        except (SecretRevoked, SecretMissing):
            raise GitHubError('auth') from None
        except SecretStoreUnavailable:
            raise GitHubError('secret_store_unavailable') from None
        return jwt(connection['app_id'], pem, self.clock())

    def configure(self, app_id, slug, private_key, *, index_new=False):
        app_id = positive_id(app_id)
        if not isinstance(slug, str) or type(index_new) is not bool:
            raise GitHubError('validation')
        try:
            component(slug)
        except GitHubError:
            raise GitHubError('validation') from None
        if not self.store.persistent:
            raise GitHubError('persistence_required')
        if not self.secrets.status().available:
            raise GitHubError('secret_store_unavailable')
        validate_private_key(private_key)
        identity = self.client.app(jwt(app_id, private_key, self.clock()))
        if not isinstance(identity, dict) or str(identity.get('id')) != app_id or identity.get('slug') != slug:
            raise GitHubError('app_identity_mismatch')
        ref = self.secrets.create('github_app_private_key', 'GitHub App', private_key)
        old = None
        try:
            with self.store.transaction() as db:
                prior = db.execute('SELECT * FROM github_connections WHERE app_id=?', (app_id,)).fetchone()
                now = self.clock()
                if prior:
                    ident, old = prior['id'], prior['private_key_ref']
                    db.execute('UPDATE github_connections SET slug=?,private_key_ref=?,index_new=?,updated_at=?,last_error_code=NULL WHERE id=?',
                               (slug, ref.id, index_new, now, ident))
                else:
                    ident = new_id('ghc')
                    db.execute('INSERT INTO github_connections(id,app_id,slug,private_key_ref,status,index_new,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                               (ident, app_id, slug, ref.id, 'configured', index_new, now, now))
        except Exception:
            self.secrets.revoke(ref.id)
            raise
        if old:
            self.secrets.revoke(old)
        return self._public_connection(self._one('github_connections', ident))

    def can_configure(self):
        """True when a created App's private key can actually be kept (persistent store and secure vault)."""
        return bool(self.store.persistent) and bool(self.secrets.status().available)

    def _require_key_storage(self):
        if not self.store.persistent:
            raise GitHubError('persistence_required')
        if not self.secrets.status().available:
            raise GitHubError('secret_store_unavailable')

    def begin_manifest(self, *, public_base_url, organization=None):
        """Prepare a GitHub App manifest; the state is in-memory, single use and expires in 10 minutes."""
        # Refuse up front: a created App whose key cannot be stored is orphaned on GitHub.
        self._require_key_storage()
        parts = urlsplit(public_base_url) if isinstance(public_base_url, str) else None
        if parts is None or parts.scheme != 'http' or parts.hostname not in ('127.0.0.1', 'localhost') \
                or parts.path not in ('', '/') or parts.query or parts.username:
            raise GitHubError('validation')
        if organization is not None and (not isinstance(organization, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,39}', organization)):
            raise GitHubError('validation')
        base = public_base_url.rstrip('/')
        callback = base + '/api/github/manifest/callback'
        manifest = {'name': 'opendevengine-' + random.token_hex(3), 'url': callback, 'redirect_url': callback,
                    'setup_url': base + '/api/github/callback',
                    # Draft PRs need contents and pull_requests write; per-call tokens stay narrower, and writes
                    # happen only on repositories where the user enabled implementation.
                    'public': False, 'default_permissions': {'contents': 'write', 'metadata': 'read', 'pull_requests': 'write'},
                    'default_events': [], 'request_oauth_on_install': False, 'setup_on_update': False}
        state, expires = random.token_urlsafe(32), self.clock() + 600000
        with self.manifest_lock:
            now = self.clock()
            self.manifest_states = {k: v for k, v in self.manifest_states.items() if v > now}
            self.manifest_states[hashlib.sha256(state.encode()).hexdigest()] = expires
        action = ('https://github.com/organizations/' + organization + '/settings/apps/new') if organization \
            else 'https://github.com/settings/apps/new'
        return {'action': action, 'state': state, 'manifest': manifest, 'expires_at': expires}

    def finish_manifest(self, state, code):
        key = hashlib.sha256(state.encode()).hexdigest() if isinstance(state, str) and 1 <= len(state) <= 200 else None
        with self.manifest_lock:
            expires = self.manifest_states.get(key) if key else None
            if expires is None or expires <= self.clock():
                raise GitHubError('manifest_state_invalid')
            # A locked vault must not consume the browser's retryable callback.
            # Check and claim under one lock so concurrent callbacks cannot both
            # spend GitHub's single-use conversion code after storage recovers.
            self._require_key_storage()
            self.manifest_states.pop(key)
        app = self.client.convert_manifest(code)
        try:
            return self.configure(app['id'], app['slug'], app['pem'])
        except GitHubError:
            raise GitHubError('manifest_key_not_saved') from None

    def connections(self):
        with self.store.read() as db:
            return [self._public_connection(dict(r)) for r in db.execute('SELECT * FROM github_connections ORDER BY id LIMIT 100')]

    def begin_connect(self, connection_id):
        connection = self._one('github_connections', connection_id)
        # Check the credential before accepting a setup attempt.
        self._app_token(connection)
        state = random.token_urlsafe(32)
        expires = self.clock() + 600000
        with self.store.transaction() as db:
            db.execute('INSERT INTO github_setup_attempts VALUES (?,?,?,NULL)',
                       (hashlib.sha256(state.encode()).hexdigest(), connection_id, expires))
            db.execute("UPDATE github_connections SET status=CASE WHEN status='connected' THEN status ELSE 'connecting' END,updated_at=? WHERE id=?", (self.clock(), connection_id))
        return {'connection_id': connection_id, 'install_url': 'https://github.com/apps/' + connection['slug'] + '/installations/new?state=' + state, 'expires_at': expires}

    def finish_connect(self, state, installation_id, *, setup_action=None):
        if not isinstance(state, str) or not 1 <= len(state) <= 200:
            raise GitHubError('setup_state_invalid')
        digest = hashlib.sha256(state.encode()).hexdigest()
        with self.store.transaction() as db:
            attempt = db.execute('SELECT * FROM github_setup_attempts WHERE state_hash=?', (digest,)).fetchone()
            if attempt is None or attempt['consumed_at'] is not None or attempt['expires_at'] <= self.clock():
                raise GitHubError('setup_state_invalid')
            db.execute('UPDATE github_setup_attempts SET consumed_at=? WHERE state_hash=?', (self.clock(), digest))
            connection = dict(db.execute('SELECT * FROM github_connections WHERE id=?', (attempt['connection_id'],)).fetchone())
        if setup_action not in (None, 'install', 'update', 'request'):
            raise GitHubError('validation')
        if setup_action == 'request':
            with self.store.transaction() as db:
                if not db.execute('SELECT 1 FROM github_setup_attempts WHERE state_hash=?', (digest,)).fetchone() or self._one('github_connections', connection['id'])['status'] == 'disconnected':
                    raise GitHubError('connection_unavailable')
                db.execute("UPDATE github_connections SET status=CASE WHEN status='connected' THEN status ELSE 'pending_approval' END,updated_at=? WHERE id=?", (self.clock(), connection['id']))
            result = self._public_connection(self._one('github_connections', connection['id']))
            result['setup_status'] = 'pending_approval'
            return result
        installation_id = positive_id(installation_id)
        try:
            value = self.client.installation(self._app_token(connection), installation_id, connection['app_id'])
        except GitHubError as exc:
            if exc.code in ('not_found', 'installation_suspended'):
                self._invalidate_installation(connection['id'], installation_id, 'suspended' if exc.code == 'installation_suspended' else 'revoked')
            raise
        before = self._eligible_map()
        with self.store.transaction() as db:
            current = db.execute('SELECT * FROM github_connections WHERE id=?', (connection['id'],)).fetchone()
            attempt_exists = db.execute('SELECT 1 FROM github_setup_attempts WHERE state_hash=?', (digest,)).fetchone()
            if not attempt_exists or current['status'] == 'disconnected' or current['private_key_ref'] != connection['private_key_ref']:
                raise GitHubError('connection_unavailable')
            prior = db.execute('SELECT connection_id FROM github_installations WHERE id=?', (installation_id,)).fetchone()
            if prior and prior[0] != connection['id']:
                raise GitHubError('installation_mismatch')
            db.execute('INSERT INTO github_installations(id,connection_id,owner,account_type,permissions,status,repository_selection) VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,account_type=excluded.account_type,permissions=excluded.permissions,status=excluded.status,repository_selection=excluded.repository_selection,suspended_at=NULL',
                       (installation_id, connection['id'], value['account']['login'], value['account']['type'], encoded(value['permissions']), 'active', value.get('repository_selection', 'selected')))
            db.execute('UPDATE repository_installations SET permissions=? WHERE installation_id=?', (encoded(value['permissions']), installation_id))
            db.execute("UPDATE github_connections SET status='connected',last_error_code=NULL,updated_at=? WHERE id=?", (self.clock(), connection['id']))
            self._access_states(db)
            # A verified callback supersedes every in-flight metadata snapshot. Fence
            # runs by durable identity/state, even if both observations share a clock tick.
            active_runs = db.execute("SELECT job_id FROM discovery_runs WHERE connection_id=? AND state IN ('queued','running','waiting')", (connection['id'],)).fetchall()
            if active_runs:
                db.execute("UPDATE discovery_runs SET state='cancelled',updated_at=? WHERE connection_id=? AND state IN ('queued','running','waiting')", (self.clock(), connection['id']))
                for active in active_runs:
                    db.execute("UPDATE jobs SET state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,cancel_requested=1,updated_at=? WHERE id=? AND state IN ('queued','leased')", (self.clock(), active['job_id']))
                # Replacement and cancellation commit with the verified grants. A crash
                # cannot leave only an obsolete continuation or lose the requested refresh.
                self._create_refresh(db, connection['id'])
        self._notify(before)
        return self._public_connection(self._one('github_connections', connection['id']))

    def _run_result(self, row):
        return {'run_id': row['id'], 'job_id': row['job_id'], 'state': row['state'], 'retry_at': row['retry_at']}

    def refresh(self, connection_id):
        with self.store.transaction() as db:
            c = db.execute('SELECT * FROM github_connections WHERE id=?', (connection_id,)).fetchone()
            if c is None:
                raise GitHubError('not_found')
            if c['status'] != 'connected':
                raise GitHubError('connection_unavailable')
            existing = db.execute("SELECT * FROM discovery_runs WHERE connection_id=? AND state IN ('queued','running','waiting')", (connection_id,)).fetchone()
            if existing:
                queued_job = db.execute('SELECT state FROM jobs WHERE id=?', (existing['job_id'],)).fetchone()
                if queued_job and queued_job['state'] in ('failed', 'cancelled', 'succeeded'):
                    db.execute("UPDATE discovery_runs SET state='failed',error_code='worker_interrupted',updated_at=? WHERE id=?", (self.clock(), existing['id']))
                else:
                    return self._run_result(existing)
            return self._create_refresh(db, connection_id)

    def _create_refresh(self, db, connection_id):
        run_id, generation, now = new_id('ghr'), new_id('gen'), self.clock()
        job = self.jobs.enqueue_in_transaction(db, 'github.refresh', {'run_id': run_id}, idempotency_key='github.refresh:' + run_id)
        db.execute('INSERT INTO discovery_runs(id,connection_id,generation,state,job_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                   (run_id, connection_id, generation, 'queued', job.id, now, now))
        return self._run_result(db.execute('SELECT * FROM discovery_runs WHERE id=?', (run_id,)).fetchone())

    def schedule_due(self):
        now = self.clock()
        with self.store.read() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM github_connections WHERE status='connected' AND (last_refresh_at IS NULL OR last_refresh_at+refresh_interval_ms<=?) AND (next_retry_at IS NULL OR next_retry_at<=?)", (now, now))]
        return [self.refresh(ident) for ident in ids]

    def update_refresh_interval(self, connection_id, interval_ms):
        """Persist periodic polling policy, with the accepted fifteen-minute minimum."""
        if type(interval_ms) is not int or not 900000 <= interval_ms <= 2592000000:
            raise GitHubError('validation')
        self._one('github_connections', connection_id)
        with self.store.transaction() as db:
            db.execute('UPDATE github_connections SET refresh_interval_ms=?,updated_at=? WHERE id=?', (interval_ms, self.clock(), connection_id))
        return self._public_connection(self._one('github_connections', connection_id))

    def register_eligibility_listener(self, callback):
        if not callable(callback):
            raise GitHubError('validation')
        self.listeners.append(callback)

    def register_purge_hook(self, name, callback):
        if not isinstance(name, str) or not name or not callable(callback):
            raise GitHubError('validation')
        self.purge_hooks[name] = callback

    _ELIGIBLE = "r.index_enabled AND EXISTS(SELECT 1 FROM repository_installations ri JOIN github_installations i ON i.id=ri.installation_id JOIN github_connections c ON c.id=i.connection_id WHERE ri.repository_id=r.id AND i.status='active' AND c.status='connected')"

    def _eligible_map(self):
        with self.store.read() as db:
            return {r[0]: bool(r[1]) for r in db.execute("SELECT r.id, " + self._ELIGIBLE + " FROM repositories r")}

    def _notify(self, before):
        after = self._eligible_map()
        for ident in before.keys() | after.keys():
            if before.get(ident, False) != after.get(ident, False):
                for callback in self.listeners:
                    callback(ident, after.get(ident, False))

    def _access_states(self, db):
        db.execute("UPDATE repositories SET access_state=CASE WHEN EXISTS(SELECT 1 FROM repository_installations ri JOIN github_installations i ON i.id=ri.installation_id JOIN github_connections c ON c.id=i.connection_id WHERE ri.repository_id=repositories.id AND i.status='active' AND c.status='connected') THEN 'accessible' ELSE 'inaccessible' END")

    def _invalidate_installation(self, connection_id, installation_id, status, *, run_id=None):
        before = self._eligible_map()
        with self.store.transaction() as db:
            if run_id is not None and not self._live(db, run_id, installation_id):
                raise _RefreshCancelled()
            owned = db.execute('SELECT 1 FROM github_installations WHERE id=? AND connection_id=?', (installation_id, connection_id)).fetchone()
            if owned:
                db.execute('UPDATE github_installations SET status=? WHERE id=?', (status, installation_id))
                db.execute('DELETE FROM repository_installations WHERE installation_id=?', (installation_id,))
                self._access_states(db)
        self._notify(before)

    def _live(self, db, run_id, installation_id=None):
        run = db.execute("SELECT d.* FROM discovery_runs d JOIN github_connections c ON c.id=d.connection_id WHERE d.id=? AND c.status='connected' AND d.state IN ('queued','running','waiting')", (run_id,)).fetchone()
        if run is None:
            return False
        if installation_id is not None:
            return db.execute("SELECT 1 FROM github_installations WHERE id=? AND connection_id=? AND status='active'", (installation_id, run['connection_id'])).fetchone() is not None
        return True

    def _repository(self, value):
        if not isinstance(value, dict):
            raise GitHubError('malformed_output')
        try:
            ident = positive_id(value['id'])
            owner, name = component(value['owner']['login']), component(value['name'])
            branch = value['default_branch']
            if not isinstance(branch, str) or not 1 <= len(branch) <= 255:
                raise ValueError()
            description = '' if value.get('description') is None else value['description']
            topics = value.get('topics', [])
            language, pushed = value.get('language'), value.get('pushed_at')
            if not isinstance(description, str) or len(description) > 20000 or not isinstance(topics, list) or len(topics) > 100 or any(not isinstance(t, str) or len(t) > 100 for t in topics):
                raise ValueError()
            if any(v is not None and (not isinstance(v, str) or len(v) > 200) for v in (language, pushed)):
                raise ValueError()
            if any(type(value.get(k, False)) is not bool for k in ('archived', 'fork', 'private')):
                raise ValueError()
            visibility = value.get('visibility', 'private' if value.get('private') else 'public')
            if visibility not in ('public', 'private', 'internal'):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise GitHubError('malformed_output') from None
        return ('gh:' + ident, int(ident), owner, name, owner + '/' + name, description, encoded(topics), branch,
                language, visibility, value.get('archived', False), value.get('fork', False),
                'https://github.com/' + owner + '/' + name + '.git', pushed)

    def _cancel_refresh(self, run_id):
        before = self._eligible_map()
        with self.store.transaction() as db:
            db.execute("UPDATE discovery_runs SET state='cancelled',updated_at=? WHERE id=? AND state IN ('queued','running','waiting')", (self.clock(), run_id))
            self._access_states(db)
        self._notify(before)
        return {'state': 'cancelled'}

    def run_refresh(self, job, context):
        run_id = job.payload['run_id']
        run = self._one('discovery_runs', run_id)
        connection = self._one('github_connections', run['connection_id'])
        with self.store.transaction() as db:
            if not self._live(db, run_id) or run['job_id'] != job.id:
                return {'state': 'cancelled'}
            db.execute("UPDATE discovery_runs SET state='running',updated_at=? WHERE id=?", (self.clock(), run_id))
        checkpoint = json.loads(run['checkpoint'])
        completed = checkpoint.get('completed', [])
        with self.store.read() as db:
            installations = [dict(r) for r in db.execute("SELECT * FROM github_installations WHERE connection_id=? AND status='active' ORDER BY id", (connection['id'],))]
        try:
            for installation in installations:
                iid = installation['id']
                if iid in completed:
                    continue
                try:
                    metadata = self.client.installation(self._app_token(connection), iid, connection['app_id'])
                except GitHubError as exc:
                    if exc.code not in ('not_found', 'installation_suspended'):
                        raise
                    before = self._eligible_map()
                    with self.store.transaction() as db:
                        if not self._live(db, run_id, iid):
                            raise _RefreshCancelled()
                        db.execute('UPDATE github_installations SET status=? WHERE id=?', ('suspended' if exc.code == 'installation_suspended' else 'revoked', iid))
                        db.execute('DELETE FROM repository_installations WHERE installation_id=?', (iid,))
                        self._access_states(db)
                    self._notify(before)
                    completed.append(iid)
                    continue
                before = self._eligible_map()
                with self.store.transaction() as db:
                    if not self._live(db, run_id, iid):
                        raise _RefreshCancelled()
                    db.execute('UPDATE github_installations SET owner=?,permissions=? WHERE id=?', (metadata['account']['login'], encoded(metadata['permissions']), iid))
                    db.execute('UPDATE repository_installations SET permissions=? WHERE installation_id=?', (encoded(metadata['permissions']), iid))
                self._notify(before)
                try:
                    token = self.client.mint_token(self._app_token(connection), iid, permissions={'contents': 'read'})['token']
                except GitHubError as exc:
                    if exc.code != 'not_found':
                        raise
                    self._invalidate_installation(connection['id'], iid, 'revoked', run_id=run_id)
                    completed.append(iid)
                    continue
                page = checkpoint.get('page', 1) if checkpoint.get('installation_id') == iid else 1
                while True:
                    if context is not None and context.cancel_requested:
                        with self.store.transaction() as db:
                            db.execute("UPDATE discovery_runs SET state='cancelled',updated_at=? WHERE id=? AND state IN ('queued','running','waiting')", (self.clock(), run_id))
                        raise _RefreshCancelled()
                    values = self.client.repositories(token, page)
                    records = [self._repository(v) for v in values]
                    if context is not None and context.cancel_requested:
                        raise _RefreshCancelled()
                    before = self._eligible_map()
                    with self.store.transaction() as db:
                        if not self._live(db, run_id, iid):
                            raise _RefreshCancelled()
                        current = db.execute('SELECT * FROM github_connections WHERE id=?', (connection['id'],)).fetchone()
                        if current['private_key_ref'] != connection['private_key_ref']:
                            raise GitHubError('auth')
                        db.execute('UPDATE github_installations SET owner=?,permissions=? WHERE id=?', (metadata['account']['login'], encoded(metadata['permissions']), iid))
                        for record in records:
                            db.execute('INSERT INTO repositories(id,github_id,owner,name,full_name,description,topics,default_branch,language,visibility,archived,fork,clone_url,pushed_at,index_enabled,implementation_enabled,access_state,created_at,updated_at) VALUES (' + ','.join('?' for _ in range(19)) + ') ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,name=excluded.name,full_name=excluded.full_name,description=excluded.description,topics=excluded.topics,default_branch=excluded.default_branch,language=excluded.language,visibility=excluded.visibility,archived=excluded.archived,fork=excluded.fork,clone_url=excluded.clone_url,pushed_at=excluded.pushed_at,updated_at=excluded.updated_at',
                                       (*record, current['index_new'], False, 'accessible', self.clock(), self.clock()))
                            db.execute('INSERT INTO repository_installations VALUES (?,?,?,?) ON CONFLICT(repository_id,installation_id) DO UPDATE SET permissions=excluded.permissions,seen_generation=excluded.seen_generation',
                                       (record[0], iid, encoded(metadata['permissions']), run['generation']))
                        if len(values) < 100:
                            db.execute('DELETE FROM repository_installations WHERE installation_id=? AND seen_generation<>?', (iid, run['generation']))
                            db.execute('UPDATE github_installations SET last_refresh_at=? WHERE id=?', (self.clock(), iid))
                            completed.append(iid)
                            checkpoint = {'completed': completed}
                        else:
                            checkpoint = {'completed': completed, 'installation_id': iid, 'page': page + 1}
                        self._access_states(db)
                        db.execute('UPDATE discovery_runs SET checkpoint=?,discovered_count=discovered_count+?,updated_at=? WHERE id=?', (encoded(checkpoint), len(records), self.clock(), run_id))
                    self._notify(before)
                    if context is not None:
                        context.checkpoint({'run_id': run_id})
                    if len(values) < 100:
                        break
                    page += 1
            with self.store.transaction() as db:
                if not self._live(db, run_id):
                    raise _RefreshCancelled()
                db.execute("UPDATE discovery_runs SET state='completed',error_code=NULL,retry_at=NULL,updated_at=? WHERE id=?", (self.clock(), run_id))
                db.execute('UPDATE github_connections SET last_refresh_at=?,next_retry_at=NULL,last_error_code=NULL WHERE id=?', (self.clock(), connection['id']))
            return {'state': 'completed', 'run_id': run_id}
        except _RefreshCancelled:
            return self._cancel_refresh(run_id)
        except GitHubError as exc:
            before = self._eligible_map()
            with self.store.transaction() as db:
                if not self._live(db, run_id):
                    return {'state': 'cancelled'}
                count = self._one('discovery_runs', run_id)['continuation'] + 1
                retry = exc.code == 'rate_limited' or (exc.code in ('timeout', 'upstream_unavailable', 'secret_store_unavailable') and count <= 3)
                retry_at = exc.retry_at if exc.code == 'rate_limited' else self.clock() + min(2000 * 2 ** min(count - 1, 8), 300000) if retry else None
                if retry:
                    continuation = self.jobs.enqueue_in_transaction(db, 'github.refresh', {'run_id': run_id}, run_after=retry_at, idempotency_key='github.refresh:%s:%s' % (run_id, count))
                    db.execute("UPDATE discovery_runs SET state='waiting',job_id=?,continuation=?,error_code=?,retry_at=?,updated_at=? WHERE id=?", (continuation.id, count, exc.code, retry_at, self.clock(), run_id))
                else:
                    db.execute("UPDATE discovery_runs SET state='failed',error_code=?,updated_at=? WHERE id=?", (exc.code, self.clock(), run_id))
                connection_retry = retry_at
                if not retry and exc.code in ('timeout', 'upstream_unavailable', 'secret_store_unavailable'):
                    connection_retry = self.clock() + connection['refresh_interval_ms']
                db.execute('UPDATE github_connections SET last_error_code=?,next_retry_at=? WHERE id=?', (exc.code, connection_retry, connection['id']))
                if exc.code == 'auth':
                    db.execute("UPDATE github_connections SET status='reconnect_required' WHERE id=?", (connection['id'],))
                    self._access_states(db)
            self._notify(before)
            return {'state': 'waiting' if retry else 'failed', 'error_code': exc.code, 'retry_at': retry_at}

    def disconnect(self, connection_id):
        self._one('github_connections', connection_id)
        before = self._eligible_map()
        with self.store.transaction() as db:
            db.execute("UPDATE github_connections SET status='disconnected',updated_at=? WHERE id=?", (self.clock(), connection_id))
            db.execute('DELETE FROM github_setup_attempts WHERE connection_id=?', (connection_id,))
            runs = db.execute("SELECT job_id FROM discovery_runs WHERE connection_id=? AND state IN ('queued','running','waiting')", (connection_id,)).fetchall()
            db.execute("UPDATE discovery_runs SET state='cancelled',updated_at=? WHERE connection_id=? AND state IN ('queued','running','waiting')", (self.clock(), connection_id))
            for run in runs:
                db.execute("UPDATE jobs SET state=CASE WHEN state='queued' THEN 'cancelled' ELSE state END,cancel_requested=1,updated_at=? WHERE id=? AND state IN ('queued','leased')", (self.clock(), run[0]))
            db.execute("UPDATE github_installations SET status='revoked' WHERE connection_id=?", (connection_id,))
            db.execute('DELETE FROM repository_installations WHERE installation_id IN (SELECT id FROM github_installations WHERE connection_id=?)', (connection_id,))
            self._access_states(db)
        self._notify(before)
        return self._public_connection(self._one('github_connections', connection_id))

    def _public_repository(self, row):
        value = dict(row)
        value['topics'] = json.loads(value['topics'])
        for key in ('archived', 'fork', 'index_enabled', 'implementation_enabled'):
            value[key] = bool(value[key])
        return value

    def get_repository(self, repository_id):
        return self._public_repository(self._one('repositories', repository_id))

    def is_eligible(self, repository_id):
        self._one('repositories', repository_id)
        # One row, not the whole map: routing asks this per candidate and per search hit.
        with self.store.read() as db:
            row = db.execute("SELECT " + self._ELIGIBLE + " FROM repositories r WHERE r.id=?", (repository_id,)).fetchone()
        return bool(row and row[0])

    def list_repositories(self, *, cursor=None, limit=50, query='', owner=None, index_enabled=None):
        if type(limit) is not int or not 1 <= limit <= 100 or not isinstance(query, str) or len(query) > 200 or (owner is not None and not isinstance(owner, str)) or (index_enabled is not None and type(index_enabled) is not bool):
            raise GitHubError('validation')
        filters, args = [], []
        if query:
            filters.append("instr(lower(full_name || ' ' || description), lower(?))>0")
            args.append(query)
        if owner is not None:
            filters.append('owner=?')
            args.append(owner)
        if index_enabled is not None:
            filters.append('index_enabled=?')
            args.append(index_enabled)
        signature = hashlib.sha256(encoded([query, owner, index_enabled]).encode()).hexdigest()
        after = None
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 500:
                    raise ValueError()
                data = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if data['filter'] != signature or not isinstance(data['after'], str):
                    raise ValueError()
                after = data['after']
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise GitHubError('validation') from None
        where = ' WHERE ' + ' AND '.join(filters) if filters else ''
        with self.store.read() as db:
            total = db.execute('SELECT count(*) FROM repositories' + where, args).fetchone()[0]
            if after is not None:
                filters.append('id>?')
                args.append(after)
            where = ' WHERE ' + ' AND '.join(filters) if filters else ''
            rows = db.execute('SELECT * FROM repositories' + where + ' ORDER BY id LIMIT ?', (*args, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = base64.urlsafe_b64encode(encoded({'filter': signature, 'after': rows[-1]['id']}).encode()).decode() if more else None
        return {'items': [self._public_repository(r) for r in rows], 'next_cursor': next_cursor, 'total': total}

    def update_selection(self, repository_id, *, index_enabled, implementation_enabled):
        if type(index_enabled) is not bool or type(implementation_enabled) is not bool:
            raise GitHubError('validation')
        self._one('repositories', repository_id)
        before = self._eligible_map()
        with self.store.transaction() as db:
            db.execute('UPDATE repositories SET index_enabled=?,implementation_enabled=?,updated_at=? WHERE id=?', (index_enabled, implementation_enabled, self.clock(), repository_id))
        self._notify(before)
        return self.get_repository(repository_id)

    def require_access(self, repository_id, capability='contents_read'):
        repository = self._one('repositories', repository_id)
        capabilities = {'contents_read': ('contents', 'read'), 'contents_write': ('contents', 'write'), 'pull_requests_write': ('pull_requests', 'write'), 'pull_requests_read': ('pull_requests', 'read'), 'metadata_read': ('metadata', 'read')}
        if capability not in capabilities:
            raise GitHubError('validation')
        name, level = capabilities[capability]
        with self.store.read() as db:
            grants = db.execute("SELECT ri.* FROM repository_installations ri JOIN github_installations i ON i.id=ri.installation_id JOIN github_connections c ON c.id=i.connection_id WHERE ri.repository_id=? AND i.status='active' AND c.status='connected' ORDER BY i.id", (repository_id,)).fetchall()
        if not grants:
            raise GitHubError('repository_unavailable')
        if level == 'write' and not repository['implementation_enabled']:
            raise GitHubError('capability_missing')
        for grant in grants:
            perms = json.loads(grant['permissions'])
            if perms.get(name) in (('read', 'write', 'admin') if level == 'read' else ('write', 'admin')):
                return {'repository_id': repository_id, 'installation_id': grant['installation_id'], 'permissions': perms}
        raise GitHubError('capability_missing')

    def mint_installation_token(self, repository_id, permissions=None):
        requested = {'contents': 'read'} if permissions is None else validate_permissions(permissions)
        if not requested or any(k not in ('contents', 'metadata', 'pull_requests') or v not in ('read', 'write') or (k == 'metadata' and v != 'read') for k, v in requested.items()):
            raise GitHubError('validation')
        for k, v in requested.items():
            self.require_access(repository_id, k + '_' + v)
        with self.store.read() as db:
            grants = [{'repository_id': repository_id, 'installation_id': r['installation_id'], 'permissions': json.loads(r['permissions'])} for r in db.execute("SELECT ri.* FROM repository_installations ri JOIN github_installations i ON i.id=ri.installation_id JOIN github_connections c ON c.id=i.connection_id WHERE ri.repository_id=? AND i.status='active' AND c.status='connected' ORDER BY i.id", (repository_id,))]
        # Choose one grant that covers the whole request, rather than combining installations.
        grant = None
        for candidate in grants:
            if all(candidate['permissions'].get(k) in (('read', 'write', 'admin') if v == 'read' else ('write', 'admin')) for k, v in requested.items()):
                grant = candidate
                break
        if grant is None:
            raise GitHubError('capability_missing')
        installation = self._one('github_installations', grant['installation_id'])
        connection = self._one('github_connections', installation['connection_id'])
        try:
            result = self.client.mint_token(self._app_token(connection), installation['id'], repository_ids=[self._one('repositories', repository_id)['github_id']], permissions=requested)
        except GitHubError as exc:
            if exc.code == 'not_found':
                self._invalidate_installation(connection['id'], installation['id'], 'revoked')
            elif exc.code == 'auth':
                before = self._eligible_map()
                with self.store.transaction() as db:
                    db.execute("UPDATE github_connections SET status='reconnect_required',last_error_code='auth',updated_at=? WHERE id=? AND status!='disconnected'", (self.clock(), connection['id']))
                    self._access_states(db)
                self._notify(before)
            raise
        # Refuse a token if access changed while the upstream request was in flight.
        for k, v in requested.items():
            self.require_access(repository_id, k + '_' + v)
        with self.store.read() as db:
            current_grant = db.execute('SELECT permissions FROM repository_installations WHERE repository_id=? AND installation_id=?', (repository_id, installation['id'])).fetchone()
        current_permissions = json.loads(current_grant[0]) if current_grant else {}
        if not all(current_permissions.get(k) in (('read', 'write', 'admin') if v == 'read' else ('write', 'admin')) for k, v in requested.items()) or self._one('github_installations', installation['id'])['status'] != 'active' or self._one('github_connections', connection['id'])['status'] != 'connected' or self._one('github_connections', connection['id'])['private_key_ref'] != connection['private_key_ref']:
            raise GitHubError('repository_unavailable')
        return result

    def purge_repository(self, repository_id):
        self.get_repository(repository_id)
        before = self._eligible_map()
        with self.store.transaction() as db:
            db.execute('UPDATE repositories SET index_enabled=0,updated_at=? WHERE id=?', (self.clock(), repository_id))
            payload = {'repository_id': repository_id}
            existing = db.execute("SELECT id,state FROM jobs WHERE kind='github.purge' AND payload=? AND state IN ('queued','leased') ORDER BY created_at,id LIMIT 1", (encoded(payload),)).fetchone()
            if existing:
                result = dict(job_id=existing['id'], state=existing['state'])
            else:
                job = self.jobs.enqueue_in_transaction(db, 'github.purge', payload, idempotency_key='github.purge:' + repository_id + ':' + new_id('purge'))
                result = dict(job_id=job.id, state=job.state)
        self._notify(before)
        return result

    def run_purge(self, job, context):
        repository_id = job.payload['repository_id']
        if self.is_eligible(repository_id):
            raise GitHubError('conflict')
        completed = (job.checkpoint or {}).get('completed', [])
        for name, callback in self.purge_hooks.items():
            if name in completed:
                continue
            if context is not None and context.cancel_requested:
                return {'state': 'cancelled'}
            callback(repository_id)
            completed.append(name)
            if context is not None:
                context.checkpoint({'completed': completed})
        return {'repository_id': repository_id, 'state': 'completed', 'hooks': completed}

    def status_summary(self):
        connections = self.connections()
        with self.store.read() as db:
            installations = db.execute("SELECT count(*) FROM github_installations WHERE status='active'").fetchone()[0]
            repository_count = db.execute("SELECT count(*) FROM repositories WHERE access_state='accessible'").fetchone()[0]
        retries = [c['next_retry_at'] for c in connections if c['next_retry_at'] is not None]
        refreshes = [c['last_refresh_at'] for c in connections if c['last_refresh_at'] is not None]
        return {'status': 'connected' if any(c['status'] == 'connected' for c in connections) else connections[0]['status'] if connections else 'disconnected',
                'installations': installations, 'repository_count': repository_count,
                'last_refresh_at': max(refreshes, default=None), 'next_retry_at': min(retries, default=None),
                'error_code': next((c['last_error_code'] for c in connections if c['last_error_code']), None)}
