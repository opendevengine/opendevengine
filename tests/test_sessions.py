"""Session service behavior over real SQLite/jobs/GitHub/Index/Plan services.

Agent, runner and profile producer are injected doubles (see test_sessions_fixture);
these tests prove orchestration, fencing and persistence, not inference or Docker.
"""
import json
import os
import time
import unittest

from opendevengine.platform.errors import IdempotencyConflict
from opendevengine.sessions import SessionError, TERMINAL
from opendevengine.sessions.workspace import Workspace, broker_exclusions, publication_rejection, review_changes
from test_sessions_fixture import IMAGE_ID, SessionCase, ScriptedAgent


def ok(stdout=''):
    return lambda files: {'exit_code': 0, 'stdout': stdout, 'stderr': '', 'timed_out': False, 'truncated': False}


class LaunchTests(SessionCase):
    def test_launch_is_idempotent_frozen_and_single_active(self):
        plan, version = self.ready_plan()
        first = self.sessions.launch(version['id'], version['content_hash'], 'start-key-1')
        self.assertEqual(first['state'], 'queued')
        self.assertEqual(self.sessions.launch(version['id'], version['content_hash'], 'start-key-1'), first)
        with self.assertRaises(SessionError) as caught:
            self.sessions.launch(version['id'], 'f' * 64, 'start-key-1')
        self.assertEqual(caught.exception.code, 'idempotency_conflict')
        with self.assertRaises(SessionError) as caught:
            self.sessions.launch(version['id'], version['content_hash'], 'start-key-2')
        self.assertEqual(caught.exception.code, 'session_active')
        with self.store.read() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 1)
            job = db.execute("SELECT * FROM jobs WHERE kind='session.run'").fetchone()
            row = db.execute('SELECT * FROM sessions').fetchone()
            repo = db.execute('SELECT * FROM session_repositories').fetchone()
        self.assertEqual((job['effect'], job['max_attempts']), ('external', 1))
        snapshot = json.loads(row['profile_snapshot_json'])
        self.assertEqual((snapshot['profile_version_id'], snapshot['image_id']), ('epv_1', IMAGE_ID))
        self.assertEqual(json.loads(row['limits_json'])['agent']['max_turns'], 5)
        self.assertEqual(json.loads(row['publication_json'])['policy'], 'manual_draft')
        self.assertEqual((repo['role'], repo['base_sha'], repo['session_branch']), ('write', 'c' * 40, 'opendevengine/' + first['session_id']))
        # A later profile change cannot alter the frozen launch snapshot.
        self.profiles.version['config']['agent_limits']['max_turns'] = 40
        self.assertEqual(self.sessions.get(first['session_id'])['limits']['agent']['max_turns'], 5)

    def test_hash_and_superseded_versions_fail_before_any_session(self):
        plan, version = self.ready_plan()
        with self.assertRaises(SessionError) as caught:
            self.sessions.launch(version['id'], '0' * 64, 'k-hash')
        self.assertEqual(caught.exception.code, 'plan_hash_mismatch')
        content = self.content(requirements=['add returns a + b', 'keep tests'])
        newer = self.plans.save_version(plan['id'], content, version['id'])['version']
        with self.assertRaises(SessionError) as caught:
            self.sessions.launch(version['id'], version['content_hash'], 'k-old')
        self.assertEqual(caught.exception.code, 'plan_not_ready')
        self.assertIn('superseded', caught.exception.details)
        self.assertEqual(self.sessions.launch(newer['id'], newer['content_hash'], 'k-new')['state'], 'queued')

    def test_preflight_blocks_unsupported_scope_and_policy_before_provisioning(self):
        content = self.plans.validate(self.content())
        self.assertEqual(self.sessions.preflight(content)['blocking_reasons'], [])
        execution = self.sessions.preflight(content)['execution']
        self.assertEqual(execution['destination']['kind'], 'remote')
        multi = dict(content, repositories=content['repositories'] + [dict(content['repositories'][0], repository_id='gh:2')])
        self.assertIn('multi_repository_write_unsupported', self.sessions.preflight(multi)['blocking_reasons'])
        local = self.index.register_local('local-copy', self.tmp.name)['repository_id']
        as_local = dict(content, repositories=[dict(content['repositories'][0], repository_id=local)])
        self.assertTrue({'repository_unavailable', 'local_write_unsupported'} & set(self.sessions.preflight(as_local)['blocking_reasons']))
        self.assertIn('citations_required', self.sessions.preflight(dict(content, citations=[]))['blocking_reasons'])
        self.providers.update_settings(local_only=True)
        self.sessions._preflight_cache.clear()
        self.assertIn('execution_local_only', self.sessions.preflight(content)['blocking_reasons'])
        self.providers.update_settings(local_only=False)
        self.profiles.version['config']['allow_external_source'] = False
        self.sessions._preflight_cache.clear()
        self.assertIn('execution_external_source_denied', self.sessions.preflight(content)['blocking_reasons'])
        self.profiles.version['config']['allow_external_source'] = True
        self.profiles.error = 'execution_test_required'
        self.sessions._preflight_cache.clear()
        self.assertIn('execution_test_required', self.sessions.preflight(content)['blocking_reasons'])
        self.profiles.error = None
        self.github.update_selection(self.repo, index_enabled=True, implementation_enabled=False)
        self.sessions._preflight_cache.clear()
        self.assertIn('implementation_not_permitted', self.sessions.preflight(content)['blocking_reasons'])
        self.assertEqual(self.world.provisioned, [])
        self.assertEqual(self.world.runs, [])

    def test_queued_stop_cancels_without_provisioning_and_is_idempotent(self):
        _, version = self.ready_plan()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-stop-q')['session_id']
        view = self.sessions.stop(sid)
        self.assertEqual(view['state'], 'cancelled')
        self.assertEqual(self.sessions.stop(sid)['state'], 'cancelled')
        self.sessions.start()
        time.sleep(0.3)
        self.assertEqual(self.world.provisioned, [])
        with self.store.read() as db:
            self.assertEqual(db.execute("SELECT state FROM jobs WHERE kind='session.run'").fetchone()[0], 'cancelled')
        types = [e['type'] for e in self.all_events(sid)]
        self.assertEqual(types.count('stop.requested'), 1)


class RunTests(SessionCase):
    def script_fix(self):
        self.world.commands['python test_calc.py'] = lambda files: (
            {'exit_code': 0, 'stdout': 'ok', 'stderr': '', 'timed_out': False, 'truncated': False}
            if b'a + b' in files['calc.py'] else
            {'exit_code': 1, 'stdout': '', 'stderr': 'AssertionError token=abc123secret', 'timed_out': False, 'truncated': False})
        self.world.commands['echo leak > .env.local; mkdir -p .git'] = lambda files: (
            files.update({'.env.local': b'AWS=AKIAABCDEFGHIJKLMNOP\n', '.git/config': b'[core]\n'}),
            {'exit_code': 0, 'stdout': '', 'stderr': '', 'timed_out': False, 'truncated': False})[1]
        self.world.script = [
            ('read_file', {'path': 'calc.py'}),
            ('read_file', {'path': '.env'}),
            ('write_file', {'path': '.git/hooks/pre-commit', 'content': 'x'}),
            ('write_file', {'path': 'config/token.txt', 'content': 'shadow'}),
            ('write_file', {'path': 'calc.py', 'content': 'def add(a, b):\n    return a + b\n'}),
            ('write_file', {'path': 'notes/new.md', 'content': 'added\n'}),
            ('delete_file', {'path': 'README.md'}),
            ('run_command', {'argv': ['sh', '-c', 'echo leak > .env.local; mkdir -p .git']}),
            ('say', 'Fixed add and ran tests'),
        ]

    def test_profile_agent_model_is_pinned_and_reaches_the_agent(self):
        self.profiles.versions['epv_1']['config']['agent_model'] = 'claude-opus-5-5'
        self.script_fix()
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-model')['session_id']
        self.wait_state(sid)
        self.assertEqual(self.world.runs[-1]['model'], 'claude-opus-5-5')
        self.assertEqual(self.sessions.get(sid)['execution']['agent_model'], 'claude-opus-5-5')

    def test_full_session_events_diff_validation_and_replay(self):
        self.script_fix()
        _, version = self.ready_plan(validation=[
            {'command': 'python test_calc.py', 'expected': 'passes', 'source': 'proposed', 'citation_ids': []},
            {'command': 'npm test', 'expected': 'passes', 'source': 'proposed', 'citation_ids': []}])
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-full')['session_id']
        final = self.wait_state(sid)
        self.assertEqual(final['state'], 'completed', final)
        self.assertEqual(final['outcome']['result'], 'reviewable')
        self.assertFalse(final['outcome']['validation']['fully_validated'])
        self.assertTrue(final['outcome']['containment']['confirmed'])
        run = self.world.runs[0]
        self.assertEqual((run['auth_mode'], run['synthetic'], run['local_only'], run['consent']), ('cli_login', False, False, True))
        self.assertIn('Plan version: ' + version['id'], run['prompt'])
        self.assertNotIn('do-not-ship', run['prompt'])
        # Inventory gate: the pre-verification call was denied without touching the runner.
        self.assertEqual(self.world.results[0][1]['code'], 'policy_denied')
        results = {name: r for name, r in self.world.results[1:] if isinstance(r, dict)}
        denied = [r for n, r in self.world.results if n == 'write_file' and not r['ok']]
        self.assertEqual(len(denied), 2)  # .git path and an omitted (excluded) original path
        self.assertFalse([r for n, r in self.world.results if n == 'read_file' and 'do-not-ship' in r.get('text', '')])
        # Source acquired only through read-only installation tokens.
        self.assertTrue(self.remote.token_permissions)
        self.assertTrue(all(p == {'contents': 'read'} for p in self.remote.token_permissions))
        diff = self.sessions.diff(sid)
        by_path = {f['path']: f for f in diff['files']}
        self.assertEqual(set(by_path), {'calc.py', 'notes/new.md', 'README.md'})
        self.assertEqual(by_path['README.md']['status'], 'deleted')
        self.assertIn('+    return a + b', by_path['calc.py']['diff'])
        rejected = {r['path']: r['reason'] for r in diff['rejected']}
        self.assertEqual(rejected['.git/config'], 'git_metadata')
        self.assertTrue(rejected['.env.local'].startswith('excluded_'))
        validations = self.sessions.validations(sid)
        statuses = [v['status'] for v in validations['items']]
        self.assertEqual(statuses, ['passed', 'unavailable'])
        self.assertEqual(validations['items'][0]['command']['argv'], ['sh', '-c', 'python test_calc.py'])
        self.assertEqual(validations['items'][0]['environment']['network'], 'none')
        events = self.all_events(sid)
        seqs = [e['seq'] for e in events]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        self.assertEqual(seqs, [e['seq'] for e in self.sessions.events(sid, limit=500)['items']])
        types = [e['type'] for e in events]
        for kind in ('session.state', 'tool.started', 'tool.finished', 'tool.denied', 'checkpoint', 'validation.finished',
                     'containment', 'usage', 'agent.summary'):
            self.assertIn(kind, types)
        states = [e['payload']['state'] for e in events if e['type'] == 'session.state' and 'previous' in e['payload']]
        self.assertEqual(states, ['queued', 'provisioning', 'running', 'validating', 'completed'])
        blob = json.dumps([events, final, diff, validations, self.sessions.artifacts(sid)])
        for secret in ('installation-secret', 'do-not-ship', 'AKIAABCDEFGHIJKLMNOP', 'abc123secret', 'socket_path', 'token_file'):
            self.assertNotIn(secret, blob)
        for container in self.world.containers:
            self.assertNotIn(container, blob)
        self.assertEqual(self.world.containers, {})  # stopped and discarded
        manifest = final['repositories'][0]['acquisition']
        self.assertEqual(manifest['files'], 5)
        with self.store.read() as db:
            base = json.loads(db.execute('SELECT base_manifest_json FROM session_repositories').fetchone()[0])
        self.assertEqual(base['omitted']['link.py'], 'symlink')
        self.assertEqual(base['omitted']['leak.py'], 'sensitive_content')
        self.assertIn('node_modules', base['omitted_dirs'])
        self.assertTrue(final['usage']['reported_cost_usd'] == 0.01 and final['usage']['cost_status'] == 'reported')

    def test_failed_and_unavailable_checks_stay_distinct(self):
        self.script_fix()
        self.world.script = [('say', 'no changes made')]
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-fail')['session_id']
        final = self.wait_state(sid)
        self.assertEqual((final['state'], final['outcome']['result']), ('completed', 'no_changes'))
        items = self.sessions.validations(sid)['items']
        self.assertEqual(items[0]['status'], 'failed')
        self.assertNotIn('abc123secret', items[0]['output'])
        self.assertFalse(final['outcome']['publication_eligible'])

    def test_runtime_runs_execution_test_jobs(self):
        self.sessions.start()
        job = self.jobs.enqueue('execution.test', {'version_id': 'epv_1'}, effect='external', max_attempts=1)
        self.wait(lambda: self.jobs.get(job.id).state == 'succeeded', message='execution.test job')
        self.assertEqual(self.profiles.tests, [job.id])


class WorkspacePolicyTests(unittest.TestCase):
    def test_publication_rejects_git_hooks_and_shadowed_omissions(self):
        manifest = {'files': {'a.py': {'sha256': 'x', 'mode': '100755'}}, 'omitted': {'secret.pem': 'sensitive', 'odd[1].txt': 'file_too_large'},
                    'omitted_dirs': {'vendor': 'generated'}, 'exclusions': ['private/']}
        self.assertEqual(publication_rejection('.git/HEAD', manifest), 'git_metadata')
        self.assertEqual(publication_rejection('sub/.GIT/config', manifest), 'git_metadata')
        self.assertEqual(publication_rejection('.husky/pre-commit', manifest), 'hook_path')
        self.assertEqual(publication_rejection('secret.pem', manifest), 'shadows_omitted')
        self.assertEqual(publication_rejection('vendor/lib.js', manifest), 'shadows_omitted')
        self.assertTrue(publication_rejection('private/x.py', manifest).startswith('excluded_'))
        self.assertIsNone(publication_rejection('src/new.py', manifest))
        import fnmatch
        patterns = broker_exclusions(manifest)
        self.assertTrue(any(fnmatch.fnmatchcase('odd[1].txt', p) for p in patterns))
        self.assertFalse(any(fnmatch.fnmatchcase('odd1.txt', p) for p in patterns))
        self.assertTrue(any(fnmatch.fnmatchcase('vendor/deep/x', p) for p in patterns))
        self.assertTrue(any(fnmatch.fnmatchcase('private/x.py', p) for p in patterns))
        workspace = Workspace('c' * 40, {'a.py': b'print(1)\n'}, manifest)
        checkpoint, patch = review_changes(workspace, {'a.py': b'print(2)\n', 'secret.pem': b'k', '.git/x': b'y', 'bin.dat': b'\0\1'})
        self.assertEqual({f['path'] for f in patch['files']}, {'a.py', 'bin.dat'})
        self.assertEqual(next(f for f in patch['files'] if f['path'] == 'a.py')['mode'], '100755')
        self.assertTrue(next(f for f in patch['files'] if f['path'] == 'bin.dat')['binary'])
        self.assertNotIn('secret.pem', checkpoint['changed'])
        self.assertEqual({r['path'] for r in patch['rejected']}, {'secret.pem', '.git/x'})


if __name__ == '__main__':
    unittest.main()
