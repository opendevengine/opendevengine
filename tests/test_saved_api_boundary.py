"""The HTTP layer cannot supply privileged model provenance or skip version checks."""
from unittest import TestCase
from opendevengine.api import FeatureAPI
from opendevengine.platform import Platform


class Plans:
    def __init__(self):self.calls=[]
    def create(self,conversation_id,content):
        self.calls.append((conversation_id,content));return {'id':'p','content_hash':'server-derived'}
    def save_version(self,plan_id,**kwargs):
        self.calls.append((plan_id,kwargs));return {'id':'v2','parent_version_id':kwargs['parent_version_id']}
    def export(self,plan_id,version_id=None):return {'plan_version_id':version_id,'content_hash':'original','markdown':'exact version'}


class Conversations:
    def __init__(self):self.calls=[]
    def send(self,conversation_id,**kwargs):self.calls.append((conversation_id,kwargs));return {'run_id':'r','state':'queued'}
    def list_messages(self,conversation_id,**kwargs):self.calls.append((conversation_id,kwargs));return {'items':[],'next_cursor':None}
    def list_runs(self,conversation_id,**kwargs):return self.list_messages(conversation_id,**kwargs)
    def export(self,conversation_id,**kwargs):self.calls.append((conversation_id,kwargs));return {'conversation_id':conversation_id,'markdown':'page','next_cursor':'more'}


class AskAPIBoundaryTests(TestCase):
    def setUp(self):
        self.platform=Platform.open(None);self.addCleanup(self.platform.close)
        self.plans,self.conversations=Plans(),Conversations()
        self.api=FeatureAPI(self.platform,None,None,conversations=self.conversations,plans=self.plans,routing=object())

    def test_manual_plan_requests_cannot_forge_model_provenance_or_bypass_parent(self):
        response=self.api.dispatch('POST','/api/plans',body={'content':{'goal':'task'}})
        self.assertEqual(response.status,201)
        for key in ['source','profile_version_id','generation','readiness','content_hash']:
            with self.assertRaises(ValueError):
                self.api.dispatch('POST','/api/plans',body={'content':{},key:'forged'})
        with self.assertRaises(ValueError):
            self.api.dispatch('POST','/api/plans/p/versions',body={'content':{}})
        response=self.api.dispatch('POST','/api/plans/p/versions',body={'content':{},'parent_version_id':'v1'})
        self.assertEqual(response.body['parent_version_id'],'v1')

    def test_answer_submissions_pin_client_identity_and_export_keeps_exact_version(self):
        response=self.api.dispatch('POST','/api/conversations/c/messages',body={'content':'question','client_request_id':'dedupe','intent':'plan'})
        self.assertEqual(response.status,202)
        self.assertEqual(self.conversations.calls[-1],('c',{'content':'question','client_request_id':'dedupe','intent':'plan'}))
        with self.assertRaises(ValueError):
            self.api.dispatch('POST','/api/conversations/c/messages',body={'content':'question'})
        response=self.api.dispatch('GET','/api/plans/p/export','version_id=v1')
        self.assertEqual(response.body['plan_version_id'],'v1')
        with self.assertRaises(ValueError):
            self.api.dispatch('GET','/api/plans/p/export','version_id=v1&version_id=v2')

    def test_message_continuation_preserves_cursor_and_bounded_limit(self):
        response=self.api.dispatch('GET','/api/conversations/c/messages','limit=25&cursor=next')
        self.assertEqual(response.body,{'items':[],'next_cursor':None})
        self.assertEqual(self.conversations.calls[-1],('c',{'limit':25,'cursor':'next'}))
        with self.assertRaises(ValueError):
            self.api.dispatch('GET','/api/conversations/c/messages','limit=10000')

    def test_history_and_export_pagination_are_callable_without_truncation(self):
        response=self.api.dispatch('GET','/api/conversations/c/runs','limit=25&cursor=older')
        self.assertEqual(self.conversations.calls[-1],('c',{'limit':25,'cursor':'older'}))
        response=self.api.dispatch('GET','/api/conversations/c/export','cursor=page2')
        self.assertEqual(response.body['next_cursor'],'more')
        self.assertEqual(self.conversations.calls[-1],('c',{'limit':100,'cursor':'page2'}))
