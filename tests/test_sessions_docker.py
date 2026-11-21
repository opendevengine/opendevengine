"""Opt-in real Docker session checks: OPENDEVENGINE_SESSION_DOCKER=1 and a local cached image.

Uses the real DockerRunner (no pulls), real broker and the scripted agent (no model).
Proves container provisioning from pinned GitHub-API source, in-container validation,
real stop/containment timing and owned-container cleanup on this host only.
"""
import os
import secrets
import time
import unittest

from opendevengine.execution.runner import DockerRunner, RunnerError
from test_sessions_fixture import SessionCase

IMAGE = os.environ.get('OPENDEVENGINE_SESSION_DOCKER_IMAGE', 'python:3.12-slim')


@unittest.skipUnless(os.environ.get('OPENDEVENGINE_SESSION_DOCKER') == '1', 'set OPENDEVENGINE_SESSION_DOCKER=1 to run real Docker checks')
class DockerSessionTests(SessionCase):
    def setUp(self):
        super().setUp()
        try:
            probe = DockerRunner('sessions-probe-' + secrets.token_hex(3)).preflight(IMAGE)
        except RunnerError as exc:
            self.skipTest('docker unavailable: ' + exc.code)
        if probe['blocking_reasons']:
            self.skipTest('docker image unavailable: ' + ','.join(probe['blocking_reasons']))
        self.image_id = probe['image_id']
        self.profiles.version['config']['image'] = IMAGE
        self.profiles.version['check']['image_id'] = self.image_id
        from opendevengine.sessions import SessionService
        from test_sessions_fixture import ScriptedAgent
        self.runners = []

        def factory(instance_id):
            runner = DockerRunner(instance_id)
            self.runners.append(runner)
            return runner
        self.sessions = SessionService(self.platform, self.index, self.github, self.providers, self.plans, self.profiles,
                                       runner_factory=factory, adapter_factory=lambda: ScriptedAgent(self.world),
                                       lease_ms=10000, poll_interval=0.05)
        self.addCleanup(self.sessions.close)
        self.plans.execution_preflight = self.sessions.preflight
        self.addCleanup(self.assert_no_owned)

    def assert_no_owned(self):
        runner = DockerRunner(self.sessions.instance_id())
        leftover = runner.list_owned()
        for handle in leftover:
            runner.stop(handle)
            runner.discard(handle)
        self.assertEqual(leftover, [])

    def test_real_container_session_validation_and_diff(self):
        self.world.script = [('read_file', {'path': 'calc.py'}),
                             ('write_file', {'path': 'calc.py', 'content': 'def add(a, b):\n    return a + b\n'}),
                             ('run_command', {'argv': ['python', 'test_calc.py']})]
        _, version = self.ready_plan(validation=[
            {'command': 'python test_calc.py', 'expected': 'passes', 'source': 'proposed', 'citation_ids': [], 'approved': True},
            {'command': 'curl https://example.com', 'expected': 'n/a', 'source': 'proposed', 'citation_ids': [], 'approved': True}])
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-docker')['session_id']
        final = self.wait_state(sid, timeout=180)
        self.assertEqual(final['state'], 'completed', final)
        statuses = [v['status'] for v in self.sessions.validations(sid)['items']]
        self.assertEqual(statuses, ['passed', 'unavailable'])
        self.assertIn('+    return a + b', self.sessions.diff(sid)['files'][0]['diff'])
        self.assertTrue(final['outcome']['containment']['confirmed'])

    def test_real_container_modes_and_read_only_context(self):
        from opendevengine.platform.worker import Worker
        run = self.index.request_index('gh:2')
        Worker(self.jobs, {'index.run': self.index.run_index}, worker_id='index2').run_once()
        self.assertEqual(self.index.get_run(run['id'])['state'], 'completed')
        snap = self.index.get_state('gh:2')['active_snapshot']
        read = {'repository_id': 'gh:2', 'role': 'read', 'snapshot_id': snap['id'], 'revision': snap['revision'], 'branch': 'main'}
        self.world.script = [('run_command', {'argv': ['./run.sh']}),
                             ('run_command', {'argv': ['cat', '/context/gh-2/shared/money.py']}),
                             ('run_command', {'argv': ['sh', '-c', 'echo x > /context/gh-2/shared/money.py']}),
                             ('run_command', {'argv': ['sh', '-c', 'chmod -x run.sh']})]
        content = self.content()
        _, version = self.ready_plan(repositories=content['repositories'] + [read], validation=[
            {'command': 'test ! -w /context/gh-2/shared/money.py', 'expected': 'context read-only', 'source': 'proposed',
             'citation_ids': [], 'approved': True}])
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-docker-ctx')['session_id']
        final = self.wait_state(sid, timeout=180)
        self.assertEqual(final['state'], 'completed', final)
        outputs = [r for n, r in self.world.results if n == 'run_command']
        import json as _json
        decoded = [_json.loads(r['text']) for r in outputs]
        self.assertEqual(decoded[0]['stdout'].strip(), 'run')  # base 0755 preserved
        self.assertIn('round_half_even', decoded[1]['stdout'])
        self.assertNotEqual(decoded[2]['exit_code'], 0)  # context immutable for the actor
        self.assertEqual([v['status'] for v in self.sessions.validations(sid)['items']], ['passed'])
        files = {f['path']: f for f in self.sessions.diff(sid)['files']}
        self.assertEqual((files['run.sh']['old_mode'], files['run.sh']['mode']), ('100755', '100644'))

    def test_real_container_stop_timing(self):
        self.world.script = ['hang']
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-docker-stop')['session_id']
        self.wait(lambda: self.world.runs, timeout=120, message='agent start')
        started = time.monotonic()
        self.assertEqual(self.sessions.stop(sid)['state'], 'stopping')
        ack = time.monotonic() - started
        final = self.wait_state(sid, timeout=30)
        total = time.monotonic() - started
        self.assertLess(ack, 2.0)
        self.assertLess(total, 10.0)
        self.assertEqual((final['state'], final['runner']['containment']), ('cancelled', 'confirmed'))
        print('docker stop ack_s=%.3f contained_s=%.3f container_ms=%s' % (
            ack, total, final['outcome']['containment']['container_elapsed_ms']))


if __name__ == '__main__':
    unittest.main()
