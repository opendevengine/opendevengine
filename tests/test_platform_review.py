"""Regression evidence for independent review F1–F7."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from opendevengine.platform import *
from opendevengine.platform import migrations, storage_files
from test_platform_foundation import Clock, MemorySecretBackend


class ReviewQueueTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.p = Platform.open(None, clock=self.clock)
        self.addCleanup(self.p.close)
        self.q = self.p.jobs

    def test_cancel_beats_retry_for_both_effects(self):
        for effect in ('safe', 'external'):
            job = self.q.enqueue('test.cancel', {}, effect=effect)
            lease = self.q.claim('worker')
            self.q.cancel(job.id)
            cancelled = self.q.fail(lease, code='transient', retryable=True)
            self.assertEqual('cancelled', cancelled.state)
            self.assertEqual(self.clock(), cancelled.finished_at)
            self.clock.advance(100000)
            self.assertIsNone(self.q.claim('worker'))

    def test_pending_cancel_blocks_uncertain_requeue_but_known_outcome_resolves(self):
        for outcome in ('succeeded', 'failed'):
            job = self.q.enqueue('test.external', {}, effect='external')
            lease = self.q.claim('worker')
            self.q.cancel(job.id)
            unknown = self.q.mark_unknown_outcome(lease, code='internal')
            with self.assertRaises(InvalidTransition):
                self.q.resolve_reconciliation(job.id, outcome='requeue')
            self.assertEqual(unknown, self.q.get(job.id))
            self.assertTrue(self.q.resolve_reconciliation(job.id, outcome=outcome).cancel_requested)

    def test_return_preserves_result_despite_late_cancel(self):
        for effect in ('safe', 'external'):
            job = self.q.enqueue('test.return', {}, effect=effect)
            def finished(job, ctx):
                self.q.cancel(job.id)
                return {'operation': 'finished'}
            Worker(self.q, {'test.return': finished}, worker_id='w').run_once()
            done = self.q.get(job.id)
            self.assertEqual('succeeded', done.state)
            self.assertEqual({'operation': 'finished'}, done.result)

    def test_explicit_cancel_and_uncertain_cancel_are_distinct(self):
        for error, state in ((CancelledError, 'cancelled'), (UnknownOutcomeError, 'needs_reconciliation')):
            job = self.q.enqueue('test.stop', {}, effect='external')
            def stopped(job, ctx):
                self.q.cancel(job.id)
                raise error()
            Worker(self.q, {'test.stop': stopped}, worker_id='w').run_once()
            self.assertEqual(state, self.q.get(job.id).state)

    def test_invalid_handler_results_classified_immediately(self):
        for effect in ('safe', 'external'):
            for result in ([], {'v': float('nan')}, {'v': float('inf')}, {'v': 'x' * 17000}, {'v': object()}):
                with self.subTest(effect=effect, result_type=type(result)):
                    job = self.q.enqueue('test.result', {}, effect=effect)
                    Worker(self.q, {'test.result': lambda job, ctx: result}, worker_id='w').run_once()
                    done = self.q.get(job.id)
                    self.assertEqual('failed' if effect == 'safe' else 'needs_reconciliation', done.state)
                    self.assertEqual('internal', done.last_error_code)
                    self.assertIsNone(done.lease_owner)
                    self.assertIsNone(self.q.claim('w'))

    def test_recent_failures_excludes_recovered_active_and_cancelled_jobs(self):
        job = self.q.enqueue('test.retry', {})
        lease = self.q.claim('w')
        self.q.fail(lease, code='transient', retryable=True)
        self.assertEqual([], self.q.recent_failures())
        self.clock.advance(2000)
        lease = self.q.claim('w')
        self.q.heartbeat(lease)
        self.assertEqual([], self.q.recent_failures())
        self.q.complete(lease)
        self.assertIsNone(self.q.get(job.id).last_error_code)
        job = self.q.enqueue('test.external', {}, effect='external')
        lease = self.q.claim('w')
        self.q.mark_unknown_outcome(lease, code='unknown_outcome')
        self.assertEqual(1, len(self.q.recent_failures()))
        self.q.resolve_reconciliation(job.id, outcome='succeeded')
        self.assertEqual([], self.q.recent_failures())
        job = self.q.enqueue('test.cancel', {})
        self.q.claim('w', lease_ms=1000)
        self.q.cancel(job.id)
        self.clock.advance(1001)
        self.q.reconcile_expired()
        self.assertIsNone(self.q.get(job.id).last_error_code)
        self.assertEqual([], self.q.recent_failures())
        job = self.q.enqueue('test.failure', {})
        lease = self.q.claim('w')
        self.q.fail(lease, code='invalid_input', retryable=False)
        failure = self.q.recent_failures()[0]
        self.clock.advance(1000)
        self.q.cancel(job.id)
        self.assertEqual(failure, self.q.recent_failures()[0])


class ReviewStorageTests(unittest.TestCase):
    def simultaneous_open(self, path):
        barrier = Barrier(2)
        def start(_):
            barrier.wait(timeout=5)
            return Store.open(path)
        with ThreadPoolExecutor(max_workers=2) as pool:
            stores = list(pool.map(start, range(2)))
        try:
            self.assertEqual([migrations.LATEST_VERSION] * 2, [s.schema_version() for s in stores])
        finally:
            for store in stores:
                store.close()

    def test_simultaneous_fresh_and_upgrade_startup(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            self.simultaneous_open(path)
            next_version = migrations.LATEST_VERSION + 1
            extra = migrations.Migration(next_version, 'review_upgrade', ('CREATE TABLE review_upgrade (id INTEGER)',))
            with patch.object(migrations, 'MIGRATIONS', migrations.MIGRATIONS + (extra,)), patch.object(migrations, 'LATEST_VERSION', next_version):
                with patch.object(migrations, 'private_backup', wraps=storage_files.private_backup) as snapshots:
                    self.simultaneous_open(path)
                    self.assertEqual(1, snapshots.call_count)
            backup = path / f'backups/opendevengine.pre-v{next_version}.sqlite3'
            conn = sqlite3.connect(backup)
            try:
                self.assertEqual(migrations.LATEST_VERSION, conn.execute('PRAGMA user_version').fetchone()[0])
                self.assertEqual('ok', conn.execute('PRAGMA integrity_check').fetchone()[0])
            finally:
                conn.close()
            self.assertEqual(0o600, backup.stat().st_mode & 0o777)

    def test_backup_private_before_write_and_connection_closed(self):
        with TemporaryDirectory() as directory:
            store = Store.open(None)
            self.addCleanup(store.close)
            real_connect = sqlite3.connect
            opened = []
            def inspect_connect(path):
                self.assertEqual(0o600, Path(path).stat().st_mode & 0o777)
                conn = real_connect(path)
                opened.append(conn)
                return conn
            with patch.object(storage_files.sqlite3, 'connect', side_effect=inspect_connect):
                store.backup_to(Path(directory) / 'backup.sqlite3')
            for conn in opened:
                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute('SELECT 1')
            self.assertEqual(0o600, (Path(directory) / 'backup.sqlite3').stat().st_mode & 0o777)


class ReviewSecretTests(unittest.TestCase):
    def test_partial_backend_put_is_cleaned_without_exception_context(self):
        backend = MemorySecretBackend()
        platform = Platform.open(None, secret_backend=backend)
        self.addCleanup(platform.close)
        def partial_put(ref, value):
            backend.values[ref] = value
            raise RuntimeError(value.decode())
        backend.put = partial_put
        with self.assertRaises(SecretStoreUnavailable) as caught:
            platform.secrets.create('github_token', 'label', 'PRIVATE_SENTINEL')
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual({}, backend.values)
        with platform.store.read() as conn:
            self.assertEqual(0, conn.execute('SELECT count(*) FROM secret_refs').fetchone()[0])

    def test_commit_failure_cleans_backend_and_sanitizes_error(self):
        backend = MemorySecretBackend()
        platform = Platform.open(None, secret_backend=backend)
        self.addCleanup(platform.close)
        @contextmanager
        def failing_commit():
            with platform.store.read() as conn:
                conn.execute('BEGIN IMMEDIATE')
                try:
                    yield conn
                    raise RuntimeError('PRIVATE_COMMIT_SENTINEL')
                finally:
                    conn.execute('ROLLBACK')
        with patch.object(platform.store, 'transaction', failing_commit):
            with self.assertRaises(SecretStoreUnavailable) as caught:
                platform.secrets.create('github_token', 'label', 'PRIVATE_SENTINEL')
        self.assertEqual({}, backend.values)
        self.assertIsNone(caught.exception.__context__)
        with platform.store.read() as conn:
            self.assertEqual(0, conn.execute('SELECT count(*) FROM secret_refs').fetchone()[0])

    def test_resolve_errors_do_not_keep_backend_context(self):
        backend = MemorySecretBackend()
        platform = Platform.open(None, secret_backend=backend)
        self.addCleanup(platform.close)
        ref = platform.secrets.create('github_token', 'label', 'PRIVATE_SENTINEL')
        for error, expected in ((KeyError, SecretMissing), (RuntimeError, SecretStoreUnavailable)):
            def failing_get(ref):
                raise error('PRIVATE_SENTINEL')
            backend.get = failing_get
            with self.assertRaises(expected) as caught:
                platform.secrets.resolve(ref.id)
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn('PRIVATE_SENTINEL', str(caught.exception))

    def test_backend_status_and_failed_cleanup_errors_are_sanitized(self):
        backend = MemorySecretBackend()
        platform = Platform.open(None, secret_backend=backend)
        self.addCleanup(platform.close)
        backend.fail_put = backend.fail_delete = True
        with self.assertRaises(SecretStoreUnavailable) as caught:
            platform.secrets.create('github_token', 'label', 'PRIVATE_SENTINEL')
        self.assertIsNone(caught.exception.__context__)
        def bad_status():
            raise RuntimeError('PRIVATE_SENTINEL')
        backend.status = bad_status
        for call in (platform.secrets.status,
                     lambda: platform.secrets.create('github_token', 'label', 'PRIVATE_SENTINEL')):
            with self.assertRaises(SecretStoreUnavailable) as caught:
                call()
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn('PRIVATE_SENTINEL', str(caught.exception))
