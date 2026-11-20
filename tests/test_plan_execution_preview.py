"""Execution preview updates live readiness without mutating immutable plan intent."""
import copy
from unittest import TestCase
import test_ask_services as fixtures


class PlanExecutionPreviewTests(TestCase):
    def test_live_execution_metadata_is_visible_but_cannot_make_unpermitted_source_ready(self):
        f=fixtures.AskTests(methodName='runTest');f.setUp();self.addCleanup(f.tearDown)
        preview={'profile_id':'ep','profile_version_id':'epv1','limits':{'agent':{'max_budget_usd':1}},'capabilities':{'ask_user':True}}
        f.plans.execution_preflight=lambda content:{'blocking_reasons':[],'warnings':[],'execution':copy.deepcopy(preview)}
        content=copy.deepcopy(f.content);content['execution_context']={'execution_profile_id':'ep'}
        plan=f.plans.create(f.conv,content)
        self.assertEqual(plan['version']['readiness']['execution'],preview)
        self.assertIn('implementation_not_permitted',plan['version']['readiness']['blocking_reasons'])
        self.assertNotEqual(plan['version']['readiness']['state'],'ready')
        old_hash=plan['version']['content_hash'];old_content=copy.deepcopy(plan['version']['content'])
        preview['profile_version_id']='epv2';preview['limits']['agent']['max_budget_usd']=2
        current=f.plans.get(plan['id'])['version']
        self.assertEqual(current['content_hash'],old_hash);self.assertEqual(current['content'],old_content)
        self.assertEqual(current['readiness']['execution']['profile_version_id'],'epv2')

    def test_unconfigured_execution_preview_remains_unknown(self):
        f=fixtures.AskTests(methodName='runTest');f.setUp();self.addCleanup(f.tearDown)
        readiness=f.plans.create(f.conv,f.content)['version']['readiness']
        self.assertIsNone(readiness['execution'])
        self.assertIn('execution_not_configured',readiness['blocking_reasons'])
