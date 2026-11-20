"""Generation budgets include prompt, history, source identities and output reserve."""
import json
from unittest import TestCase
import test_ask_services as fixtures
from opendevengine.platform.worker import Worker


class AskContextBudgetTests(TestCase):
    def setUp(self):
        self.f=fixtures.AskTests(methodName='runTest');self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def profile(self,context=512,output=128,structured=False):
        f=self.f
        current=f.providers.get_version(f.profile['active_version_id'])
        return f.providers.update_profile(f.profile['id'],{'config':{**current['config'],'context_limit':context,'max_output_tokens':output,'capabilities':['chat','structured_output'] if structured else ['chat']}})

    def test_small_profile_fits_full_prompt_and_identity_bearing_source(self):
        self.profile()
        f=self.f
        run=f.service.send(f.conv,'How does send_confirmation deliver mail?','bounded-source')
        f.work()
        self.assertEqual(f.service.get_run(run['id'])['state'],'completed')
        body=json.loads(f.transport.requests[0].body)
        self.assertNotIn('format',body)  # Chat-only profiles retain server validation.
        size=sum(len(m['content'].encode()) for m in body['messages'])
        self.assertLessEqual(size,(512-128)*4-256)
        payload=json.loads(body['messages'][1]['content'])
        self.assertEqual(payload['request'],'How does send_confirmation deliver mail?')
        self.assertTrue(payload['evidence'])
        for hit in payload['evidence'].values():
            self.assertEqual(set(hit),{'repository_id','snapshot_id','revision','path','start_line','end_line','text'})

    def test_many_retrieval_hits_are_packed_with_their_citation_metadata(self):
        self.profile();f=self.f
        def reader(source,revision,caps):
            return {'revision':'b'*40,'revision_kind':'git','branch':'main','files':[{'path':f'worker_{i}.py','content':b'def send_confirmation():\n    """Deliver mail with retry."""\n    return True\n'} for i in range(20)]}
        f.index.snapshot_reader=reader
        indexing=f.index.request_index(f.repo)
        Worker(f.jobs,{'index.run':f.index.run_index},worker_id='budget-index').run_once()
        self.assertEqual(f.index.get_run(indexing['id'])['state'],'completed')
        run=f.service.send(f.conv,'How does send_confirmation deliver mail?','many-sources');f.work()
        self.assertEqual(f.service.get_run(run['id'])['state'],'completed')
        body=json.loads(f.transport.requests[0].body)
        payload=json.loads(body['messages'][1]['content'])
        self.assertGreater(len(payload['evidence']),0);self.assertLess(len(payload['evidence']),12)
        self.assertLessEqual(sum(len(m['content'].encode()) for m in body['messages']),(512-128)*4-256)

    def test_history_is_bounded_without_discarding_latest_request(self):
        f=self.f
        run=f.service.send(f.conv,'How does send_confirmation work?','history-source');f.work()
        self.assertEqual(f.service.get_run(run['id'])['state'],'completed')
        with f.store.transaction() as conn:
            conn.execute("UPDATE conversation_messages SET content=? WHERE role='assistant'",('Long saved answer '*1000,))
        self.profile()
        f.transport.requests.clear()
        run=f.service.send(f.conv,'Explain send_confirmation delivery.','latest-request');f.work()
        self.assertEqual(f.service.get_run(run['id'])['state'],'completed')
        body=json.loads(f.transport.requests[0].body)
        payload=json.loads(body['messages'][1]['content'])
        self.assertEqual(payload['request'],'Explain send_confirmation delivery.')
        self.assertEqual(payload['prior_conversation'],[])
        self.assertLessEqual(sum(len(m['content'].encode()) for m in body['messages']),(512-128)*4-256)

    def test_oversized_latest_request_fails_before_transport_without_silent_clipping(self):
        self.profile();f=self.f
        content='Preserve this full request. '*100
        run=f.service.send(f.conv,content,'too-large');f.work()
        self.assertEqual(f.service.get_run(run['id'])['error_code'],'context_overflow')
        self.assertEqual(f.transport.calls,0)
        self.assertEqual(f.service.get(f.conv)['messages'][0]['content'],content)

    def test_structured_capability_uses_native_answer_schema_and_chat_only_remains_valid(self):
        self.profile(context=2048,structured=True);f=self.f
        run=f.service.send(f.conv,'How does send_confirmation work?','native-schema');f.work()
        self.assertEqual(f.service.get_run(run['id'])['state'],'completed')
        schema=json.loads(f.transport.requests[0].body)['format']
        self.assertEqual(schema['required'],['answer','citation_ids'])
        self.assertIn('e1',schema['properties']['citation_ids']['items']['enum'])
        self.assertFalse(schema['additionalProperties'])


    def test_no_evidence_native_schema_allows_only_empty_citation_ids(self):
        self.profile(context=2048,structured=True);f=self.f
        f.transport.output={'answer':'Source behavior is unknown: no evidence.','citation_ids':[]}
        conversation=f.service.create()['id']
        run=f.service.send(conversation,'xylophonic nonexistent zebra','no-evidence');f.work()
        self.assertEqual(f.service.get_run(run['id'])['state'],'completed')
        body=json.loads(f.transport.requests[0].body)
        self.assertEqual(body['format']['properties']['citation_ids']['maxItems'],0)
        self.assertNotIn('enum',body['format']['properties']['citation_ids']['items'])

    def test_plan_intent_keeps_server_validation_and_obeys_configured_prompt_budget(self):
        self.profile(context=2048,output=512,structured=True);f=self.f
        from test_native_plan_generation import draft
        f.transport.output=draft(f.content)
        run=f.service.send(f.conv,'Add bounded retries to send_confirmation.','bounded-plan',intent='plan');f.work()
        self.assertEqual(f.service.get_run(run['id'])['state'],'completed')
        body=json.loads(f.transport.requests[0].body)
        self.assertIn('format',body)
        self.assertLessEqual(sum(len(m['content'].encode()) for m in body['messages']),(2048-512)*4-256)
        self.assertEqual(len(f.service.get(f.conv)['plans']),1)
