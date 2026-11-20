"""Shutdown, crash/restart recovery, leadership and stale-lease reconciliation."""
import os
import subprocess
import threading
import time
import unittest

from opendevengine.platform import Platform
from opendevengine.sessions import SessionError
from opendevengine.sessions.processes import contain_recorded, start_identity
from test_sessions_fixture import FakeRunner, SessionCase


def orphan_like(argv):
    """A harness from a dead runtime is reaped by init; model that with a reaper thread."""
    proc = subprocess.Popen(argv, start_new_session=True)
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


def gone(proc, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc.poll() is not None:
            return True
        time.sleep(0.02)
    return False


class RecoveryTests(SessionCase):
    def crashed_session(self):
        """Durable state exactly as a dead runtime process leaves it, with real resources."""
        _, version = self.ready_plan()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-crash')['session_id']
        lease = self.jobs.claim('runtime:deadbeef:0', kinds=['session.run'], lease_ms=600000)
        harness = orphan_like(['/bin/sleep', '300'])
        self.world.children.append(harness)
        identity = start_identity(harness.pid)
        runner = FakeRunner(self.sessions.instance_id(), self.world)
        handle = runner.provision(sid, {'calc.py': b'x'}, image='python:3.12-slim', limits={})
        with self.store.transaction() as db:
            db.execute("UPDATE sessions SET state='running',phase='agent',started_at=1 WHERE id=?", (sid,))
            db.execute("INSERT INTO session_runners (session_id,instance_id,container_id,harness_pid,harness_start,state,updated_at) "
                       "VALUES (?,?,?,?,?,'running',1)", (sid, self.sessions.instance_id(), handle.container_id, harness.pid, identity))
        return sid, lease, harness

    def test_startup_without_session_history_never_waits_on_docker(self):
        from opendevengine.sessions import SessionService
        calls=[]
        def factory(iid):
            calls.append(iid)
            return FakeRunner(iid, self.world)
        def fresh():
            svc = SessionService(self.platform, self.index, self.github, self.providers, self.plans, self.profiles,
                                 runner_factory=factory, adapter_factory=lambda: None, poll_interval=0.05)
            self.addCleanup(svc.close)
            return svc
        self.sessions.close()
        with self.store.transaction() as db:
            checks = [tuple(r) for r in db.execute('SELECT * FROM execution_profile_checks')]
            db.execute('DELETE FROM execution_profile_checks')
        first = fresh(); first.start(); first.close()
        self.assertEqual(calls, [], 'a workspace that never ran a session or profile test needs no Docker sweep')
        with self.store.transaction() as db:
            db.executemany('INSERT INTO execution_profile_checks VALUES (%s)' % ','.join('?' * len(checks[0])), checks)
        second = fresh(); second.start(); second.close()
        self.assertTrue(calls, 'after a profile test (which provisions containers) the startup sweep runs')

    def test_restart_contains_recorded_harness_and_container_without_rerun(self):
        sid, lease, harness = self.crashed_session()
        report = self.sessions.start()
        self.assertEqual(report['status'], 'running')
        final = self.sessions.get(sid)
        self.assertEqual((final['state'], final['error_code']), ('interrupted', 'runtime_interrupted'))
        self.assertTrue(final['outcome']['edits_since_last_checkpoint_may_be_lost'])
        self.assertTrue(gone(harness))
        self.assertEqual(self.world.containers, {})
        self.assertEqual(self.jobs.get(lease.job.id).state, 'failed')  # resolved only after verified containment
        time.sleep(0.3)
        self.assertEqual(self.world.provisioned, [sid])  # the fixture's own container, never a rerun
        self.assertEqual(self.world.runs, [])
        recovery = [e['payload'] for e in self.all_events(sid) if e['type'] == 'containment']
        self.assertEqual(recovery[0]['source'], 'recovery')
        self.assertEqual((recovery[0]['harness'], recovery[0]['container']), ('terminated', 'terminated'))
        # An interrupted attempt can be retried explicitly as a linked successor.
        self.assertEqual(self.sessions.retry(sid, 'k-crash-retry')['state'], 'queued')

    def test_unverifiable_containment_stays_visible_and_blocks_launches(self):
        sid, lease, harness = self.crashed_session()
        self.world.stop_fails = True
        self.sessions.start()
        final = self.sessions.get(sid)
        self.assertEqual((final['state'], final['error_code']), ('interrupted', 'containment_failed'))
        self.assertEqual(final['runner']['containment'], 'failed')
        self.assertEqual(self.jobs.get(lease.job.id).state, 'needs_reconciliation')
        _, other = self.ready_plan(goal='Next')
        with self.assertRaises(SessionError) as caught:
            self.sessions.launch(other['id'], other['content_hash'], 'k-next')
        self.assertEqual(caught.exception.code, 'containment_unresolved')
        # A later reconciliation that verifies containment resolves the uncertain job.
        self.world.stop_fails = False
        self.sessions.runtime.reconcile()
        self.assertEqual(self.sessions.get(sid)['runner']['containment'], 'confirmed')
        self.assertEqual(self.jobs.get(lease.job.id).state, 'failed')

    def test_recycled_pid_is_never_signalled(self):
        victim = subprocess.Popen(['/bin/sleep', '300'], start_new_session=True)
        self.world.children.append(victim)
        self.assertEqual(contain_recorded(victim.pid, 'ps:Thu Jan  1 00:00:00 1970'), 'absent')
        self.assertIsNone(victim.poll())
        self.assertEqual(contain_recorded(os.getpid(), start_identity(os.getpid())), 'unverified')

    def test_second_process_on_same_data_dir_reports_runtime_in_use(self):
        sid, lease, harness = self.crashed_session()
        other_platform = Platform.open(self.data)
        self.addCleanup(other_platform.close)
        holder = self.make_service()
        self.assertEqual(holder.start()['status'], 'running')
        self.assertEqual(self.sessions.get(sid)['state'], 'interrupted')
        harness2 = subprocess.Popen(['/bin/sleep', '300'], start_new_session=True)
        self.world.children.append(harness2)
        from opendevengine.sessions import SessionService
        from test_sessions_fixture import ScriptedAgent
        second = SessionService(other_platform, self.index, self.github, self.providers, self.plans, self.profiles,
                                runner_factory=lambda iid: FakeRunner(iid, self.world), adapter_factory=lambda: ScriptedAgent(self.world))
        self.addCleanup(second.close)
        self.assertEqual(second.start()['status'], 'runtime_in_use')
        self.assertEqual(second.recover()['status'], 'runtime_in_use')
        self.assertIn('runtime_in_use', second.preflight(self.plans.validate(self.content()))['blocking_reasons'])
        self.assertIsNone(harness2.poll())
        holder.close()
        self.assertEqual(second.start()['status'], 'running')  # leadership released on close

    def test_shutdown_interrupts_contains_and_restart_resolves_without_rerun(self):
        self.world.script = [('write_file', {'path': 'calc.py', 'content': 'z = 3\n'}), 'hang']
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-shutdown')['session_id']
        self.wait(lambda: any(e['type'] == 'checkpoint' and e['payload'].get('files') for e in self.all_events(sid)),
                  message='edit checkpoint')
        harness = self.world.children[0]
        self.sessions.close()
        final = self.sessions.get(sid)
        self.assertEqual((final['state'], final['error_code']), ('interrupted', 'shutdown'))
        self.assertTrue(final['outcome']['containment']['confirmed'])
        self.assertTrue(gone(harness))
        self.assertEqual(self.world.containers, {})
        job = self.jobs.get(self.store.conn.execute('SELECT job_id FROM sessions WHERE id=?', (sid,)).fetchone()[0])
        self.assertEqual(job.state, 'needs_reconciliation')
        self.assertEqual({f['path'] for f in self.sessions.diff(sid)['files']}, {'calc.py'})
        restarted = self.make_service()
        restarted.start()
        self.assertEqual(self.jobs.get(job.id).state, 'failed')
        self.assertEqual(restarted.get(sid)['state'], 'interrupted')
        time.sleep(0.3)
        self.assertEqual(len(self.world.runs), 1)

    def test_expired_lease_is_fenced_then_reconciled_as_interrupted(self):
        def expire():
            with self.store.transaction() as db:
                db.execute("UPDATE jobs SET lease_expires_at=1 WHERE kind='session.run'")
        self.world.script = [('read_file', {'path': 'calc.py'}), expire, ('write_file', {'path': 'calc.py', 'content': 'q\n'})]
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-expire')['session_id']
        final = self.wait_state(sid, timeout=20)  # maintenance reconcile runs inside the runtime loop
        self.assertEqual(final['state'], 'interrupted')
        self.assertFalse([e for e in self.all_events(sid) if e['type'] == 'tool.started' and e['payload'].get('tool') == 'write_file'])
        self.assertEqual(self.world.containers, {})
        self.assertEqual(self.jobs.get(self.store.conn.execute('SELECT job_id FROM sessions WHERE id=?', (sid,)).fetchone()[0]).state, 'failed')


if __name__ == '__main__':
    unittest.main()
