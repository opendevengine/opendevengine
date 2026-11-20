"""Per-session retention controls using durable real SQLite fixtures."""
import json
from pathlib import Path
from test_sessions_fixture import SessionCase
from opendevengine.sessions import TERMINAL
from opendevengine.sessions.errors import SessionError
from opendevengine.session_api import ExecutionAPI


class SessionPurgeTests(SessionCase):
    def launch(self, key='purge'):
        _, v = self.ready_plan()
        return self.sessions.launch(v['id'], v['content_hash'], key)['session_id']

    def finish(self, sid, state='completed'):
        with self.store.transaction() as db:
            db.execute('UPDATE sessions SET state=?,finished_at=123 WHERE id=?', (state, sid))

    def publication(self, sid, state):
        with self.store.transaction() as db:
            db.execute('''INSERT INTO publications
                (id,session_id,repository_id,idempotency_key,request_hash,intent_version,policy,
                 expected_diff_hash,expected_base_sha,base_branch,head_branch,title,body,
                 commit_name,commit_email,commit_date,commit_message,state,
                 pull_request_number,pull_request_url,created_at,updated_at)
                 VALUES ('pub',?,'gh:1','pub-key','hash',1,'manual_draft','hash','base','main','head',
                 'PRIVATE','PRIVATE','Private','private@example.com','date','PRIVATE',?,
                 42,'https://github.com/owner/repo1/pull/42',1,1)''', (sid,state))

    def test_active_states_refused_and_terminal_states_allowed(self):
        sid = self.launch()
        for state in ('queued','provisioning','running','validating','waiting_for_user','stopping'):
            self.finish(sid,state)
            with self.assertRaises(SessionError) as error: self.sessions.purge_session(sid)
            self.assertEqual(error.exception.code,'session_active')
        for state in TERMINAL:
            self.finish(sid,state)
            self.assertEqual(self.sessions.purge_session(sid)['session_id'],sid)

    def test_publication_blocks_until_terminal_and_identity_survives(self):
        sid = self.launch(); self.finish(sid); self.publication(sid,'queued')
        for state in ('queued','preparing','creating_objects','creating_branch','creating_pr','reconciling'):
            with self.store.transaction() as db: db.execute('UPDATE publications SET state=?',(state,))
            result=ExecutionAPI(sessions=self.sessions).dispatch('POST',f'/api/sessions/{sid}/purge',body={})
            self.assertEqual(result.status,409); self.assertEqual(result.body['code'],'publication_in_progress')
        with self.store.transaction() as db: db.execute("UPDATE publications SET state='published'")
        self.sessions.purge_session(sid)
        self.assertEqual(self.sessions.get(sid)['publication']['pull_request_number'],42)
        with self.store.read() as db:
            row=db.execute('SELECT * FROM publications').fetchone()
            self.assertEqual(row['body'],''); self.assertEqual(row['pull_request_number'],42)

    def test_content_disk_isolation_restart_idempotency_and_api(self):
        sid=self.launch(); self.finish(sid)
        plan_id=self.sessions.get(sid)['plan_id']
        other=self.launch('other'); self.finish(other)
        for ident in (sid,other):
            with self.store.transaction() as db:
                db.execute("INSERT INTO session_artifacts VALUES (?,?, 'context','hash',7,?,1)", ('a-'+ident,ident,b'PRIVATE'))
                db.execute("UPDATE sessions SET instructions_json=? WHERE id=?",(json.dumps({'secret':'PRIVATE'}),ident))
                db.execute("INSERT INTO session_messages VALUES (?,?,'client','PRIVATE',NULL,'delivered',NULL,1,1)",('m-'+ident,ident))
                db.execute("INSERT INTO session_questions VALUES (?,?,'PRIVATE',NULL,'cancelled',1,1)",('q-'+ident,ident))
                db.execute("INSERT INTO validation_runs VALUES (?,?,1,?,'observed','failed',1,1,?,?,1,1)",
                           ('v-'+ident,ident,json.dumps(['PRIVATE']),'a-'+ident,json.dumps({'cwd':'PRIVATE'})))
                db.execute("UPDATE session_events SET payload_json=? WHERE session_id=?",(json.dumps({'text':'PRIVATE'}),ident))
            path=self.data/'sessions'/ident; path.mkdir(parents=True); (path/'log').write_text('PRIVATE')
        outside=Path(self.tmp.name)/'outside'; outside.write_text('KEEP')
        (self.data/'sessions'/sid/'link').symlink_to(outside)
        api=ExecutionAPI(sessions=self.sessions)
        result=api.dispatch('POST',f'/api/sessions/{sid}/purge',body={})
        self.assertEqual(result.status,200); self.assertEqual(set(result.body),{'session_id','purged_at'})
        self.assertFalse((self.data/'sessions'/sid).exists()); self.assertEqual(outside.read_text(),'KEEP')
        self.assertTrue((self.data/'sessions'/other/'log').exists())
        self.assertEqual(self.make_service().purge_session(sid),result.body)
        from opendevengine.platform import Platform
        from opendevengine.sessions import SessionService
        reopened=Platform.open(self.data); self.addCleanup(reopened.close)
        restarted=SessionService(reopened,None,None,None,None,None)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.purge_session(sid),result.body)
        view=restarted.get(sid); self.assertTrue(view['purged']); self.assertNotIn('instructions',view)
        self.assertEqual(view['plan_id'],plan_id)
        self.assertEqual(api.dispatch('GET',f'/api/sessions/{sid}').body['plan_id'],plan_id)
        self.assertTrue(next(s for s in self.sessions.list_sessions()['items'] if s['id']==sid)['purged'])
        for endpoint in ('events','messages','diff','validations','artifacts'):
            response=api.dispatch('GET',f'/api/sessions/{sid}/{endpoint}')
            self.assertEqual(response.status,409); self.assertEqual(response.body['code'],'purged')
        with self.store.read() as db:
            for table in ('sessions','session_events','session_messages','session_questions','session_repositories','session_artifacts','validation_runs'):
                key='id' if table=='sessions' else 'session_id'
                rows=db.execute(f'SELECT * FROM {table} WHERE {key}=?',(sid,)).fetchall()
                self.assertNotIn('PRIVATE',str([tuple(r) for r in rows]),table)
            self.assertIsNone(db.execute('SELECT content FROM session_artifacts WHERE session_id=?',(sid,)).fetchone()[0])
            self.assertEqual(db.execute('SELECT content FROM session_artifacts WHERE session_id=?',(other,)).fetchone()[0],b'PRIVATE')
        with self.assertRaises(ValueError):api.dispatch('POST',f'/api/sessions/{sid}/purge',body={'force':True})

    def test_cleanup_interruption_keeps_tombstone_and_retry_removes_files(self):
        from unittest.mock import patch
        sid=self.launch(); self.finish(sid)
        path=self.data/'sessions'/sid; path.mkdir(parents=True); (path/'log').write_text('PRIVATE')
        with patch('opendevengine.sessions.purge.remove_session_files',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):self.sessions.purge_session(sid)
        first=self.sessions.get(sid)['purged_at']
        self.assertTrue(path.exists())
        self.assertEqual(self.sessions.purge_session(sid)['purged_at'],first)
        self.assertFalse(path.exists())
        with self.assertRaises(SessionError) as error:
            self.sessions.send_message(sid,'new retained content','new-message')
        self.assertEqual(error.exception.code,'purged')

    def test_symlinked_session_directory_does_not_remove_target(self):
        sid=self.launch(); self.finish(sid)
        outside=Path(self.tmp.name)/'outside-dir'; outside.mkdir(); (outside/'log').write_text('KEEP')
        root=self.data/'sessions'; root.mkdir(); (root/sid).symlink_to(outside,target_is_directory=True)
        self.sessions.purge_session(sid)
        self.assertFalse((root/sid).is_symlink()); self.assertEqual((outside/'log').read_text(),'KEEP')

    def test_publication_insert_from_stale_snapshot_is_rejected_after_purge(self):
        import sqlite3
        sid=self.launch(); self.finish(sid)
        self.sessions.purge_session(sid)
        with self.assertRaises(sqlite3.IntegrityError):
            self.publication(sid,'queued')
        with self.store.read() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM publications WHERE session_id=?',(sid,)).fetchone()[0],0)
        self.assertTrue(self.sessions.get(sid)['purged'])
