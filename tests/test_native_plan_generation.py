"""Native drafts cannot invent source identities or execution/publication grants."""
import copy
import json
from unittest import TestCase
import test_ask_services as fixtures


def draft(content):
    return {
        'goal':content['goal'],
        'repositories':[{'repository_id':r['repository_id'],'role':r['role']} for r in content['repositories']],
        'citation_ids':[c['id'] for c in content['citations']],
        'findings':copy.deepcopy(content['findings']),'requirements':list(content['requirements']),
        'steps':copy.deepcopy(content['steps']),
        'validation':[{k:item[k] for k in ('command','expected','citation_ids')} for item in content['validation']],
        'risks':[],'assumptions':[],
        'open_questions':[{'text':q['text'],'blocking':q['blocking']} for q in content['open_questions']],
    }


class NativePlanTests(TestCase):
    def setUp(self):
        self.f=fixtures.AskTests(methodName='runTest');self.f.setUp();self.addCleanup(self.f.tearDown)
        f=self.f
        config=f.providers.get_version(f.profile['active_version_id'])['config']
        f.providers.update_profile(f.profile['id'],{'config':{**config,'capabilities':['chat','structured_output']}})
        f.transport.output=draft(f.content)

    def test_compact_draft_is_bound_to_exact_snapshot_and_full_user_request(self):
        f=self.f;request='Preserve snake_case and <literal> markup. Add bounded confirmation retries.'
        run=f.service.send(f.conv,request,'native-plan',intent='plan');f.work()
        self.assertEqual(f.service.get_run(run['id'])['state'],'completed')
        saved=f.service.get(f.conv)['plans'][0]['version']['content']
        self.assertEqual(saved['user_request'],request)
        self.assertEqual(saved['repositories'],f.content['repositories'])
        body=json.loads(f.transport.requests[0].body)
        evidence=json.loads(body['messages'][1]['content'])['evidence']['e1']
        self.assertEqual(saved['citations'],[{'id':'e1',**{key:evidence[key] for key in ('repository_id','snapshot_id','revision','path','start_line','end_line')}}])
        citation=saved['citations'][0]
        source=f.index.source(citation['repository_id'],citation['snapshot_id'],citation['path'],citation['start_line'],citation['end_line'],context_lines=0)
        self.assertEqual('\n'.join(source['lines']),evidence['text'])
        self.assertEqual(saved['validation'][0]['source'],'proposed')
        self.assertEqual(saved['publication'],{'policy':'manual_draft'})
        self.assertIsNone(saved['execution_context']['execution_profile_id'])
        body=json.loads(f.transport.requests[0].body)
        schema=body['format']
        self.assertNotIn('user_request',schema['properties'])
        self.assertNotIn('snapshot_id',schema['properties']['repositories']['items']['properties'])
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(schema['properties']['repositories']['maxItems'],1)
        self.assertEqual(schema['properties']['citation_ids']['maxItems'],len(json.loads(body['messages'][1]['content'])['evidence']))
        self.assertLessEqual(sum(len(m['content'].encode()) for m in body['messages'])+len(json.dumps(schema,sort_keys=True,separators=(',',':')).encode()),(8192-1024)*4-256)

    def test_invalid_provider_response_cannot_add_identities_or_grants(self):
        for mutate in (
            lambda d:d['repositories'][0].update(snapshot_id='invented'),
            lambda d:d['repositories'][0].update(repository_id='gh:999'),
            lambda d:d.update(publication={'policy':'on_success_draft'}),
            lambda d:d.update(execution_context={'execution_profile_id':'invented'}),
            lambda d:d['citation_ids'].append('invented'),
            lambda d:d['repositories'].append(copy.deepcopy(d['repositories'][0])),
        ):
            f=self.f;payload=draft(f.content);mutate(payload);f.transport.output=payload
            run=f.service.send(f.conv,'Plan confirmation retries','bad-'+str(f.transport.calls),intent='plan');f.work()
            self.assertEqual(f.service.get_run(run['id'])['state'],'failed')
            self.assertEqual(f.service.get(f.conv)['plans'],[])

    def test_no_evidence_draft_retains_blocking_question_and_unknown_finding(self):
        f=self.f;conversation=f.service.create()['id']
        f.transport.output={**draft(f.content),'repositories':[],'citation_ids':[],
                            'findings':[{'text':'Unknown: no source evidence.','citation_ids':[]}],
                            'steps':[],'validation':[],
                            'open_questions':[{'text':'Which repository should be indexed?','blocking':True}]}
        run=f.service.send(conversation,'xylophonic nonexistent zebra','unknown-native-plan',intent='plan');f.work()
        self.assertEqual(f.service.get_run(run['id'])['state'],'completed')
        version=f.service.get(conversation)['plans'][0]['version']
        self.assertIn('blocking_questions',version['readiness']['blocking_reasons'])
        self.assertIsNone(version['content']['open_questions'][0]['answer'])


    def test_chat_only_model_cannot_choose_execution_or_automatic_publication(self):
        f=self.f
        config=f.providers.get_version(f.profile['active_version_id'])['config']
        f.providers.update_profile(f.profile['id'],{'config':{**config,'capabilities':['chat']}})
        payload=copy.deepcopy(f.content)
        payload['publication']={'policy':'on_success_draft'}
        payload['execution_context']={'execution_profile_id':'model-selected','base_choices':{f.repo:'retain'}}
        f.transport.output=payload
        run=f.service.send(f.conv,'Plan confirmation retries','untrusted-user-choices',intent='plan');f.work()
        self.assertEqual(f.service.get_run(run['id'])['state'],'completed')
        saved=f.service.get(f.conv)['plans'][0]['version']['content']
        self.assertEqual(saved['publication'],{'policy':'manual_draft'})
        self.assertIsNone(saved['execution_context']['execution_profile_id'])
        self.assertEqual(saved['execution_context']['base_choices'],{})

    def test_repair_prompt_names_the_failed_rule_and_second_attempt_succeeds(self):
        f=self.f;good=draft(f.content)
        bad={**good,'findings':[{'text':'The worker retries forever.','citation_ids':[]}]}
        f.transport.output=bad
        def fix():
            if f.transport.calls==2:
                f.transport.output=good
        f.transport.callback=fix
        run=f.service.send(f.conv,'Plan confirmation retries','repair-hint',intent='plan');f.work()
        self.assertEqual(f.service.get_run(run['id'])['state'],'completed')
        self.assertEqual(f.transport.calls,2)
        repair=json.loads(f.transport.requests[1].body)['messages'][-1]['content']
        self.assertIn("start with 'Unknown:' or 'Gap:'",repair)
        self.assertNotIn('The worker retries forever.',repair)

    def test_directory_step_path_with_trailing_slash_is_normalized(self):
        f=self.f;payload=draft(f.content)
        payload['steps'][0]['paths']=['new/retry/','worker.py']
        f.transport.output=payload
        run=f.service.send(f.conv,'Plan confirmation retries','dir-path',intent='plan');f.work()
        self.assertEqual(f.service.get_run(run['id'])['state'],'completed')
        saved=f.service.get(f.conv)['plans'][0]['version']['content']
        self.assertEqual(saved['steps'][0]['paths'],['new/retry','worker.py'])
        for unsafe in ('/etc/','../up/','a//b','./a'):
            with self.subTest(unsafe=unsafe):
                payload=draft(f.content);payload['steps'][0]['paths']=[unsafe];f.transport.output=payload
                run=f.service.send(f.conv,'Plan confirmation retries','unsafe-'+unsafe,intent='plan');f.work()
                self.assertEqual(f.service.get_run(run['id'])['state'],'failed')
