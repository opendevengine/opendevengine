"""M1 schema integrity and atomic service/job composition."""
from pathlib import Path
from tempfile import TemporaryDirectory
import sqlite3
import unittest
from unittest.mock import patch

from opendevengine.platform import Platform
from opendevengine.platform import migrations
from opendevengine.platform.errors import IdempotencyConflict


class ServiceFoundationTests(unittest.TestCase):
    def test_m0_database_upgrades_without_losing_jobs_or_mutating_migration1(self):
        first = migrations.MIGRATIONS[0]
        with TemporaryDirectory() as directory:
            path = Path(directory)
            with patch.object(migrations, 'MIGRATIONS', (first,)), patch.object(migrations, 'LATEST_VERSION', 1):
                platform = Platform.open(path)
                original = platform.jobs.enqueue('test.persist', {'item': 7})
                platform.close()
            platform = Platform.open(path)
            try:
                self.assertEqual(platform.store.schema_version(), migrations.LATEST_VERSION)
                self.assertEqual(platform.jobs.get(original.id).payload, {'item': 7})
                with platform.store.read() as conn:
                    self.assertEqual(conn.execute('SELECT checksum FROM schema_migrations WHERE version=1').fetchone()[0], first.checksum)
                    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    self.assertTrue({'repositories', 'discovery_runs', 'provider_profiles', 'provider_settings'} <= names)
                    self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            finally:
                platform.close()

    def test_service_record_and_job_commit_or_rollback_together(self):
        platform = Platform.open(None)
        self.addCleanup(platform.close)
        with self.assertRaises(RuntimeError):
            with platform.store.transaction() as conn:
                platform.jobs.enqueue_in_transaction(conn, 'test.atomic', {'run': 'x'}, idempotency_key='one')
                conn.execute("INSERT INTO provider_settings VALUES ('local_only','true',1)")
                raise RuntimeError('simulate transaction failure')
        self.assertEqual(sum(platform.jobs.counts().values()), 0)
        with platform.store.read() as conn:
            self.assertIsNone(conn.execute("SELECT value FROM provider_settings WHERE key='local_only'").fetchone())
        with platform.store.transaction() as conn:
            job = platform.jobs.enqueue_in_transaction(conn, 'test.atomic', {'run': 'x'}, idempotency_key='one')
            self.assertEqual(job.id, platform.jobs.enqueue_in_transaction(conn, 'test.atomic', {'run': 'x'}, idempotency_key='one').id)
            with self.assertRaises(IdempotencyConflict):
                platform.jobs.enqueue_in_transaction(conn, 'test.atomic', {'run': 'y'}, idempotency_key='one')
        with platform.store.read() as conn:
            with self.assertRaises(ValueError):
                platform.jobs.enqueue_in_transaction(conn, 'test.atomic', {})

    def test_foreign_keys_and_unique_active_discovery_enforced(self):
        platform = Platform.open(None)
        self.addCleanup(platform.close)
        with platform.store.transaction() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO model_role_assignments VALUES ('ask','missing',0)")
            conn.execute("INSERT INTO secret_refs VALUES ('sec_app','test','github_app_private_key','app',0,NULL)")
            conn.execute("INSERT INTO github_connections (id,app_id,slug,private_key_ref,status,created_at,updated_at) VALUES ('con_app','1','test-app','sec_app','connected',0,0)")
            insert = "INSERT INTO discovery_runs(id,connection_id,generation,state,created_at,updated_at) VALUES (?, 'con_app', ?, 'queued',0,0)"
            conn.execute(insert, ('run_first', 'generation1'))
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(insert, ('run_second', 'generation2'))
            conn.execute("UPDATE discovery_runs SET state='completed' WHERE id='run_first'")
            conn.execute(insert, ('run_second', 'generation2'))
