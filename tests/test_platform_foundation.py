import ast
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from opendevengine.platform import *
from opendevengine.platform import migrations


class Clock:
    value = 10000
    def __call__(self):
        return self.value
    def advance(self, ms):
        self.value += ms


class MemorySecretBackend:
    name = 'test_memory'
    def __init__(self):
        self.values = {}
        self.fail_put = self.fail_delete = False
    def status(self):
        return BackendStatus(self.name, True, None)
    def put(self, ref_id, value):
        if self.fail_put:
            raise RuntimeError(value.decode())
        self.values[ref_id] = value
    def get(self, ref_id):
        return self.values[ref_id]
    def delete(self, ref_id):
        if self.fail_delete:
            raise RuntimeError('private backend detail')
        self.values.pop(ref_id, None)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.platform = Platform.open(None, clock=self.clock)
        self.addCleanup(self.platform.close)
        self.q = self.platform.jobs

    def test_idempotency_priority_and_concurrent_claim(self):
        job = self.q.enqueue('test.echo', {'b': 2, 'a': 1}, idempotency_key='one')
        self.assertEqual(job, self.q.enqueue('test.echo', {'a': 1, 'b': 2}, priority=10, idempotency_key='one'))
        for options in ({'payload': {}}, {'effect': 'external'}, {'kind': 'test.other'}):
            args = dict(kind='test.echo', payload={'a': 1, 'b': 2}, idempotency_key='one')
            args.update(options)
            with self.assertRaises(IdempotencyConflict):
                self.q.enqueue(**args)
        urgent = self.q.enqueue('test.echo', {}, priority=10)
        self.assertEqual(urgent.id, self.q.claim('first').job.id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(self.q.claim, ['second', 'third']))
        self.assertEqual(1, sum(c is not None for c in claims))
        self.assertEqual(job.id, next(c.job.id for c in claims if c))

    def test_recovery_token_fencing_and_exhaustion(self):
        job = self.q.enqueue('test.safe', {}, max_attempts=2)
        lease = self.q.claim('worker', lease_ms=1000)
        self.clock.advance(1000)
        self.assertEqual([], self.q.reconcile_expired())
        self.clock.advance(1)
        # Expiry alone leaves ownership intact.
        self.q.heartbeat(lease, checkpoint={'cursor': 1}, lease_ms=1000)
        self.clock.advance(1001)
        self.assertEqual([job.id], self.q.reconcile_expired())
        for operation in (self.q.heartbeat, self.q.complete, self.q.acknowledge_cancel):
            with self.assertRaises(LeaseLost):
                operation(lease)
        lease2 = self.q.claim('new', lease_ms=1000)
        self.assertNotIn(lease2.token, repr(lease2))
        self.assertFalse(hasattr(lease2.job, 'lease_token'))
        self.assertEqual({'cursor': 1}, lease2.job.checkpoint)
        self.clock.advance(1001)
        self.q.reconcile_expired()
        self.assertEqual('failed', self.q.get(job.id).state)
        self.assertIsNone(self.q.claim('worker'))

    def test_external_unknown_exception_never_retried_and_exhausted_resolution(self):
        job = self.q.enqueue('test.external', {}, effect='external', max_attempts=1)
        def crash(job, context):
            raise RuntimeError('private source and credential')
        worker = Worker(self.q, {'test.external': crash}, worker_id='worker')
        self.assertTrue(worker.run_once())
        self.assertFalse(worker.run_once())
        unknown = self.q.get(job.id)
        self.assertEqual(('needs_reconciliation', 'internal', 'RuntimeError'),
                         (unknown.state, unknown.last_error_code, unknown.last_error_message))
        with self.assertRaises(InvalidTransition):
            self.q.resolve_reconciliation(job.id, outcome='requeue')
        self.assertEqual(unknown, self.q.get(job.id))
        with self.assertRaises(InvalidTransition):
            self.q.cancel(job.id)
        self.assertEqual('succeeded', self.q.resolve_reconciliation(job.id, outcome='succeeded').state)
        safe = self.q.enqueue('test.external', {})
        worker.run_once()
        self.assertEqual('failed', self.q.get(safe.id).state)

    def test_external_expiry_and_manual_requeue(self):
        job = self.q.enqueue('test.external', {}, effect='external')
        lease = self.q.claim('worker', lease_ms=1000)
        self.q.cancel(job.id)
        self.clock.advance(1001)
        self.assertIsNone(self.q.claim('worker'))
        self.assertEqual('needs_reconciliation', self.q.get(job.id).state)
        with self.assertRaises(LeaseLost):
            self.q.mark_unknown_outcome(lease, code='internal')
        before = self.q.get(job.id)
        with self.assertRaises(InvalidTransition):
            self.q.resolve_reconciliation(job.id, outcome='requeue')
        self.assertEqual(before, self.q.get(job.id))
        self.assertEqual('failed', self.q.resolve_reconciliation(job.id, outcome='failed').state)

    def test_cancellation_backoff_and_result(self):
        job = self.q.enqueue('test.echo', {})
        self.assertEqual('cancelled', self.q.cancel(job.id).state)
        self.assertEqual(self.q.get(job.id), self.q.cancel(job.id))
        job = self.q.enqueue('test.echo', {}, max_attempts=3)
        for delay in (2000, 4000):
            lease = self.q.claim('worker')
            failed = self.q.fail(lease, code='rate_limited', retryable=True, message='x' * 501)
            self.assertEqual(self.clock() + delay, failed.run_after)
            self.assertEqual(500, len(failed.last_error_message))
            self.assertIsNone(self.q.claim('worker'))
            self.clock.advance(delay)
        lease = self.q.claim('worker')
        with self.assertRaises(InvalidTransition):
            self.q.acknowledge_cancel(lease)
        self.q.cancel(job.id)
        self.assertTrue(self.q.heartbeat(lease).cancel_requested)
        self.assertEqual('cancelled', self.q.acknowledge_cancel(lease).state)
        self.q.enqueue('test.echo', {})
        lease = self.q.claim('worker')
        self.assertEqual({'ok': True}, self.q.complete(lease, result={'ok': True}).result)

    def test_worker_classifications_and_checkpoints(self):
        for error, state in ((RetryableError, 'queued'), (PermanentError, 'failed'), (UnknownOutcomeError, 'needs_reconciliation')):
            with self.subTest(error=error):
                job = self.q.enqueue('test.worker', {}, effect='external')
                def handler(job, ctx):
                    ctx.checkpoint({'offset': 2})
                    raise error('secret')
                Worker(self.q, {'test.worker': handler}, worker_id='worker').run_once()
                self.assertEqual(state, self.q.get(job.id).state)
                self.assertEqual({'offset': 2}, self.q.get(job.id).checkpoint)
        def cancel_handler(job, ctx):
            self.q.cancel(job.id)
            self.assertTrue(ctx.cancel_requested)
            raise CancelledError()
        job = self.q.enqueue('test.cancel', {})
        Worker(self.q, {'test.cancel': cancel_handler}, worker_id='worker').run_once()
        self.assertEqual('cancelled', self.q.get(job.id).state)

    def test_cancelled_safe_expiry_and_exhausted_failure(self):
        job = self.q.enqueue('test.safe', {})
        self.q.claim('worker', lease_ms=1000)
        self.q.cancel(job.id)
        self.clock.advance(1001)
        self.q.reconcile_expired()
        self.assertEqual('cancelled', self.q.get(job.id).state)
        job = self.q.enqueue('test.safe', {}, max_attempts=1)
        lease = self.q.claim('worker')
        failed = self.q.fail(lease, code='timeout', retryable=True)
        self.assertEqual('failed', failed.state)
        self.assertEqual(self.clock(), failed.finished_at)
        with self.assertRaises(LeaseLost):
            self.q.fail(lease, code='timeout', retryable=False)
        job = self.q.enqueue('test.external', {}, effect='external')
        lease = self.q.claim('worker')
        self.q.mark_unknown_outcome(lease, code='unknown_outcome')
        self.assertEqual('failed', self.q.resolve_reconciliation(job.id, outcome='failed').state)

    def test_validation_and_empty_kind_filter(self):
        for payload in ([], {'v': float('nan')}, {'v': object()}, {'v': 'x' * 17000}):
            with self.assertRaises(ValueError):
                self.q.enqueue('test.echo', payload)
        with self.assertRaises(ValueError):
            self.q.enqueue('Bad', {})
        self.q.enqueue('test.echo', {})
        self.assertIsNone(self.q.claim('worker', kinds=[]))
        with self.assertRaises(ValueError):
            self.q.claim('worker', lease_ms=999)
        with self.assertRaises(NotFound):
            self.q.get('missing')


class StorageTests(unittest.TestCase):
    def test_restart_backup_permissions_and_two_connections(self):
        with TemporaryDirectory() as directory:
            path, clock = Path(directory), Clock()
            platform = Platform.open(path, clock=clock)
            job = platform.jobs.enqueue('test.echo', {})
            lease = platform.jobs.claim('old', lease_ms=1000)
            platform.store.backup_to(path / 'snapshot.sqlite3')
            self.assertGreater(platform.store.size_bytes(), 0)
            platform.close()
            platform = Platform.open(path, clock=clock)
            other = Platform.open(path, clock=clock)
            try:
                self.assertEqual(migrations.LATEST_VERSION, platform.store.schema_version())
                self.assertEqual('leased', platform.jobs.get(job.id).state)
                self.assertIsNone(other.jobs.claim('new'))
                clock.advance(1001)
                new = other.jobs.claim('new')
                with self.assertRaises(LeaseLost):
                    platform.jobs.complete(lease)
                other.jobs.complete(new)
                self.assertEqual('succeeded', platform.jobs.get(job.id).state)
                self.assertEqual(0o600, (path / DB_FILENAME).stat().st_mode & 0o777)
                with sqlite3.connect(path / 'snapshot.sqlite3') as conn:
                    self.assertEqual('leased', conn.execute('SELECT state FROM jobs').fetchone()[0])
            finally:
                platform.close()
                other.close()

    def test_migration_checks_rollback_and_preupgrade_backup(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            store = Store.open(path)
            with store.transaction() as conn:
                conn.execute("INSERT INTO secret_refs VALUES ('sec_test','test','github_token','label',1,NULL)")
            store.close()
            next_version = migrations.LATEST_VERSION + 1
            bad = migrations.Migration(next_version, 'test_failure', ('CREATE TABLE temporary_test (id INTEGER)', 'INVALID SQL'))
            with patch.object(migrations, 'MIGRATIONS', migrations.MIGRATIONS + (bad,)), patch.object(migrations, 'LATEST_VERSION', next_version):
                with self.assertRaises(MigrationFailed):
                    Store.open(path)
            self.assertTrue((path / f'backups/opendevengine.pre-v{next_version}.sqlite3').is_file())
            store = Store.open(path)
            with store.read() as conn:
                self.assertEqual(1, conn.execute('SELECT count(*) FROM secret_refs').fetchone()[0])
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='temporary_test'").fetchone())
                self.assertEqual(migrations.LATEST_VERSION, conn.execute('PRAGMA user_version').fetchone()[0])
            with store.transaction() as conn:
                conn.execute("UPDATE schema_migrations SET checksum='wrong'")
            store.close()
            with self.assertRaises(MigrationChecksumMismatch):
                Store.open(path)
            with sqlite3.connect(path / DB_FILENAME) as conn:
                conn.execute("INSERT INTO schema_migrations VALUES (99,'future','x',0)")
            with self.assertRaises(SchemaTooNew):
                Store.open(path)

    def test_busy_storage_and_transaction_rollback(self):
        with TemporaryDirectory() as directory:
            store = Store.open(Path(directory))
            other = Store.open(Path(directory))
            try:
                with other.read() as conn:
                    conn.execute('PRAGMA busy_timeout=1')
                with store.transaction():
                    with self.assertRaises(StorageBusy):
                        with other.transaction():
                            pass
                with self.assertRaises(RuntimeError):
                    with store.transaction() as conn:
                        conn.execute("INSERT INTO secret_refs VALUES ('sec_test','test','github_token','label',1,NULL)")
                        raise RuntimeError('rollback')
                with store.read() as conn:
                    self.assertEqual(0, conn.execute('SELECT count(*) FROM secret_refs').fetchone()[0])
            finally:
                store.close()
                other.close()

    def test_successful_upgrade_and_fail_closed_open(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            Store.open(path).close()
            next_version = migrations.LATEST_VERSION + 1
            extra = migrations.Migration(next_version, 'test_upgrade', ('CREATE TABLE upgrade_test (id INTEGER)',))
            with patch.object(migrations, 'MIGRATIONS', migrations.MIGRATIONS + (extra,)), patch.object(migrations, 'LATEST_VERSION', next_version):
                store = Store.open(path)
                self.assertEqual(next_version, store.schema_version())
                store.close()
            with self.assertRaises(PlatformError):
                Store.open(path / DB_FILENAME)


class SecretTests(unittest.TestCase):
    def test_unavailable_has_no_rows(self):
        platform = Platform.open(None)
        self.addCleanup(platform.close)
        with self.assertRaises(SecretStoreUnavailable):
            platform.secrets.create('github_token', 'label', 'credential')
        with platform.store.read() as conn:
            self.assertEqual(0, conn.execute('SELECT count(*) FROM secret_refs').fetchone()[0])

    def test_rollback_missing_revoke_and_no_secret_on_disk(self):
        with TemporaryDirectory() as directory:
            backend = MemorySecretBackend()
            platform = Platform.open(Path(directory), secret_backend=backend, clock=lambda: 0)
            secret = 'UNIQUE_PRIVATE_TEST_CREDENTIAL_93937'
            backend.fail_put = True
            with self.assertRaises(SecretStoreUnavailable) as caught:
                platform.secrets.create('github_token', 'label', secret)
            self.assertNotIn(secret, str(caught.exception))
            with platform.store.read() as conn:
                self.assertEqual(0, conn.execute('SELECT count(*) FROM secret_refs').fetchone()[0])
            backend.fail_put = False
            ref = platform.secrets.create('github_token', 'label', secret)
            value = platform.secrets.resolve(ref.id)
            self.assertEqual(secret, value.reveal())
            self.assertNotIn(secret, repr(value) + str(value) + repr(ref))
            with self.assertRaises(TypeError):
                json.dumps(value)
            backend.values.clear()
            with self.assertRaises(SecretMissing):
                platform.secrets.resolve(ref.id)
            backend.fail_delete = True
            revoked = platform.secrets.revoke(ref.id)
            self.assertEqual(0, revoked.revoked_at)
            self.assertEqual(revoked, platform.secrets.revoke(ref.id))
            with self.assertRaises(SecretRevoked):
                platform.secrets.resolve(ref.id)
            platform.store.backup_to(Path(directory) / 'backup.sqlite3')
            platform.close()
            for path in Path(directory).iterdir():
                if path.is_file():
                    self.assertNotIn(secret.encode(), path.read_bytes())


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.fixtures = json.loads(Path('tests/fixtures/api-contract.json').read_text())
        # Schema numbers reflect the current registry; archived M0 shape remains fixed.
        for fixture in self.fixtures['workspace'].values():
            if fixture['storage']['schema_version'] is not None:
                fixture['storage']['schema_version'] = migrations.LATEST_VERSION
        for fixture in self.fixtures['health'].values():
            for key in ('schema_version', 'expected_schema_version'):
                if fixture['components']['storage'][key] is not None:
                    fixture['components']['storage'][key] = migrations.LATEST_VERSION
        self.now = 1794583800000
        self.index = SimpleNamespace(snapshots={str(i): SimpleNamespace(demo=True, limited=False) for i in range(4)})

    def test_exact_demo_and_disabled_fixtures(self):
        platform = Platform.open(None)
        self.addCleanup(platform.close)
        for name, value in [('no_platform', None), ('demo_memory', platform)]:
            self.assertEqual(self.fixtures['workspace'][name], build_workspace_summary(self.index, None, value, now=self.now))
            self.assertEqual(self.fixtures['health'][name], build_health(self.index, None, value, now=self.now))

    def test_local_persistence_and_readonly_degraded_health(self):
        with TemporaryDirectory() as directory:
            clock = Clock()
            platform = Platform.open(Path(directory), clock=clock)
            self.addCleanup(platform.close)
            local = SimpleNamespace(snapshots={str(i): SimpleNamespace(demo=False, limited=i == 0) for i in range(3)})
            model = SimpleNamespace(model='example-model:latest')
            self.assertEqual(self.fixtures['workspace']['local_with_model_persistent'], build_workspace_summary(local, model, platform, now=self.now))
            job = platform.jobs.enqueue('test.external', {'private': 'do not expose'}, effect='external')
            lease = platform.jobs.claim('worker', lease_ms=1000)
            clock.advance(1001)
            before = platform.jobs.get(job.id)
            health = build_health(local, model, platform, now=clock())
            self.assertEqual('degraded', health['status'])
            self.assertEqual(1, health['components']['jobs']['expired_leases'])
            self.assertEqual(0, health['components']['jobs']['workers_running'])
            self.assertEqual(before, platform.jobs.get(job.id))
            self.assertNotIn('do not expose', json.dumps(health))
            platform.jobs.mark_unknown_outcome(lease, code='internal', message='private exception')
            health = build_health(local, model, platform, now=clock())
            self.assertEqual(1, health['components']['jobs']['counts']['needs_reconciliation'])
            self.assertEqual({'job_id', 'kind', 'code', 'at'}, set(health['recent_failures'][0]))
            self.assertNotIn('private exception', json.dumps(health))

    def test_python310_syntax(self):
        for path in Path('opendevengine/platform').glob('*.py'):
            ast.parse(path.read_text(), feature_version=(3, 10))
