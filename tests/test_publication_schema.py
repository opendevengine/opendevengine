"""Migration 11 preserves one immutable publication intent and append-only events."""
import sqlite3
from unittest import TestCase

import test_ask_services as fixtures


class PublicationSchemaTests(TestCase):
    def setUp(self):
        self.f = fixtures.AskTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        plan = self.f.plans.create(self.f.conv, self.f.content)
        self.plan = plan
        with self.f.store.transaction() as db:
            db.execute("INSERT INTO execution_profiles(id,name,active_version_id,created_at,updated_at) "
                       "VALUES('ep','Personal CLI','epv',1,1)")
            db.execute("INSERT INTO execution_profile_versions VALUES('epv','ep',1,'claude_code','cli_login','{}',1)")
            db.execute("INSERT INTO sessions(id,plan_id,plan_version_id,content_hash,profile_version_id,idempotency_key,"
                       "launch_hash,attempt,state,created_at,updated_at) VALUES('ses',?,?,?,'epv','launch','intent',1,"
                       "'completed',1,1)", (plan['id'], plan['version']['id'], plan['version']['content_hash']))
            db.execute("INSERT INTO session_artifacts VALUES('checkpoint','ses','checkpoint','cp-hash',2,?,1)", (b'{}',))
            db.execute("INSERT INTO session_artifacts VALUES('patch','ses','patch','diff-hash',2,?,1)", (b'{}',))

    def insert_publication(self, ident='pub', key='publish-key'):
        with self.f.store.transaction() as db:
            db.execute("INSERT INTO publications(id,session_id,repository_id,idempotency_key,request_hash,intent_version,"
                       "policy,expected_diff_hash,checkpoint_artifact_id,patch_artifact_id,expected_base_sha,base_branch,"
                       "head_branch,title,body,commit_name,commit_email,commit_date,commit_message,state,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,1,'manual_draft','diff-hash','checkpoint','patch',?,'main','opendevengine/ses-a',"
                       "'Change behavior','Body','OpenDevEngine','operator@example.test','2026-11-15T00:00:00Z',"
                       "'OpenDevEngine session ses','queued',1,1)",
                       (ident, 'ses', self.f.repo, key, 'request-hash', self.f.revision))

    def test_session_and_idempotency_keys_allow_only_one_immutable_intent(self):
        self.insert_publication()
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_publication('pub-two', 'different-key')
        with self.f.store.read() as db:
            row = db.execute('SELECT session_id,state FROM publications').fetchone()
            self.assertEqual((row['session_id'], row['state']), ('ses', 'queued'))

    def test_events_are_ordered_and_artifact_deletion_tombstones_references(self):
        self.insert_publication()
        with self.f.store.transaction() as db:
            db.execute("INSERT INTO publication_events VALUES('pub',1,'publication.state','{}',1)")
            db.execute("DELETE FROM session_artifacts WHERE id='checkpoint'")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.f.store.transaction() as db:
                db.execute("INSERT INTO publication_events VALUES('pub',1,'publication.state','{}',2)")
        with self.f.store.read() as db:
            row = db.execute("SELECT checkpoint_artifact_id,patch_artifact_id FROM publications WHERE id='pub'").fetchone()
            self.assertIsNone(row[0])
            self.assertEqual(row[1], 'patch')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
