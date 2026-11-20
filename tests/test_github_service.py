"""Behavioral service checks against actual SQLite and queue; no GitHub network."""
import base64
import json
import threading
from datetime import datetime, timezone
import unittest
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import serialization, hashes
from opendevengine.platform import Platform
from opendevengine.platform.secrets import SecretService, BackendStatus
from opendevengine.platform.worker import Worker
from opendevengine.github import GitHubService, GitHubError, Response


class TestVault:
    name = 'test-only'
    def __init__(self): self.values = {}
    def status(self): return BackendStatus(self.name, True, None)
    def put(self, key, value): self.values[key] = value
    def get(self, key): return self.values[key]
    def delete(self, key): self.values.pop(key, None)


def repo(i, name=None):
    return {'id': i, 'owner': {'login': 'owner' if i % 2 else 'team'}, 'name': name or 'repo' + str(i),
            'default_branch': 'main', 'description': 'synthetic', 'private': True,
            'clone_url': 'https://evil.invalid/credentials', 'topics': ['test']}


class FakeGitHub:
    def __init__(self, test):
        self.test = test
        self.repos = {'11': [repo(i) for i in range(1, 251)]}
        self.suspended, self.revoked = set(), set()
        self.failure, self.on_page = None, None
        self.token_scope = {}
        self.last_request = None
        self.calls = []

    def __call__(self, request):
        service = self.test.service
        self.test.assertFalse(service.store.conn.in_transaction)
        # A different thread can take the Store lock during every transport call.
        acquired = threading.Event()
        def acquire():
            with service.store.read(): acquired.set()
        thread = threading.Thread(target=acquire)
        thread.start(); thread.join(1)
        self.test.assertTrue(acquired.is_set(), 'network call held Store lock')
        self.last_request = request
        self.calls.append((request.method, request.path))
        path = request.path
        if path == '/app':
            token = request.headers['Authorization'].split()[1]
            h, payload, signature = token.split('.')
            decode = lambda v: base64.urlsafe_b64decode(v + '=' * (-len(v) % 4))
            self.test.key.public_key().verify(decode(signature), (h + '.' + payload).encode(), padding.PKCS1v15(), hashes.SHA256())
            claims = json.loads(decode(payload))
            self.test.assertEqual(claims['exp'] - claims['iat'], 600)
            return Response(200, {}, {'id': int(claims['iss']), 'slug': 'synthetic-app'})
        if path.startswith('/app/installations/'):
            iid = path.split('/')[3]
            if path.endswith('/access_tokens'):
                token = 'installation-secret-' + iid
                self.token_scope[token] = iid
                return Response(201, {}, {'token': token, 'expires_at': datetime.fromtimestamp((self.test.now + 3600000) / 1000, timezone.utc).isoformat()})
            if iid in self.revoked: return Response(404, {}, {'message': 'SECRET_ERROR'})
            return Response(200, {}, {'id': int(iid), 'app_id': 2 if iid == '33' else 1, 'account': {'login': 'owner', 'type': 'Organization'},
                                     'permissions': {'contents': 'write', 'pull_requests': 'write', 'metadata': 'read'},
                                     'suspended_at': '2026-01-01' if iid in self.suspended else None})
        if path.startswith('/installation/repositories?'):
            page = int(parse_qs(urlsplit(path).query)['page'][0])
            if self.on_page: self.on_page(page)
            if self.failure and self.failure[0] == page:
                return self.failure[1]
            iid = self.token_scope[request.headers['Authorization'].split()[1]]
            return Response(200, {}, {'repositories': self.repos[iid][(page - 1) * 100:page * 100]})
        raise AssertionError('Unexpected request')


class GitHubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pem = cls.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()

    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.platform = Platform.open(self.temp.name); self.addCleanup(self.platform.close)
        self.now = 1700000000000
        self.vault = TestVault()
        self.secrets = SecretService(self.platform.store, self.vault, clock=lambda: self.now)
        self.remote = FakeGitHub(self)
        self.service = GitHubService(self.platform.store, self.platform.jobs, self.secrets, transport=self.remote, clock=lambda: self.now)
        self.connection = self.service.configure(1, 'synthetic-app', self.pem, index_new=True)
        self.attach('11')
        self.worker = Worker(self.platform.jobs, {'github.refresh': self.service.run_refresh, 'github.purge': self.service.run_purge}, worker_id='test')

    def state(self, connection_id=None):
        result = self.service.begin_connect(connection_id or self.connection['id'])
        return parse_qs(urlsplit(result['install_url']).query)['state'][0]

    def attach(self, iid):
        return self.service.finish_connect(self.state(), iid)

    def refresh(self):
        result = self.service.refresh(self.connection['id'])
        self.assertTrue(self.worker.run_once())
        return self.service._one('discovery_runs', result['run_id'])

    def error(self, code, function, *args, **kwargs):
        with self.assertRaises(GitHubError) as caught: function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_250_pagination_duplicate_refresh_catalog_cursor_and_privacy(self):
        first = self.service.refresh(self.connection['id'])
        self.assertEqual(first, self.service.refresh(self.connection['id']))
        self.worker.run_once()
        self.assertEqual(self.service.list_repositories()['total'], 250)
        pages, cursor = [], None
        while True:
            result = self.service.list_repositories(cursor=cursor, limit=37)
            pages.extend(result['items']); cursor = result['next_cursor']
            if cursor is None: break
        self.assertEqual(len({r['id'] for r in pages}), 250)
        self.assertTrue(all(r['index_enabled'] and not r['implementation_enabled'] for r in pages))
        self.assertEqual(self.refresh()['state'], 'completed')
        self.assertEqual(self.service.list_repositories()['total'], 250)
        self.assertEqual(self.service.get_repository('gh:1')['clone_url'], 'https://github.com/owner/repo1.git')
        with self.platform.store.read() as db: dump = '\n'.join(db.iterdump())
        output = json.dumps([self.service.connections(), self.service.status_summary(), pages])
        for text in (dump, output):
            self.assertNotIn('BEGIN PRIVATE KEY', text)
            self.assertNotIn('installation-secret', text)
            self.assertNotIn('SECRET_ERROR', text)
        self.assertNotIn('private_key_ref', output)
        self.error('validation', self.service.list_repositories, limit=True)
        self.error('validation', self.service.list_repositories, cursor='!!!')

    def test_failed_middle_page_preserves_grants_and_rename_selection(self):
        self.refresh()
        self.service.update_selection('gh:1', index_enabled=False, implementation_enabled=True)
        self.remote.repos['11'][0] = repo(1, 'renamed')
        self.remote.failure = (2, Response(500, {}, {'message': 'SECRET_ERROR'}))
        result = self.refresh()
        self.assertEqual(result['state'], 'waiting')
        self.assertTrue(self.service.is_eligible('gh:250'))
        value = self.service.get_repository('gh:1')
        self.assertEqual(value['name'], 'renamed')
        self.assertFalse(value['index_enabled']); self.assertTrue(value['implementation_enabled'])
        self.remote.failure = None
        self.remote.repos['11'] = [repo(1, 'renamed')]
        self.refresh()
        self.assertFalse(self.service.is_eligible('gh:250'))
        self.error('repository_unavailable', self.service.require_access, 'gh:250')

    def test_callback_replay_expiry_mismatch_and_pending_approval(self):
        state = self.state()
        self.service.finish_connect(state, '11')
        self.error('setup_state_invalid', self.service.finish_connect, state, '11')
        state = self.state(); self.now += 600001
        self.error('setup_state_invalid', self.service.finish_connect, state, '11')
        original = self.remote
        def mismatch(request):
            if request.path == '/app/installations/22':
                return Response(200, {}, {'id': 22, 'app_id': 9})
            return original(request)
        self.service.client.transport = mismatch
        state = self.state()
        self.error('installation_mismatch', self.service.finish_connect, state, '22')
        self.error('setup_state_invalid', self.service.finish_connect, state, '22')
        result = self.service.finish_connect(self.state(), None, setup_action='request')
        self.assertEqual(result['setup_status'], 'pending_approval')
        self.assertEqual(result['status'], 'connected')
        with self.platform.store.read() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM github_installations').fetchone()[0], 1)

    def test_multiple_grants_suspension_revocation_and_permissions(self):
        self.remote.repos['22'] = [repo(1)]
        self.attach('22'); self.refresh()
        self.remote.suspended.add('11'); self.refresh()
        self.assertTrue(self.service.is_eligible('gh:1'))
        self.assertFalse(self.service.is_eligible('gh:2'))
        self.assertEqual(self.service.require_access('gh:1')['installation_id'], '22')
        self.error('capability_missing', self.service.require_access, 'gh:1', 'contents_write')
        self.service.update_selection('gh:1', index_enabled=False, implementation_enabled=True)
        self.service.mint_installation_token('gh:1', {'contents': 'write', 'pull_requests': 'write'})
        self.assertEqual(self.remote.last_request.json['repository_ids'], [1])
        self.assertEqual(self.remote.last_request.json['permissions'], {'contents': 'write', 'pull_requests': 'write'})
        self.remote.revoked.add('22'); self.refresh()
        self.error('repository_unavailable', self.service.require_access, 'gh:1')

    def test_disconnect_racing_page_fences_upsert_and_signals(self):
        self.refresh()
        changes = []; self.service.register_eligibility_listener(lambda *args: changes.append(args))
        self.remote.repos['11'] = [repo(999)]
        self.remote.on_page = lambda page: self.service.disconnect(self.connection['id'])
        self.refresh()
        self.error('not_found', self.service.get_repository, 'gh:999')
        self.assertFalse(self.service.is_eligible('gh:1'))
        self.assertIn(('gh:1', False), changes)
        self.error('connection_unavailable', self.service.refresh, self.connection['id'])

    def test_rate_limit_checkpoint_continuation(self):
        self.remote.failure = (2, Response(403, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': str(self.now // 1000 + 120)}, {}))
        result = self.refresh()
        self.assertEqual(result['state'], 'waiting')
        self.assertEqual(json.loads(result['checkpoint'])['page'], 2)
        self.assertEqual(self.service.status_summary()['next_retry_at'], self.now + 120000)
        self.assertEqual(self.platform.jobs.get(result['job_id']).run_after, self.now + 120000)
        self.remote.failure = None
        # Use explicit job handler because the queue uses its independent real clock.
        continuation = self.platform.jobs.get(result['job_id'])
        self.assertEqual(self.service.run_refresh(continuation, None)['state'], 'completed')
        self.assertEqual(self.service.list_repositories()['total'], 250)
        self.assertEqual(self.service.status_summary()['next_retry_at'], None)

    def test_rotation_failure_cleanup_purge_and_periodic(self):
        self.refresh()
        old = next(iter(self.vault.values))
        self.service.configure(1, 'synthetic-app', self.pem)
        self.assertNotIn(old, self.vault.values)
        self.assertEqual(len(self.vault.values), 1)
        self.assertEqual(self.service.schedule_due(), [])
        self.now += 3600001
        self.assertEqual(len(self.service.schedule_due()), 1)
        self.worker.run_once()
        purged = []
        self.service.register_purge_hook('synthetic', purged.append)
        self.service.purge_repository('gh:1')
        self.assertFalse(self.service.is_eligible('gh:1'))
        self.worker.run_once()
        self.assertEqual(purged, ['gh:1'])

    def test_auth_marks_reconnect_and_hides_grants(self):
        self.refresh()
        self.remote.failure = (1, Response(401, {}, {'message': 'SECRET_ERROR'}))
        result = self.refresh()
        self.assertEqual(result['error_code'], 'auth')
        self.assertFalse(self.service.is_eligible('gh:1'))
        self.assertEqual(self.service.connections()[0]['status'], 'reconnect_required')

    def test_callback_race_consumes_only_once(self):
        state = self.state()
        outcomes = []
        def finish():
            try: self.service.finish_connect(state, '11'); outcomes.append('ok')
            except GitHubError as exc: outcomes.append(exc.code)
        threads = [threading.Thread(target=finish) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertCountEqual(outcomes, ['ok', 'setup_state_invalid'])


    def test_multiple_connections_disconnect_preserves_other_grant(self):
        self.refresh()
        second = self.service.configure(2, 'synthetic-app', self.pem, index_new=False)
        self.remote.repos['33'] = [repo(1)]
        self.service.finish_connect(self.state(second['id']), '33')
        self.service.refresh(second['id']); self.worker.run_once()
        self.service.disconnect(self.connection['id'])
        self.assertTrue(self.service.is_eligible('gh:1'))
        self.assertEqual(self.service.require_access('gh:1')['installation_id'], '33')
        self.assertFalse(self.service.is_eligible('gh:2'))

    def test_suspended_callback_and_new_pending_approval_create_no_grant(self):
        second = self.service.configure(2, 'synthetic-app', self.pem)
        result = self.service.finish_connect(self.state(second['id']), None, setup_action='request')
        self.assertEqual(result['status'], 'pending_approval')
        self.remote.suspended.add('33')
        state = self.state(second['id'])
        self.error('installation_suspended', self.service.finish_connect, state, '33')
        self.error('setup_state_invalid', self.service.finish_connect, state, '33')
        self.error('not_found', self.service._one, 'github_installations', '33')

    def test_purge_is_idempotent_while_pending_and_selection_not_reenabled(self):
        self.refresh()
        result = self.service.purge_repository('gh:1')
        self.assertEqual(result, self.service.purge_repository('gh:1'))
        self.worker.run_once()
        self.refresh()
        self.assertFalse(self.service.is_eligible('gh:1'))

    def test_persistence_required_before_secret_or_network(self):
        memory = Platform.open(None)
        self.addCleanup(memory.close)
        service = GitHubService(memory.store, memory.jobs, SecretService(memory.store, TestVault()), transport=lambda req: self.fail('unexpected network'))
        self.error('persistence_required', service.configure, 1, 'synthetic-app', self.pem)

    def test_bad_rsa_keys_and_identity_do_not_create_credentials(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        for key in (rsa.generate_private_key(public_exponent=65537, key_size=1024), ec.generate_private_key(ec.SECP256R1())):
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
            self.error('validation', self.service.configure, 1, 'synthetic-app', pem)
        encrypted = self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(b'synthetic')).decode()
        self.error('validation', self.service.configure, 1, 'synthetic-app', encrypted)
        self.error('app_identity_mismatch', self.service.configure, 1, 'wrong-app', self.pem)
        self.assertEqual(len(self.vault.values), 1)

    def test_authoritative_permissions_downgrade_survives_failed_page(self):
        self.refresh()
        original = self.remote
        def downgrade(request):
            response = original(request)
            if request.path == '/app/installations/11':
                response.json['permissions'] = {'metadata': 'read'}
            return response
        self.service.client.transport = downgrade
        self.remote.failure = (1, Response(500, {}, {}))
        self.refresh()
        self.error('capability_missing', self.service.require_access, 'gh:1')
        self.assertEqual(self.service.require_access('gh:1', 'metadata_read')['installation_id'], '11')

    def test_revoked_secret_becomes_reconnect_without_transport(self):
        self.refresh()
        ref = self.service._one('github_connections', self.connection['id'])['private_key_ref']
        self.secrets.revoke(ref)
        self.assertEqual(self.refresh()['error_code'], 'auth')
        self.assertFalse(self.service.is_eligible('gh:1'))


    def test_transaction_failure_cleans_new_vault_secret(self):
        import sqlite3
        with self.platform.store.transaction() as db:
            db.execute("CREATE TRIGGER fail_connection BEFORE INSERT ON github_connections BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.service.configure(2, 'synthetic-app', self.pem)
        self.assertEqual(len(self.vault.values), 1)
        self.assertEqual(len(self.service.connections()), 1)

    def test_installation_revoked_during_page_cannot_restore_access(self):
        self.refresh()
        self.remote.repos['11'] = [repo(999)]
        def revoke(page):
            with self.platform.store.transaction() as db:
                db.execute("UPDATE github_installations SET status='revoked' WHERE id='11'")
        self.remote.on_page = revoke
        self.refresh()
        self.error('not_found', self.service.get_repository, 'gh:999')
        self.error('repository_unavailable', self.service.require_access, 'gh:1')

    def test_continuation_recovers_from_reopened_store(self):
        self.remote.failure = (2, Response(429, {'Retry-After': '1'}, {}))
        result = self.refresh()
        self.assertEqual(result['state'], 'waiting')
        reopened = Platform.open(self.temp.name)
        self.addCleanup(reopened.close)
        self.service = GitHubService(reopened.store, reopened.jobs, SecretService(reopened.store, self.vault), transport=self.remote, clock=lambda: self.now)
        self.remote.failure = None
        worker = Worker(reopened.jobs, {'github.refresh': self.service.run_refresh}, worker_id='reopened')
        self.assertTrue(worker.run_once())
        self.assertEqual(self.service._one('discovery_runs', result['id'])['state'], 'completed')
        self.assertEqual(self.service.list_repositories()['total'], 250)

    def test_configurable_polling_minimum(self):
        self.error('validation', self.service.update_refresh_interval, self.connection['id'], 899999)
        self.error('validation', self.service.update_refresh_interval, self.connection['id'], True)
        self.assertEqual(self.service.update_refresh_interval(self.connection['id'], 900000)['refresh_interval_ms'], 900000)


    def test_transient_retries_are_bounded_and_do_not_erase_grants(self):
        self.refresh()
        self.remote.failure = (1, Response(500, {}, {}))
        result = self.refresh()
        for count in range(1, 4):
            self.assertEqual(result['state'], 'waiting')
            self.assertEqual(result['continuation'], count)
            self.assertTrue(self.service.is_eligible('gh:250'))
            job = self.platform.jobs.get(result['job_id'])
            self.service.run_refresh(job, None)
            result = self.service._one('discovery_runs', result['id'])
        self.assertEqual(result['state'], 'failed')


    def test_transfer_and_catalog_filters_preserve_explicit_preferences(self):
        self.refresh()
        self.assertEqual(self.service.list_repositories(owner='team')['total'], 125)
        self.service.update_selection('gh:1', index_enabled=False, implementation_enabled=True)
        transferred = repo(1, 'moved')
        transferred['owner']['login'] = 'another-owner'
        self.remote.repos['11'][0] = transferred
        self.refresh()
        value = self.service.get_repository('gh:1')
        self.assertEqual(value['full_name'], 'another-owner/moved')
        self.assertFalse(value['index_enabled'])
        self.assertTrue(value['implementation_enabled'])
        self.assertEqual(self.service.list_repositories(query='another-owner', index_enabled=False)['total'], 1)

    def test_verified_callback_downgrade_updates_existing_grant_immediately(self):
        self.refresh()
        self.service.update_selection('gh:1', index_enabled=True, implementation_enabled=True)
        original = self.remote
        def downgrade(request):
            response = original(request)
            if request.path == '/app/installations/11':
                response.json['permissions'] = {'contents': 'read', 'metadata': 'read'}
            return response
        self.service.client.transport = downgrade
        self.attach('11')
        self.error('capability_missing', self.service.require_access, 'gh:1', 'contents_write')
        self.assertTrue(self.service.is_eligible('gh:1'))

    def test_disconnect_during_callback_prevents_late_attachment(self):
        original = self.remote
        def disconnect(request):
            response = original(request)
            if request.path == '/app/installations/22':
                self.service.disconnect(self.connection['id'])
                self.service.begin_connect(self.connection['id'])
            return response
        self.service.client.transport = disconnect
        self.error('connection_unavailable', self.service.finish_connect, self.state(), '22')
        self.error('not_found', self.service._one, 'github_installations', '22')


    def test_callback_detection_of_suspension_removes_existing_grants(self):
        self.refresh()
        self.remote.suspended.add('11')
        self.error('installation_suspended', self.service.finish_connect, self.state(), '11')
        self.error('repository_unavailable', self.service.require_access, 'gh:1')
        self.assertFalse(self.service.is_eligible('gh:1'))

    def test_token_request_revocation_immediately_blocks_future_access(self):
        self.refresh()
        original = self.remote
        def revoked(request):
            if request.path == '/app/installations/11/access_tokens':
                return Response(404, {}, {})
            return original(request)
        self.service.client.transport = revoked
        self.error('not_found', self.service.mint_installation_token, 'gh:1')
        self.error('repository_unavailable', self.service.require_access, 'gh:1')
        self.assertFalse(self.service.is_eligible('gh:1'))

    def _review_r1_callback_during_discovery(self, phase):
        self.refresh()
        self.service.update_selection('gh:1', index_enabled=True, implementation_enabled=True)
        self.service.require_access('gh:1', 'contents_write')
        state = self.state()
        fixed_now = self.now
        original = self.remote
        triggered = False
        downgraded = False
        self.remote.repos['11'][0] = repo(1, 'late-page')

        def race(request):
            nonlocal triggered, downgraded
            response = original(request)
            metadata_request = request.path == '/app/installations/11'
            if metadata_request and downgraded:
                response.json['permissions'] = {'contents': 'read', 'metadata': 'read'}
            at_boundary = metadata_request if phase == 'metadata' else request.path.startswith('/installation/repositories?')
            if at_boundary and not triggered:
                triggered = downgraded = True
                # Reentrant verified callback, while the older response is still in flight.
                # The clock deliberately never advances: timestamp-only fences must fail.
                self.service.finish_connect(state, '11')
            return response

        self.service.client.transport = race
        old_run = self.refresh()
        self.assertTrue(triggered)
        self.assertEqual(self.now, fixed_now)
        self.error('capability_missing', self.service.require_access, 'gh:1', 'contents_write')
        self.assertEqual(json.loads(self.service._one('github_installations', '11')['permissions'])['contents'], 'read')
        self.assertEqual(old_run['state'], 'cancelled')
        self.assertEqual(self.service.get_repository('gh:1')['name'], 'repo1')
        with self.platform.store.read() as db:
            grant = db.execute("SELECT permissions FROM repository_installations WHERE repository_id='gh:1' AND installation_id='11'").fetchone()
            active = db.execute("SELECT * FROM discovery_runs WHERE state IN ('queued','running','waiting')").fetchall()
        self.assertEqual(json.loads(grant[0])['contents'], 'read')
        self.assertEqual(len(active), 1)
        self.assertNotEqual(active[0]['id'], old_run['id'])
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.service._one('discovery_runs', active[0]['id'])['state'], 'completed')
        self.error('capability_missing', self.service.require_access, 'gh:1', 'contents_write')
        self.assertEqual(self.service.get_repository('gh:1')['name'], 'late-page')

    def test_review_r1_same_millisecond_callback_fences_repository_page(self):
        self._review_r1_callback_during_discovery('page')

    def test_review_r1_same_millisecond_callback_fences_installation_metadata(self):
        self._review_r1_callback_during_discovery('metadata')

    def test_review_r1_discovery_token_404_revokes_only_affected_installation(self):
        self.remote.repos['22'] = [repo(1)]
        self.attach('22')
        self.refresh()
        self.assertTrue(self.service.is_eligible('gh:2'))
        changes = []
        self.service.register_eligibility_listener(lambda *args: changes.append(args))
        original = self.remote
        validated = False

        def revoked_token(request):
            nonlocal validated
            if request.path == '/app/installations/11/access_tokens':
                self.assertTrue(validated, 'Reproduction requires successful installation validation first')
                return Response(404, {}, {'message': 'SECRET_ERROR'})
            response = original(request)
            if request.path == '/app/installations/11':
                self.assertEqual(response.status, 200)
                validated = True
            return response

        self.service.client.transport = revoked_token
        result = self.refresh()
        self.assertTrue(validated)
        self.assertFalse(self.service.is_eligible('gh:2'))
        self.error('repository_unavailable', self.service.require_access, 'gh:2')
        self.assertEqual(self.service.get_repository('gh:2')['access_state'], 'inaccessible')
        self.assertEqual(self.service._one('github_installations', '11')['status'], 'revoked')
        self.assertIn(('gh:2', False), changes)
        self.assertNotIn(('gh:1', False), changes)
        self.assertTrue(self.service.is_eligible('gh:1'))
        self.assertEqual(self.service.require_access('gh:1')['installation_id'], '22')
        self.assertEqual(self.service._one('github_installations', '22')['status'], 'active')
        self.assertEqual(result['state'], 'completed')
        with self.platform.store.read() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM repository_installations WHERE installation_id='11'").fetchone()[0], 0)

    def test_review_r1_callback_replaces_waiting_continuation_durably(self):
        self.refresh()
        self.remote.failure = (2, Response(429, {'Retry-After': '120'}, {}))
        old_run = self.refresh()
        stale_job = self.platform.jobs.get(old_run['job_id'])
        self.assertEqual(old_run['state'], 'waiting')
        original = self.remote

        def read_only(request):
            response = original(request)
            if request.path == '/app/installations/11':
                response.json['permissions'] = {'contents': 'read', 'metadata': 'read'}
            return response

        self.service.client.transport = read_only
        self.attach('11')
        self.assertEqual(self.platform.jobs.get(stale_job.id).state, 'cancelled')
        self.assertEqual(self.service._one('discovery_runs', old_run['id'])['state'], 'cancelled')
        self.assertEqual(self.service.run_refresh(stale_job, None), {'state': 'cancelled'})
        with self.platform.store.read() as db:
            replacements = db.execute("SELECT * FROM discovery_runs WHERE state='queued'").fetchall()
        self.assertEqual(len(replacements), 1)
        replacement = dict(replacements[0])
        self.assertNotEqual(replacement['generation'], old_run['generation'])
        self.assertEqual(json.loads(replacement['checkpoint']), {})
        self.remote.failure = None
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.service._one('discovery_runs', replacement['id'])['state'], 'completed')
        self.assertEqual(self.service.require_access('gh:1')['permissions']['contents'], 'read')
