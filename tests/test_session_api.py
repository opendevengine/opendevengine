"""Execution HTTP rejects host execution knobs and bounds replay requests."""
from unittest import TestCase
from opendevengine.api import FeatureAPI
from opendevengine.platform import Platform


class Profiles:
    def __init__(self):self.calls=[]
    def create(self,spec):self.calls.append(spec);return {'id':'ep','active_version_id':'epv'}
    def request_test(self,ident):self.calls.append(ident);return {'job_id':'check','status':'queued'}
    def list_profiles(self,**query):self.calls.append(query);return {'items':[],'next_cursor':None}
    def settings(self):return {'max_active_sessions':1}


class Sessions:
    def __init__(self):self.calls=[]
    def launch(self,**data):self.calls.append(data);return {'session_id':'ses','state':'queued'}
    def events(self,ident,**query):self.calls.append((ident,query));return {'items':[],'next_seq':query['after'],'has_more':False}
    def stop(self,ident):self.calls.append(ident);return {'session_id':ident,'state':'stopping'}
    def send_message(self,ident,**data):self.calls.append((ident,data));return {'delivery':'queued'}


class ExecutionAPITests(TestCase):
    def setUp(self):
        self.platform=Platform.open(None);self.addCleanup(self.platform.close)
        self.profiles,self.sessions=Profiles(),Sessions()
        self.api=FeatureAPI(self.platform,None,None,execution_profiles=self.profiles,sessions=self.sessions)

    def test_launch_accepts_only_pinned_plan_intent_never_commands_paths_or_grants(self):
        data={'plan_version_id':'pv','content_hash':'hash','idempotency_key':'key'}
        result=self.api.dispatch('POST','/api/sessions',body=data)
        self.assertEqual(result.status,202);self.assertEqual(result.body['session_id'],'ses')
        self.assertEqual(self.sessions.calls,[data])
        for key in ('command','host_path','container_id','mounts','repository_ids','execution_profile_id','allow_external_source'):
            with self.assertRaises(ValueError):self.api.dispatch('POST','/api/sessions',body={**data,key:'untrusted'})
        self.assertEqual(len(self.sessions.calls),1)

    def test_event_replay_bounds_duplicate_queries_and_unknown_fields_are_rejected(self):
        result=self.api.dispatch('GET','/api/sessions/ses/events','after=42&limit=200')
        self.assertEqual(result.body['next_seq'],42)
        self.assertEqual(self.sessions.calls,[('ses',{'after':42,'limit':200})])
        for query in ('after=-1','after=1.5','after=9007199254740992','limit=201','limit=0','after=1&after=2','path=/etc/passwd'):
            with self.assertRaises(ValueError):self.api.dispatch('GET','/api/sessions/ses/events',query)
        self.assertEqual(len(self.sessions.calls),1)

    def test_profile_setup_and_checks_cannot_take_host_binaries_or_arbitrary_environment(self):
        spec={'name':'CLI','adapter':'claude_code','auth_mode':'cli_login','image':'python:3.12-slim'}
        self.assertEqual(self.api.dispatch('POST','/api/execution-profiles',body=spec).status,201)
        for key in ('binary','env','private_key','token','command','socket'):
            with self.assertRaises(ValueError):self.api.dispatch('POST','/api/execution-profiles',body={**spec,key:'untrusted'})
        result=self.api.dispatch('POST','/api/execution-profiles/ep/test',body={})
        self.assertEqual(result.status,202);self.assertEqual(result.body['status'],'queued')
        with self.assertRaises(ValueError):self.api.dispatch('POST','/api/execution-profiles/ep/test',body={'prompt':'private source'})

    def test_control_routes_reject_scope_changes_and_mutation_queries(self):
        self.assertEqual(self.api.dispatch('POST','/api/sessions/ses/stop',body={}).body['state'],'stopping')
        data={'content':'Clarify the current task','client_request_id':'one','question_id':'q'}
        self.assertEqual(self.api.dispatch('POST','/api/sessions/ses/messages',body=data).status,202)
        with self.assertRaises(ValueError):self.api.dispatch('POST','/api/sessions/ses/stop','force=true',{})
        with self.assertRaises(ValueError):self.api.dispatch('POST','/api/sessions/ses/messages',body={**data,'grants':['all']})
        self.assertIsNone(self.api.dispatch('POST','/api/sessions/ses/exec',body={'command':'anything'}))
