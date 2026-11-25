"""Schema contracts preserve source purge and immutable execution intent."""
from unittest import TestCase
import sqlite3
import test_ask_services as fixtures


class ExecutionSchemaTests(TestCase):
    def setUp(self):
        self.f=fixtures.AskTests(methodName='runTest');self.f.setUp();self.addCleanup(self.f.tearDown)
        f=self.f;plan=f.plans.create(f.conv,f.content)
        self.plan=plan
        with f.store.transaction() as c:
            c.execute("INSERT INTO execution_profiles(id,name,active_version_id,created_at,updated_at) VALUES('ep','Personal CLI','epv',1,1)")
            c.execute("INSERT INTO execution_profile_versions(id,profile_id,version,adapter,auth_mode,config_json,created_at) VALUES('epv','ep',1,'claude_code','cli_login','{}',1)")
            c.execute("INSERT INTO sessions(id,plan_id,plan_version_id,content_hash,profile_version_id,idempotency_key,launch_hash,attempt,state,created_at,updated_at) VALUES('ses',?,?,?,'epv','unique-launch','intent',1,'queued',1,1)",(plan['id'],plan['version']['id'],plan['version']['content_hash']))

    def test_source_purge_is_not_prevented_by_historical_session_snapshot_reference(self):
        f=self.f;snapshot=f.content['repositories'][0]['snapshot_id']
        with f.store.transaction() as c:
            c.execute("INSERT INTO session_repositories(session_id,repository_id,role,snapshot_id,base_sha) VALUES('ses',?,'write',?,?)",(f.repo,snapshot,f.revision))
        f.index.purge_repository(f.repo)
        with f.store.read() as c:
            row=c.execute("SELECT snapshot_id,base_sha FROM session_repositories WHERE session_id='ses'").fetchone()
            self.assertIsNone(row[0]);self.assertEqual(row[1],f.revision)
            self.assertEqual(c.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_session_event_sequences_and_launch_keys_are_unique(self):
        f=self.f
        with f.store.transaction() as c:
            c.execute("INSERT INTO session_events VALUES('ses',1,'session.state','{}',1)")
        with self.assertRaises(sqlite3.IntegrityError):
            with f.store.transaction() as c:c.execute("INSERT INTO session_events VALUES('ses',1,'session.state','{}',2)")
        with self.assertRaises(sqlite3.IntegrityError):
            with f.store.transaction() as c:
                c.execute("INSERT INTO sessions(id,plan_id,plan_version_id,content_hash,profile_version_id,idempotency_key,launch_hash,attempt,state,created_at,updated_at) SELECT 'duplicate',plan_id,plan_version_id,content_hash,profile_version_id,idempotency_key,launch_hash,2,'queued',2,2 FROM sessions WHERE id='ses'")
        with self.assertRaises(sqlite3.IntegrityError):
            with f.store.transaction() as c:c.execute("INSERT INTO session_artifacts VALUES('bad','ses','patch','hash',4,?,1)",(b'not-four-bytes',))
