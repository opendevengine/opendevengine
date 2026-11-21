"""Validation commands run only when the user approved them in the plan version (issue #9)."""
import unittest

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


if __name__ == '__main__':
    unittest.main()
