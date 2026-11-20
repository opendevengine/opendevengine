"""Regression tests for implementation session completion paths.

Real SQLite/jobs/GitHub/Index/Plan services; injected runner/agent doubles as in
test_sessions_fixture (orchestration proof only, not inference or Docker proof).
"""
import json
import os
import subprocess
import threading
import time
import unittest
from unittest import mock

from opendevengine.platform.worker import Worker
from opendevengine.sessions import SessionError
from opendevengine.sessions import attempt as attempt_module
from opendevengine.sessions.processes import contain_recorded
from test_sessions_fixture import FakeRunner, SessionCase

SESSION_TABLES = ('sessions', 'session_repositories', 'session_runners', 'session_events', 'session_messages',
                  'session_questions', 'session_artifacts', 'validation_runs')
FIX = 'def add(a, b):\n    return a + b\n'


def ok(stdout=''):
    return {'exit_code': 0, 'stdout': stdout, 'stderr': '', 'timed_out': False, 'truncated': False}


class ReviewCase(SessionCase):
    def session_dump(self):
        with self.store.read() as db:
            return '\n'.join(repr([tuple(r) for r in db.execute('SELECT * FROM ' + t)]) for t in SESSION_TABLES)

    def run_plan(self, key, **plan):
        _, version = self.ready_plan(**plan)
        self.sessions.start()
        return self.sessions.launch(version['id'], version['content_hash'], key)['session_id']

    def tool_results(self, name):
        return [r for n, r in self.world.results if n == name]


class PrivacyFenceTests(ReviewCase):
    def test_r1_exclusion_change_stops_tools_and_hides_every_projection(self):
        def exclude():
            self.index.set_exclusions(self.repo, ['calc.py'])
        self.world.script = [('read_file', {'path': 'calc.py'}), ('ask_user', {'question': 'calc.py shows a - b?'}),
                             exclude, ('read_file', {'path': 'calc.py'}), ('read_file', {'path': 'README.md'})]
        sid = self.run_plan('k-r1')
        question = self.wait(lambda: self.sessions.get(sid)['pending_question'], message='question')
        self.sessions.send_message(sid, 'yes, calc.py subtracts', 'c-r1', question_id=question['id'])
        final = self.wait_state(sid)
        self.assertEqual((final['state'], final['error_code']), ('blocked', 'policy_changed'))
        later = self.tool_results('read_file')[1:]
        self.assertTrue(later and all(not r['ok'] for r in later))
        self.assertFalse(any('return a - b' in r.get('text', '') for r in later))
        self.assertFalse(final['source_available'])
        self.assertEqual(final['source_unavailable_reason'], 'exclusions_changed')
        events = self.sessions.events(sid, limit=500)
        self.assertFalse(events['source_available'])
        blob = json.dumps(events)
        self.assertNotIn('calc.py', blob)
        with self.assertRaises(SessionError):
            self.sessions.diff(sid)
        validations = self.sessions.validations(sid)
        self.assertTrue(all(v['command'] == {'redacted': True} and v['output'] is None for v in validations['items']))
        messages = self.sessions.list_messages(sid)
        self.assertTrue(all(m['content'] is None and m['content_redacted'] for m in messages['items']))
        # Reverting the policy to the pinned one restores visibility of retained history.
        self.index.set_exclusions(self.repo, [])
        self.assertTrue(self.sessions.get(sid)['source_available'])

    def test_r2_purge_during_active_run_fences_all_later_source_writes(self):
        def purge():
            self.github.purge_repository(self.repo)
            self.github_worker.run_once()
        self.world.commands['python test_calc.py'] = lambda files: ok('PURGE-OUTPUT')
        self.world.script = [('write_file', {'path': 'calc.py', 'content': FIX}), purge,
                             ('read_file', {'path': 'calc.py'}), ('say', 'calc.py summary after purge')]
        sid = self.run_plan('k-r2')
        final = self.wait_state(sid)
        self.assertEqual((final['state'], final['error_code']), ('blocked', 'repository_purged'))
        self.assertTrue(final['outcome']['source_redacted'])
        dump = self.session_dump()
        for marker in ('python test_calc.py', 'PURGE-OUTPUT', 'return a + b', 'calc.py', 'summary after purge'):
            self.assertNotIn(marker, dump)
        with self.store.read() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM session_artifacts WHERE content IS NOT NULL').fetchone()[0], 0)
            commands = [json.loads(r[0]) for r in db.execute('SELECT command_json FROM validation_runs')]
        self.assertTrue(commands and all(c == {'redacted': True} for c in commands))
        self.assertTrue(all(v['command'] == {'redacted': True} for v in self.sessions.validations(sid)['items']))

    def test_r5_local_only_enabled_mid_run_stops_source_transmission(self):
        def privacy_on():
            self.providers.update_settings(local_only=True)
        self.world.script = [('read_file', {'path': 'calc.py'}), privacy_on, ('read_file', {'path': 'calc.py'}),
                             ('list_files', {})]
        sid = self.run_plan('k-r5')
        final = self.wait_state(sid)
        self.assertEqual((final['state'], final['error_code']), ('blocked', 'policy_changed'))
        later = self.tool_results('read_file')[1:] + self.tool_results('list_files')
        self.assertTrue(later and all(not r['ok'] for r in later))
        self.assertEqual(self.world.containers, {})

    def test_profile_consent_revoked_mid_run_stops_tools(self):
        def revoke():
            self.profiles.version['config']['allow_external_source'] = False
        self.world.script = [('read_file', {'path': 'calc.py'}), revoke, lambda: time.sleep(2.5),
                             ('read_file', {'path': 'calc.py'})]
        sid = self.run_plan('k-consent')
        final = self.wait_state(sid)
        self.assertEqual((final['state'], final['error_code']), ('blocked', 'policy_changed'))
        self.assertTrue(all(not r['ok'] for r in self.tool_results('read_file')[1:]))
        self.assertEqual(self.world.containers, {})

    def test_profile_consent_revocation_is_synchronous_at_next_tool(self):
        def revoke():
            self.profiles.version['config']['allow_external_source'] = False
        self.world.script = [('read_file', {'path': 'calc.py'}), revoke,
                             ('read_file', {'path': 'README.md'})]
        sid = self.run_plan('k-consent-sync')
        final = self.wait_state(sid)
        self.assertEqual((final['state'], final['error_code']), ('blocked', 'policy_changed'))
        self.assertEqual(self.tool_results('read_file')[-1]['code'], 'policy_denied')


class StopAdmissionTests(ReviewCase):
    def test_r3_stop_between_guard_and_admission_never_starts_the_tool(self):
        original = attempt_module.Attempt.admit
        reads = []
        real_read = FakeRunner.read_file

        def counting_read(runner, handle, path):
            reads.append(path)
            return real_read(runner, handle, path)

        def racing_admit(attempt, payload):
            if payload.get('path') == 'README.md':
                # The broker guard already allowed this call; stop commits before admission.
                self.sessions.stop(attempt.sid)
            return original(attempt, payload)
        self.world.script = [('read_file', {'path': 'calc.py'}), ('read_file', {'path': 'README.md'})]
        with mock.patch.object(attempt_module.Attempt, 'admit', racing_admit), \
                mock.patch.object(FakeRunner, 'read_file', counting_read):
            sid = self.run_plan('k-r3')
            final = self.wait_state(sid)
        self.assertEqual(final['state'], 'cancelled')
        self.assertEqual(reads, ['calc.py'])  # the raced call never reached the runner
        self.assertEqual(self.tool_results('read_file')[-1]['code'], 'event_unavailable')
        events = self.all_events(sid)
        stop_seq = next(e['seq'] for e in events if e['type'] == 'stop.requested')
        self.assertFalse([e for e in events if e['type'] == 'tool.started' and e['seq'] > stop_seq])
        denied = [e for e in events if e['type'] == 'tool.denied' and e['payload'].get('phase') == 'admission']
        self.assertTrue(denied)


class IdentityTests(ReviewCase):
    def test_r4_live_pid_without_identity_is_unverified_not_absent(self):
        proc = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True)
        self.world.children.append(proc)
        self.assertEqual(contain_recorded(proc.pid, None), 'unverified')
        self.assertIsNone(proc.poll())

    def test_r4_identity_capture_failure_fails_closed_and_blocks_launches(self):
        self.world.script = [('read_file', {'path': 'calc.py'})]
        with mock.patch.object(attempt_module, 'start_identity', side_effect=OSError('ps unavailable')):
            sid = self.run_plan('k-r4')
            final = self.wait_state(sid)
        self.assertEqual((final['state'], final['error_code']), ('failed', 'runtime_identity_unverified'))
        self.assertEqual(final['runner']['containment'], 'failed')
        self.assertTrue(all(not r['ok'] for r in self.tool_results('read_file')))
        _, other = self.ready_plan(goal='next')
        with self.assertRaises(SessionError) as caught:
            self.sessions.launch(other['id'], other['content_hash'], 'k-r4b')
        self.assertEqual(caught.exception.code, 'containment_unresolved')
        # The harness is gone (adapter reaped it); a later reconcile proves absence and clears the block.
        self.sessions.runtime.reconcile()
        self.assertEqual(self.sessions.get(sid)['runner']['containment'], 'confirmed')


class ValidationCaptureTests(ReviewCase):
    def test_r6_validation_mutation_with_failed_capture_is_never_clean(self):
        def mutate_then_break(files):
            files['calc.py'] = b'def add(a, b):\n    return 42\n'
            self.world.snapshot_fails = True
            return ok('passes')
        self.world.commands['python test_calc.py'] = mutate_then_break
        self.world.script = [('say', 'no edits by agent')]
        sid = self.run_plan('k-r6')
        final = self.wait_state(sid)
        outcome = final['outcome']
        self.assertEqual(final['state'], 'completed')
        self.assertEqual(outcome['result'], 'incomplete')
        self.assertTrue(outcome['artifacts_stale'] and outcome['edits_since_last_checkpoint_may_be_lost'])
        self.assertFalse(outcome['validation']['fully_validated'])
        self.assertFalse(outcome['publication_eligible'])
        self.assertIn('checkpoint', [e['type'] for e in self.all_events(sid) if e['payload'].get('ok') is False])


class PinnedRetryTests(ReviewCase):
    def test_r7_retry_keeps_predecessor_version_limits_and_instructions(self):
        _, version = self.ready_plan()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-r7')['session_id']
        self.sessions.stop(sid)
        before = self.sessions.get(sid)
        self.profiles.add_version(agent_limits={'wall_seconds': 60, 'max_turns': 7, 'max_budget_usd': 2.0,
                                                'max_output_bytes': 65536})
        self.sessions._preflight_cache.clear()
        successor = self.sessions.get(self.sessions.retry(sid, 'k-r7-retry')['session_id'])
        self.assertEqual(successor['execution']['profile_version_id'], 'epv_1')
        self.assertEqual(successor['limits'], before['limits'])
        self.assertEqual(successor['instructions'], before['instructions'])
        self.assertEqual(successor['publication']['policy'], before['publication']['policy'])
        self.world.script = [('say', 'retry')]
        self.sessions.start()
        self.assertEqual(self.wait_state(successor['id'])['state'], 'completed')
        self.assertEqual((self.world.runs[-1]['max_turns'], self.world.runs[-1]['max_budget_usd']), (5, 1.0))

    def test_r7_harmless_active_update_does_not_block_queued_pinned_attempt(self):
        _, version = self.ready_plan()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-r7b')['session_id']
        self.profiles.add_version(image='python:3.12-slim')
        self.world.script = [('say', 'pinned')]
        self.sessions.start()
        self.assertEqual(self.wait_state(sid)['state'], 'completed')

    def test_r7_retry_still_requires_fresh_permission(self):
        _, version = self.ready_plan()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-r7c')['session_id']
        self.sessions.stop(sid)
        self.profiles.version['config']['allow_external_source'] = False
        self.sessions._preflight_cache.clear()
        with self.assertRaises(SessionError) as caught:
            self.sessions.retry(sid, 'k-r7c-retry')
        self.assertEqual(caught.exception.code, 'plan_not_ready')

    def test_retry_keeps_exact_predecessor_after_newer_plan_version(self):
        plan, version = self.ready_plan()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-old-attempt')['session_id']
        self.sessions.stop(sid)
        newer = self.plans.save_version(plan['id'], self.content(requirements=['new intent']), version['id'])['version']
        successor = self.sessions.get(self.sessions.retry(sid, 'k-old-retry')['session_id'])
        self.assertEqual(successor['plan_version_id'], version['id'])
        self.assertNotEqual(successor['plan_version_id'], newer['id'])


class ProjectionAndPublicationTests(ReviewCase):
    def test_artifact_metadata_is_tombstoned_after_access_revocation(self):
        self.world.script = [('write_file', {'path': 'calc.py', 'content': FIX})]
        sid = self.run_plan('k-artifact-fence')
        self.wait_state(sid)
        before = self.sessions.artifacts(sid)
        self.assertTrue(before['items'] and before['source_available'])
        self.github.update_selection(self.repo, index_enabled=True, implementation_enabled=False)
        after = self.sessions.artifacts(sid)
        self.assertFalse(after['source_available'])
        self.assertTrue(all(x['redacted'] and x['sha256'] is None and x['byte_count'] is None
                            for x in after['items']))

    def test_automatic_publication_requires_every_validation_to_pass(self):
        self.world.script = [('write_file', {'path': 'calc.py', 'content': FIX})]
        _, version = self.ready_plan(publication={'policy': 'on_success_draft'})
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-publish-gate')['session_id']
        final = self.wait_state(sid)
        self.assertFalse(final['outcome']['publication_eligible'])
        self.assertFalse(final['outcome']['validation']['fully_validated'])


class FollowUpTests(ReviewCase):
    def test_generic_update_dispatched_in_tool_result_and_acked_only_by_marker(self):
        holder = {}

        def send():
            holder['m'] = self.sessions.send_message(holder['sid'], 'Prefer integer math please', 'c-gen')
        self.world.script = [('read_file', {'path': 'calc.py'}), send, ('list_files', {}), 'ack',
                             ('say', 'done'), lambda: holder.update(late=self.sessions.send_message(holder['sid'], 'too late', 'c-late'))]
        _, version = self.ready_plan()
        self.sessions.start()
        holder['sid'] = sid = self.sessions.launch(version['id'], version['content_hash'], 'k-gen')['session_id']
        final = self.wait_state(sid)
        self.assertEqual(final['state'], 'completed')
        listed = self.tool_results('list_files')[0]
        self.assertIn('Prefer integer math please', listed['text'])
        self.assertIn('[message:%s:ack]' % holder['m']['id'], listed['text'])
        with self.store.read() as db:
            rows = {r['client_request_id']: (r['delivery'], r['reason']) for r in db.execute('SELECT * FROM session_messages')}
        self.assertEqual(rows['c-gen'], ('acknowledged', 'model_marker_observed'))
        self.assertEqual(rows['c-late'], ('rejected', 'terminal_before_delivery'))
        deliveries = [e['payload'] for e in self.all_events(sid) if e['type'] == 'message.delivery'
                      and e['payload'].get('message_id') == holder['m']['id']]
        self.assertEqual([d['delivery'] for d in deliveries], ['queued', 'delivered', 'acknowledged'])
        summaries = [e['payload'].get('text', '') for e in self.all_events(sid) if e['type'] == 'agent.summary']
        self.assertFalse([t for t in summaries if '[message:' in t])  # private marker stripped from UI text
        self.assertTrue(final['capabilities']['follow_up'])


class ReadContextTests(ReviewCase):
    def setUp(self):
        super().setUp()
        run = self.index.request_index('gh:2')
        Worker(self.jobs, {'index.run': self.index.run_index}, worker_id='index2').run_once()
        self.assertEqual(self.index.get_run(run['id'])['state'], 'completed')
        snap = self.index.get_state('gh:2')['active_snapshot']
        self.read = {'repository_id': 'gh:2', 'role': 'read', 'snapshot_id': snap['id'], 'revision': snap['revision'],
                     'branch': 'main'}

    def plan_with_read(self):
        content = self.content()
        content['repositories'] = content['repositories'] + [self.read]
        return content

    def test_read_only_context_is_provisioned_separately_and_listed(self):
        self.world.script = [('run_command', {'argv': ['cat', '/context/gh-2/shared/money.py']}),
                             ('write_file', {'path': 'calc.py', 'content': FIX})]
        _, version = self.ready_plan(repositories=self.plan_with_read()['repositories'])
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-ctx')['session_id']
        final = self.wait_state(sid)
        self.assertEqual(final['state'], 'completed', final)
        text = json.loads(self.tool_results('run_command')[0]['text'])
        self.assertIn('round_half_even', text['stdout'])
        self.assertIn('/context/gh-2 = gh:2 @ ' + 'd' * 40, self.world.runs[0]['prompt'])
        read = next(r for r in final['repositories'] if r['role'] == 'read')
        self.assertEqual((read['mount'], read['provisioned']), ('/context/gh-2', True))
        provisioned = self.world.provisioned
        self.assertEqual(provisioned, [sid])
        self.assertEqual({f['path'] for f in self.sessions.diff(sid)['files']}, {'calc.py'})
        with self.store.read() as db:
            manifest = json.loads(db.execute("SELECT base_manifest_json FROM session_repositories WHERE role='read'").fetchone()[0])
        self.assertIn('shared/money.py', manifest['files'])
        self.assertEqual(manifest['omitted']['secrets.txt'], 'sensitive')

    def test_context_files_reach_runner_only_under_context_never_workspace(self):
        captured = {}
        real = FakeRunner.provision

        def spy(runner, session_id, files, **kwargs):
            captured.update(files=dict(files), context=dict(kwargs.get('context') or {}))
            return real(runner, session_id, files, **kwargs)
        self.world.script = [('say', 'x')]
        _, version = self.ready_plan(repositories=self.plan_with_read()['repositories'])
        with mock.patch.object(FakeRunner, 'provision', spy):
            self.sessions.start()
            sid = self.sessions.launch(version['id'], version['content_hash'], 'k-ctx2')['session_id']
            self.wait_state(sid)
        self.assertEqual(set(captured['context']), {'gh-2/shared/money.py'})
        self.assertFalse([p for p in captured['files'] if p.startswith('gh-2') or 'money' in p])

    def test_read_context_revocation_stops_the_session(self):
        def revoke():
            self.github.update_selection('gh:2', index_enabled=False, implementation_enabled=False)
        self.world.script = [('read_file', {'path': 'calc.py'}), revoke, lambda: time.sleep(2.5),
                             ('read_file', {'path': 'calc.py'})]
        _, version = self.ready_plan(repositories=self.plan_with_read()['repositories'])
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-ctx3')['session_id']
        final = self.wait_state(sid)
        self.assertEqual((final['state'], final['error_code']), ('blocked', 'access_revoked'))
        self.assertTrue(all(not r['ok'] for r in self.tool_results('read_file')[1:]))
        self.assertEqual(self.world.containers, {})

    def test_unsupported_read_context_kind_blocks_preflight(self):
        local = self.index.register_local('local-ref', self.tmp.name)['repository_id']
        content = self.plans.validate(self.content())
        content['repositories'].append(dict(self.read, repository_id=local))
        self.assertTrue({'read_context_unsupported_kind', 'read_context_unavailable'}
                        & set(self.sessions.preflight(content)['blocking_reasons']))


class ModeTests(ReviewCase):
    def test_executable_modes_preserved_and_mode_changes_reviewable(self):
        captured = {}
        real = FakeRunner.provision

        def spy(runner, session_id, files, **kwargs):
            captured.update(kwargs.get('file_modes') or {})
            return real(runner, session_id, files, **kwargs)

        def chmod(container):
            container['modes']['run.sh'] = '100644'
            container['modes']['calc.py'] = '100755'
            return ok()
        self.world.container_commands['chmod -x run.sh; chmod +x calc.py'] = chmod
        self.world.script = [('run_command', {'argv': ['sh', '-c', 'chmod -x run.sh; chmod +x calc.py']})]
        with mock.patch.object(FakeRunner, 'provision', spy):
            sid = self.run_plan('k-mode')
            final = self.wait_state(sid)
        self.assertEqual(captured['run.sh'], '100755')
        self.assertEqual(captured['calc.py'], '100644')
        self.assertEqual(final['outcome']['result'], 'reviewable')
        files = {f['path']: f for f in self.sessions.diff(sid)['files']}
        self.assertEqual((files['run.sh']['old_mode'], files['run.sh']['mode'], files['run.sh']['mode_changed']),
                         ('100755', '100644', True))
        self.assertEqual((files['calc.py']['old_mode'], files['calc.py']['mode']), ('100644', '100755'))


class ProfileTestRecoveryTests(ReviewCase):
    def setUp(self):
        super().setUp()
        from opendevengine.execution.profiles import ExecutionProfileService
        from opendevengine.sessions import SessionService
        from test_sessions_fixture import ScriptedAgent
        self.real_profiles = ExecutionProfileService(self.platform, self.providers,
                                                     runner_factory=lambda iid: FakeRunner(iid, self.world),
                                                     adapter_factory=lambda: ScriptedAgent(self.world))
        profile = self.real_profiles.create({'name': 'real', 'adapter': 'claude_code', 'auth_mode': 'cli_login',
                                             'image': 'python:3.12-slim', 'allow_external_source': True})
        self.test_job = self.real_profiles.request_test(profile['id'])
        self.version_id = self.test_job['version_id']
        self.sessions.close()
        self.sessions = SessionService(self.platform, self.index, self.github, self.providers, self.plans, self.real_profiles,
                                       runner_factory=lambda iid: FakeRunner(iid, self.world),
                                       adapter_factory=lambda: ScriptedAgent(self.world), lease_ms=1000, poll_interval=0.05)
        self.addCleanup(self.sessions.close)

    def plant(self, owner, lease_ms):
        lease = self.jobs.claim(owner, kinds=['execution.test'], lease_ms=lease_ms)
        harness = subprocess.Popen(['/bin/sleep', '300'], start_new_session=True)
        threading.Thread(target=harness.wait, daemon=True).start()
        self.world.children.append(harness)
        from opendevengine.sessions.processes import start_identity
        runner = FakeRunner(self.sessions.instance_id(), self.world)
        handle = runner.provision(lease.job.id, {'calc.sh': b'x'}, image='python:3.12-slim', limits={})
        private = {'instance_id': self.sessions.instance_id(), 'container_id': handle.container_id,
                   'harness_pid': harness.pid, 'harness_start': start_identity(harness.pid)}
        with self.store.transaction() as db:
            db.execute("UPDATE execution_profile_checks SET status='running',result_json=? WHERE version_id=?",
                       (json.dumps({'private_runtime': private}), self.version_id))
        return lease, harness

    def test_dead_profile_test_with_unexpired_lease_resolves_at_startup(self):
        lease, harness = self.plant('runtime:deadbeef:0', 600000)  # the dead owner's lease is still valid
        report = self.sessions.start()
        self.assertEqual(report['status'], 'running')
        end = time.monotonic() + 5
        while harness.poll() is None and time.monotonic() < end:
            time.sleep(0.05)
        self.assertIsNotNone(harness.poll())
        self.assertEqual(self.world.containers, {})
        self.assertEqual(self.jobs.get(lease.job.id).state, 'failed')
        self.assertEqual(self.real_profiles.get_version(self.version_id)['check']['status'], 'interrupted')

    def test_dead_profile_test_is_contained_then_resolved_without_rerun(self):
        lease, harness = self.plant('runtime:deadbeef:0', 1000)
        time.sleep(1.1)  # the dead owner's lease expires; never stolen from metadata alone
        report = self.sessions.start()
        self.assertEqual(report['status'], 'running')
        end = time.monotonic() + 5
        while harness.poll() is None and time.monotonic() < end:
            time.sleep(0.05)
        self.assertIsNotNone(harness.poll())
        self.assertEqual(self.world.containers, {})
        self.assertEqual(self.jobs.get(lease.job.id).state, 'failed')
        check = self.real_profiles.get_version(self.version_id)['check']
        self.assertEqual(check['status'], 'interrupted')
        self.assertNotIn('private_runtime', json.dumps(self.real_profiles.get_version(self.version_id)))

    def test_live_local_profile_test_container_is_never_swept(self):
        owner = sorted(self.sessions.runtime.worker_ids())[0]
        lease, harness = self.plant(owner, 600000)
        self.sessions.start()
        self.sessions.runtime.reconcile()
        self.sessions.runtime.reconcile(startup=True)
        self.assertIsNone(harness.poll())
        self.assertTrue(any(c['session'] == lease.job.id and c['running'] for c in self.world.containers.values()))
        self.assertEqual(self.jobs.get(lease.job.id).state, 'leased')


if __name__ == '__main__':
    unittest.main()
