"""Validation commands run only when the user approved them in the plan version (issue #9)."""
import json
import unittest

from opendevengine.sessions.prompt import build_prompt
from test_sessions_fixture import SessionCase


def check(**extra):
    return {'command': 'python test_calc.py', 'expected': 'passes', 'source': 'proposed', 'citation_ids': [], **extra}


class PlanApprovalTests(SessionCase):
    def test_missing_approved_is_accepted_and_warns(self):
        plan = self.plans.create(None, self.content(validation=[check()]))
        readiness = plan['version']['readiness']
        self.assertEqual(readiness['state'], 'ready', readiness)
        self.assertIn('validation_unapproved', readiness['warnings'])
        self.assertNotIn('approved', plan['version']['content']['validation'][0])

    def test_non_boolean_approved_is_rejected(self):
        with self.assertRaises(ValueError):
            self.plans.create(None, self.content(validation=[check(approved='yes')]))

    def test_manual_save_keeps_user_approval(self):
        plan = self.plans.create(None, self.content(validation=[check(), check(command='npm test')]))
        content = plan['version']['content']
        content['validation'][1]['approved'] = True
        saved = self.plans.save_version(plan['id'], content, plan['version']['id'])
        self.assertEqual([x.get('approved') for x in saved['version']['content']['validation']], [None, True])
        self.assertIn('validation_unapproved', saved['version']['readiness']['warnings'])

    def test_all_approved_has_no_warning(self):
        plan = self.plans.create(None, self.content(validation=[check(approved=True)]))
        self.assertNotIn('validation_unapproved', plan['version']['readiness']['warnings'])

    def test_model_version_is_forced_unapproved(self):
        # 'amendment' takes the same non-manual branch as 'model' without needing a provider profile row.
        plan = self.plans.create(None, self.content(validation=[check()]))
        content = plan['version']['content']
        content['validation'][0]['approved'] = True
        saved = self.plans.save_version(plan['id'], content, plan['version']['id'], source='amendment')
        self.assertIs(saved['version']['content']['validation'][0]['approved'], False)
        self.assertIs(content['validation'][0]['approved'], True)  # the caller's dict is untouched


def ok(files):
    return {'exit_code': 0, 'stdout': 'ok', 'stderr': '', 'timed_out': False, 'truncated': False}


class SessionApprovalTests(SessionCase):
    BAD = 'Run `python3 -m http.server 8000` and $(touch /tmp/x) check it.'
    FIX = 'def add(a, b):\n    return a + b\n'

    def run_session(self, key, validation, **changes):
        _, version = self.ready_plan(validation=validation, **changes)
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], key)['session_id']
        return sid, self.wait_state(sid)

    def test_unapproved_command_never_reaches_runner(self):
        called = []
        self.world.commands['python test_calc.py'] = ok
        self.world.commands[self.BAD] = lambda files: called.append(1) or ok(files)
        self.world.script = [('write_file', {'path': 'calc.py', 'content': self.FIX})]
        sid, final = self.run_session('k-approve', [check(approved=True), check(command=self.BAD, expected='x')])
        self.assertEqual(final['state'], 'completed', final)
        self.assertEqual(called, [])
        result = self.sessions.validations(sid)
        self.assertEqual([i['status'] for i in result['items']], ['passed', 'not_run'])
        self.assertEqual(result['items'][1]['environment']['reason'], 'not_approved')
        self.assertFalse(result['items'][1]['required'])
        self.assertTrue(result['items'][0]['required'])
        self.assertEqual(result['skipped_not_approved'], 1)
        self.assertFalse(final['outcome']['validation']['fully_validated'])
        started = [e for e in self.all_events(sid) if e['type'] == 'validation.started']
        self.assertEqual([e['payload']['ordinal'] for e in started], [1])

    def test_unapproved_reason_wins_when_agent_fails(self):
        self.world.script = [('fail', 'agent_error')]
        sid, final = self.run_session('k-approve-fail', [check(approved=True), check(command='npm test')])
        self.assertNotEqual(final['state'], 'completed', final)
        items = self.sessions.validations(sid)['items']
        self.assertEqual([i['environment']['reason'] for i in items], ['agent_not_completed', 'not_approved'])

    def test_skipped_checks_block_auto_publication(self):
        self.world.commands['python test_calc.py'] = ok
        self.world.commands['npm test'] = ok
        self.world.script = [('write_file', {'path': 'calc.py', 'content': self.FIX})]
        sid, final = self.run_session('k-approve-publish', [check(approved=True), check(command='npm test')],
                                      publication={'policy': 'on_success_draft'})
        self.assertEqual((final['state'], final['outcome']['result']), ('completed', 'reviewable'), final)
        self.assertFalse(final['outcome']['publication_eligible'])

    def test_all_approved_passing_checks_stay_publishable(self):
        self.world.commands['python test_calc.py'] = ok
        self.world.script = [('write_file', {'path': 'calc.py', 'content': self.FIX})]
        sid, final = self.run_session('k-approve-ok', [check(approved=True)], publication={'policy': 'on_success_draft'})
        self.assertTrue(final['outcome']['validation']['fully_validated'], final['outcome'])
        self.assertTrue(final['outcome']['publication_eligible'], final['outcome'])

    def test_agent_prompt_omits_unapproved_checks(self):
        self.world.commands['python test_calc.py'] = ok
        self.world.script = [('write_file', {'path': 'calc.py', 'content': self.FIX})]
        self.run_session('k-approve-prompt', [check(approved=True), check(command=self.BAD, expected='x')])
        prompt = self.world.runs[-1]['prompt']
        self.assertIn('`python test_calc.py`', prompt)
        self.assertNotIn(self.BAD, prompt)

    def test_retry_keeps_the_prompt_of_a_session_started_before_approval(self):
        self.world.script = [('fail', 'agent_error')]
        sid, final = self.run_session('k-approve-v1', [check(), check(command='npm test')])
        self.assertEqual(final['state'], 'failed', final)
        plan = self.plans.get(final['plan_id'], final['plan_version_id'])['version']
        with self.store.read() as db:
            row = db.execute('SELECT s.instructions_json, r.base_sha FROM sessions s JOIN session_repositories r ON r.session_id=s.id WHERE s.id=?', (sid,)).fetchone()
        old, _ = build_prompt(plan['content'], plan_version_id=plan['id'], content_hash=plan['content_hash'],
                              repository_name=self.github.get_repository(self.repo)['full_name'], base_sha=row['base_sha'],
                              instructions_version='opendevengine.session-instructions/1')
        self.assertIn('`npm test`', old)
        import hashlib
        instructions = dict(json.loads(row['instructions_json']), version='opendevengine.session-instructions/1',
                            prompt_sha256=hashlib.sha256(old.encode()).hexdigest())
        with self.store.transaction() as db:
            db.execute('UPDATE sessions SET instructions_json=? WHERE id=?', (json.dumps(instructions), sid))
        self.world.script = []
        retried = self.sessions.retry(sid, 'k-approve-v1-retry')
        self.assertTrue(retried['session_id'])


class PromptVersionTests(unittest.TestCase):
    CONTENT = {'goal': 'g', 'user_request': 'u', 'validation': [check(approved=True), check(command='npm test')]}

    def build(self, **kw):
        return build_prompt(self.CONTENT, plan_version_id='pv', content_hash='h', repository_name='o/r', base_sha='b', **kw)[0]

    def test_current_prompt_lists_only_approved_checks(self):
        prompt = self.build()
        self.assertIn('`python test_calc.py`', prompt)
        self.assertNotIn('npm test', prompt)

    def test_version_one_prompt_is_unchanged(self):
        prompt = self.build(instructions_version='opendevengine.session-instructions/1')
        self.assertIn('Validation (the service also runs these independently after you finish):\n- `python test_calc.py` expected: passes\n- `npm test` expected: passes\n', prompt)


if __name__ == '__main__':
    unittest.main()
