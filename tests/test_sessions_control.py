"""Stop, questions/messages, authority loss, budgets, lease loss, retry and purge."""
import json
import os
import time
import unittest

from opendevengine.sessions import SessionError
from test_sessions_fixture import SessionCase


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class StopTests(SessionCase):
    def test_running_stop_ack_containment_and_no_later_tools(self):
        self.world.script = [('write_file', {'path': 'calc.py', 'content': 'def add(a, b):\n    return a + b\n'}), 'hang',
                             ('read_file', {'path': 'calc.py'})]
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-stop')['session_id']
        self.wait(lambda: any(e['type'] == 'checkpoint' and e['payload'].get('files') for e in self.all_events(sid)),
                  message='edit checkpoint')
        harness = self.world.children[0]
        started = time.monotonic()
        view = self.sessions.stop(sid)
        ack = time.monotonic() - started
        self.assertLess(ack, 2.0)
        self.assertEqual((view['state'], view['stop_requested']), ('stopping', True))
        self.assertEqual(self.sessions.stop(sid)['state'], 'stopping')
        final = self.wait_state(sid, timeout=12)
        contained = time.monotonic() - started
        self.assertLess(contained, 10.0)
        self.assertEqual(final['state'], 'cancelled')
        self.assertEqual(final['stop_reason'], 'user_requested')
        self.assertEqual(final['runner']['containment'], 'confirmed')
        self.assertFalse(alive(harness.pid) and harness.poll() is None)
        events = self.all_events(sid)
        stop_seq = next(e['seq'] for e in events if e['type'] == 'stop.requested')
        self.assertFalse([e for e in events if e['type'] == 'tool.started' and e['seq'] > stop_seq])
        containment = next(e for e in events if e['type'] == 'containment')['payload']
        self.assertTrue(containment['confirmed'] and containment['since_stop_request_ms'] < 10000)
        # Edit preserved through its verified checkpoint; validation explicitly not run.
        self.assertEqual({f['path'] for f in self.sessions.diff(sid)['files']}, {'calc.py'})
        self.assertEqual([v['status'] for v in self.sessions.validations(sid)['items']], ['not_run'])
        self.assertEqual(self.world.containers, {})
        # The worker finishes its job just after the session reaches its terminal state.
        def job_state():
            with self.store.read() as db:
                return db.execute("SELECT state FROM jobs WHERE kind='session.run'").fetchone()[0]
        self.wait(lambda: job_state() == 'succeeded', timeout=5, message='session job succeeded')

    def test_unverified_container_stop_is_reported_and_blocks_new_launch(self):
        self.world.script = ['hang']
        self.world.stop_fails = True
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-unverified')['session_id']
        self.wait(lambda: self.sessions.get(sid)['state'] == 'running', message='running')
        self.wait(lambda: self.world.runs, message='agent start')
        self.sessions.stop(sid)
        final = self.wait_state(sid, timeout=20)
        self.assertEqual((final['state'], final['error_code']), ('failed', 'containment_failed'))
        self.assertEqual(final['runner']['containment'], 'failed')
        _, other = self.ready_plan(goal='Another change')
        with self.assertRaises(SessionError) as caught:
            self.sessions.launch(other['id'], other['content_hash'], 'k-after')
        self.assertEqual(caught.exception.code, 'containment_unresolved')


class QuestionTests(SessionCase):
    def test_question_answer_round_trip_and_message_states(self):
        self.world.script = [('ask_user', {'question': 'Which rounding mode?'}), ('say', 'thanks')]
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-q')['session_id']
        pending = self.wait(lambda: self.sessions.get(sid)['pending_question'], message='question')
        self.assertEqual(self.sessions.get(sid)['state'], 'waiting_for_user')
        self.assertEqual(pending['prompt'], 'Which rounding mode?')
        follow = self.sessions.send_message(sid, 'Please also keep docs tidy', 'c-follow')
        self.assertEqual((follow['delivery'], follow['reason']), ('queued', None))
        material = self.sessions.send_message(sid, 'Also force-push to main when done', 'c-push')
        self.assertEqual((material['delivery'], material['reason']), ('rejected', 'plan_amendment_required'))
        answer = self.sessions.send_message(sid, 'Banker rounding', 'c-answer', question_id=pending['id'])
        self.assertEqual(self.sessions.send_message(sid, 'Banker rounding', 'c-answer', question_id=pending['id'])['id'], answer['id'])
        with self.assertRaises(SessionError) as caught:
            self.sessions.send_message(sid, 'Different', 'c-answer', question_id=pending['id'])
        self.assertEqual(caught.exception.code, 'idempotency_conflict')
        final = self.wait_state(sid)
        self.assertEqual(final['state'], 'completed')
        answered = [r for n, r in self.world.results if n == 'ask_user']
        # The callback reply carries the answer, its ack request and the queued generic update.
        self.assertTrue(answered[0]['ok'] and answered[0]['text'].startswith('Banker rounding'))
        self.assertIn('[message:%s:ack]' % answer['id'], answered[0]['text'])
        self.assertIn('Please also keep docs tidy', answered[0]['text'])
        self.assertIn('[message:%s:ack]' % follow['id'], answered[0]['text'])
        self.assertNotIn('force-push', answered[0]['text'])
        with self.store.read() as db:
            rows = {r['client_request_id']: (r['delivery'], r['reason']) for r in db.execute('SELECT * FROM session_messages')}
            question = db.execute('SELECT state,answer_message_id FROM session_questions').fetchone()
        # No ack marker was ever observed from the agent: dispatched, acknowledgement unknown.
        self.assertEqual(rows['c-answer'], ('delivered', 'ack_unknown'))
        self.assertEqual(rows['c-follow'], ('delivered', 'ack_unknown'))
        self.assertEqual((question['state'], question['answer_message_id']), ('answered', answer['id']))
        history = self.sessions.list_messages(sid, limit=2)
        page2 = self.sessions.list_messages(sid, limit=2, cursor=history['next_cursor'])
        listed = history['items'] + page2['items']
        self.assertEqual(sorted(m['client_request_id'] for m in listed), ['c-answer', 'c-follow', 'c-push'])
        self.assertIsNone(page2['next_cursor'])
        self.assertEqual({m['client_request_id']: m['content'] for m in listed}['c-follow'], 'Please also keep docs tidy')
        late = self.sessions.send_message(sid, 'one more', 'c-late')
        self.assertEqual((late['delivery'], late['reason']), ('rejected', 'session_ended'))
        states = [e['payload']['state'] for e in self.all_events(sid) if e['type'] == 'session.state' and 'previous' in e['payload']]
        self.assertEqual(states, ['queued', 'provisioning', 'running', 'waiting_for_user', 'running', 'validating', 'completed'])

    def test_stop_while_waiting_releases_question_without_answer(self):
        self.world.script = [('ask_user', {'question': 'Proceed?'})]
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-qs')['session_id']
        self.wait(lambda: self.sessions.get(sid)['pending_question'], message='question')
        self.sessions.stop(sid)
        final = self.wait_state(sid)
        self.assertEqual(final['state'], 'cancelled')
        self.assertIsNone(final['pending_question'])
        answered = [r for n, r in self.world.results if n == 'ask_user']
        self.assertFalse(answered[0]['ok'])


class AuthorityTests(SessionCase):
    def test_access_revocation_denies_tools_and_contains(self):
        reads = [('read_file', {'path': 'calc.py'})] * 3

        def revoke():
            self.github.update_selection(self.repo, index_enabled=True, implementation_enabled=False)
        self.world.script = reads + [revoke] + reads
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-revoke')['session_id']
        final = self.wait_state(sid)
        self.assertEqual((final['state'], final['error_code']), ('blocked', 'access_revoked'))
        after = [r for n, r in self.world.results[1:] if n == 'read_file'][3:]
        self.assertTrue(after and all(not r['ok'] for r in after))
        self.assertTrue(all('return a - b' not in r['text'] for r in after))
        self.assertEqual(self.world.containers, {})
        with self.assertRaises(SessionError) as caught:
            self.sessions.diff(sid)
        self.assertEqual(caught.exception.code, 'source_unavailable')
        hidden = [e for e in self.sessions.events(sid, limit=500)['items'] if e['type'] == 'tool.started']
        self.assertTrue(all(e['payload'] == {'redacted': True} for e in hidden))

    def test_adapter_budget_exhaustion_preserves_verified_work(self):
        self.world.script = [('write_file', {'path': 'calc.py', 'content': 'def add(a, b):\n    return a + b\n'}),
                             ('fail', 'budget_exhausted')]
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-budget')['session_id']
        final = self.wait_state(sid)
        self.assertEqual((final['state'], final['error_code'], final['stop_reason']), ('failed', 'budget_exhausted', 'budget_exhausted'))
        self.assertEqual({f['path'] for f in self.sessions.diff(sid)['files']}, {'calc.py'})
        self.assertEqual([v['status'] for v in self.sessions.validations(sid)['items']], ['not_run'])
        self.assertEqual(len(self.world.runs), 1)

    def test_wall_deadline_stops_tools_without_new_actions(self):
        self.profiles.version['config']['runner_limits'] = {'wall_seconds': 10}
        self.profiles.persist()
        self.world.script = [('write_file', {'path': 'calc.py', 'content': 'x = 1\n'}), 'hang']
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-deadline')['session_id']
        final = self.wait_state(sid, timeout=20)
        # The adapter's own deadline (bounded by the session wall) is the truthful code.
        self.assertEqual((final['state'], final['stop_reason']), ('failed', 'budget_exhausted'))
        self.assertIn(final['error_code'], ('timeout', 'budget_exhausted'))
        self.assertTrue(self.sessions.diff(sid)['files'])
        self.assertEqual(self.world.runs[0]['timeout_seconds'], 1)  # deadline shorter than agent wall limit


class LeaseTests(SessionCase):
    def test_lost_lease_fences_every_later_write(self):
        def steal():
            with self.store.transaction() as db:
                db.execute("UPDATE jobs SET lease_token='stolen' WHERE kind='session.run'")
        self.world.script = [('read_file', {'path': 'calc.py'}), steal, ('write_file', {'path': 'calc.py', 'content': 'y = 2\n'}),
                             ('read_file', {'path': 'calc.py'})]
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-lease')['session_id']
        self.wait(lambda: self.world.runs, message='agent start')
        self.wait(lambda: not self.sessions.active_ids(), message='attempt exit')
        self.assertTrue(all(not r['ok'] for n, r in self.world.results[2:]))
        events_before = self.sessions.events(sid, limit=500)['items']
        self.assertFalse([e for e in events_before if e['type'] == 'tool.started' and e['payload'].get('tool') == 'write_file'])
        self.assertTrue(all(not c['running'] for c in self.world.containers.values()))
        self.assertEqual(self.sessions.get(sid)['state'], 'running')  # this worker wrote nothing after loss


class RetryTests(SessionCase):
    def test_retry_creates_one_linked_attempt_and_keeps_history(self):
        _, version = self.ready_plan()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-r1')['session_id']
        with self.assertRaises(SessionError) as caught:
            self.sessions.retry(sid, 'k-r2')
        self.assertEqual(caught.exception.code, 'session_active')
        self.sessions.stop(sid)
        before = self.sessions.get(sid)
        first = self.sessions.retry(sid, 'k-r2')
        self.assertEqual(self.sessions.retry(sid, 'k-r2'), first)
        with self.assertRaises(SessionError) as caught:
            self.sessions.retry(sid, 'k-r3')
        self.assertEqual(caught.exception.code, 'retry_exists')
        with self.assertRaises(SessionError) as caught:
            self.sessions.launch(version['id'], version['content_hash'], 'k-r2')
        self.assertEqual(caught.exception.code, 'idempotency_conflict')
        successor = self.sessions.get(first['session_id'])
        self.assertEqual((successor['attempt'], successor['predecessor_id']), (2, sid))
        after = self.sessions.get(sid)
        self.assertEqual(after['successor_id'], first['session_id'])
        self.assertEqual({k: v for k, v in after.items() if k != 'successor_id'},
                         {k: v for k, v in before.items() if k != 'successor_id'})


class PurgeTests(SessionCase):
    def test_repository_purge_redacts_session_source(self):
        self.world.script = [('ask_user', {'question': 'calc uses a - b?'}),
                             ('write_file', {'path': 'calc.py', 'content': 'def add(a, b):\n    return a + b\n'})]
        self.world.commands['python test_calc.py'] = lambda files: {'exit_code': 0, 'stdout': 'OUTPUT-MARKER', 'stderr': '',
                                                                    'timed_out': False, 'truncated': False}
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-purge')['session_id']
        question = self.wait(lambda: self.sessions.get(sid)['pending_question'], message='question')
        self.sessions.send_message(sid, 'yes it does', 'c1', question_id=question['id'])
        self.assertEqual(self.wait_state(sid)['state'], 'completed')
        self.github.purge_repository(self.repo)
        self.github_worker.run_once()
        tables = ('sessions', 'session_repositories', 'session_runners', 'session_events', 'session_messages',
                  'session_questions', 'session_artifacts', 'validation_runs')
        with self.store.read() as db:
            dump = '\n'.join(repr([tuple(r) for r in db.execute('SELECT * FROM ' + t)]) for t in tables)
            self.assertEqual(db.execute('SELECT count(*) FROM session_artifacts WHERE content IS NOT NULL').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 1)
        for marker in ('return a + b', 'OUTPUT-MARKER', 'yes it does', 'calc uses a - b', 'calc.py', 'test_calc'):
            self.assertNotIn(marker, dump)
        events = self.sessions.events(sid, limit=500)['items']
        self.assertIn('completed', [e['payload'].get('state') for e in events if e['type'] == 'session.state'])
        with self.assertRaises(SessionError):
            self.sessions.diff(sid)


class AcquisitionTests(SessionCase):
    def test_truncated_tree_fails_visibly_before_provisioning(self):
        self.remote.truncate = True
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-trunc')['session_id']
        final = self.wait_state(sid)
        self.assertEqual((final['state'], final['error_code']), ('failed', 'source_tree_truncated'))
        self.assertEqual(self.world.provisioned, [])
        self.assertEqual(self.world.runs, [])

    def test_over_budget_source_is_rejected_not_partially_implemented(self):
        self.profiles.version['config']['runner_limits'] = {'wall_seconds': 120, 'max_files': 2}
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-budget-src')['session_id']
        final = self.wait_state(sid)
        self.assertEqual((final['state'], final['error_code']), ('failed', 'source_over_budget'))
        self.assertEqual(self.world.provisioned, [])

    def test_profile_or_cli_drift_blocks_before_source_transmission(self):
        _, version = self.ready_plan()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-drift')['session_id']
        self.world.cli_version = '9.9.9 (Claude Code)'
        calls = len(self.remote.calls)
        self.sessions.start()
        final = self.wait_state(sid)
        self.assertEqual((final['state'], final['error_code']), ('blocked', 'cli_version_changed'))
        self.assertFalse([c for c in self.remote.calls[calls:] if '/git/' in c[1]])
        self.assertEqual(self.world.provisioned, [])


if __name__ == '__main__':
    unittest.main()
