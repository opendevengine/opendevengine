"""Real SQLite, IndexService and ProviderService; injected immutable source and transport."""
import copy
import json
from pathlib import Path
import tempfile
import time
import threading
import unittest
from unittest.mock import patch
from opendevengine.ask import RoutingService, PlanService, ConversationService
from opendevengine.platform.db import Store
from opendevengine.platform.jobs import JobQueue
from opendevengine.platform.worker import Worker, JobContext, JobInterrupted
from opendevengine.platform.errors import InvalidTransition, IdempotencyConflict, LeaseLost, StorageBusy
from opendevengine.providers import ProviderService, TransportResponse
from opendevengine.indexing import IndexService


class Transport:
    def __init__(self):
        self.output={'answer':'The worker sends confirmation mail.','citation_ids':['e1']}
        self.callback=None
        self.calls=0
        self.requests=[]
        self.cancel_events=[]
    def send(self,request,*,cancel_event=None):
        self.calls+=1
        self.requests.append(request)
        self.cancel_events.append(cancel_event)
        if self.callback:
            self.callback()
        payload={'done':True,'message':{'content':json.dumps(self.output)},'prompt_eval_count':10,'eval_count':8}
        if request.url.endswith('/chat/completions'):
            payload={'choices':[{'message':{'content':json.dumps(self.output)}}],'usage':{'prompt_tokens':10,'completion_tokens':8}}
        return TransportResponse(200,{},json.dumps(payload).encode())


class AskTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.store=Store.open(self.root/'data'); self.jobs=JobQueue(self.store)
        self.transport=Transport(); self.providers=ProviderService(self.store,None,transport=self.transport)
        self.profile=self.providers.create_profile({'name':'synthetic','adapter':'ollama','endpoint':'http://localhost:11434','model':'injected','config':{'capabilities':['chat']}})
        for role in ('ask','planning'):
            self.providers.assign_role(role,self.profile['id'])
        self.revision='a'*40
        def reader(source,revision,caps):
            return {'revision':revision or self.revision,'revision_kind':'git','branch':'main','files':[{'path':'worker.py','content':b'def send_confirmation():\n    """Send order confirmation mail with retry delivery."""\n    return True\n'},{'path':'README.md','content':b'# Mail\nOrder fulfillment notifications and confirmation delivery worker.'}]}
        self.index=IndexService(self.store,self.jobs,None,self.providers,data_dir=self.root/'data',snapshot_reader=reader)
        self.repo=self.index.register_local('acme/mail-worker',self.root)['repository_id']
        run=self.index.request_index(self.repo)
        Worker(self.jobs,{'index.run':self.index.run_index},worker_id='index').run_once()
        self.assertEqual(self.index.get_run(run['id'])['state'],'completed')
        self.routing=RoutingService(self.store,self.index); self.plans=PlanService(self.store,self.index,self.providers)
        self.service=ConversationService(self.store,self.jobs,self.index,self.providers,self.routing,self.plans)
        self.conv=self.service.create(repository_ids=[self.repo])['id']
        snap=self.index.get_state(self.repo)['active_snapshot']
        self.content={'goal':'Add bounded retries','user_request':'Retry confirmation mail','repositories':[{'repository_id':self.repo,'role':'write','snapshot_id':snap['id'],'revision':snap['revision'],'branch':'main'}],'citations':[{'id':'e1','repository_id':self.repo,'snapshot_id':snap['id'],'revision':snap['revision'],'path':'worker.py','start_line':1,'end_line':3}],'findings':[{'text':'Worker sends mail','citation_ids':['e1']}],'requirements':['Preserve all confirmation messages','Bound retries'],'steps':[{'title':'Retry','description':'Add backoff','paths':['new/retry.py'],'citation_ids':['e1']}],'validation':[{'command':'python -m unittest','expected':'Tests pass','source':'proposed','citation_ids':[]}],'open_questions':[{'id':'q1','text':'Retry limit?','blocking':True,'answer':None}]}
    def tearDown(self):
        self.index.vectors.close(); self.store.close(); self.tmp.cleanup()
    def work(self):
        Worker(self.jobs,{'ask.answer':self.service.run_answer},worker_id='answer').run_once()
    def test_unresolved_route_survives_reload_until_scope_or_message(self):
        conv=self.service.create()['id']
        self.assertIsNone(self.service.get(conv)['pending_route'])
        decision=self.routing.route('mail delivery',conv)
        self.assertEqual(decision['status'],'needs_selection')
        pending=self.service.get(conv)['pending_route']
        self.assertEqual((pending['id'],pending['status'],pending['prompt']),(decision['id'],'needs_selection','mail delivery'))
        self.assertEqual([c['repository_id'] for c in pending['candidates']],[self.repo])
        reason=pending['candidates'][0]['reason']
        self.assertTrue(reason.startswith('Catalog evidence: '))
        self.assertNotRegex(reason,r'\b(?:and|the)\b')
        # A later unresolved decision replaces the earlier one.
        later=self.routing.route('confirmation mail retry',conv)
        self.assertEqual(self.service.get(conv)['pending_route']['id'],later['id'])
        # Choosing repositories resolves it.
        self.service.update_scope(conv,[self.repo],1)
        self.assertIsNone(self.service.get(conv)['pending_route'])
        # The historical decision keeps its routing outcome; consumption is recorded separately.
        history=self.routing.get(decision['id'])
        self.assertEqual((history['status'],history['selected_repository_ids'],history['next_action']),('needs_selection',[],'select_repositories'))
        self.assertEqual(history['question'],'Which repositories should provide context?')
        with self.store.read() as db:
            self.assertIsNotNone(db.execute('SELECT consumed_at FROM routing_decisions WHERE id=?',(later['id'],)).fetchone()[0])
        # Clearing the scope later does not resurrect the consumed question.
        self.service.update_scope(conv,[],2)
        self.assertIsNone(self.service.get(conv)['pending_route'])
        # Nor does choosing repositories after the prompt was abandoned, then removing them.
        third=self.service.create()['id']
        self.routing.route('mail delivery',third)
        self.service.update_scope(third,[self.repo],1); self.service.update_scope(third,[],2)
        self.assertIsNone(self.service.get(third)['pending_route'])
        # A new unresolved question after that is pending again.
        fresh=self.routing.route('mail delivery again',third)
        self.assertEqual(self.service.get(third)['pending_route']['id'],fresh['id'])
        # Deletion redacts the prompt, so nothing is restored afterwards.
        other=self.service.create()['id']
        self.routing.route('mail delivery',other)
        self.service.delete(other)
        self.assertIsNone(self.service.get(other)['pending_route'])
    def test_runs_report_their_intent(self):
        self.service.send(self.conv,'Plan retries','plan-intent',intent='plan')
        runs=self.service.get(self.conv)['runs']
        self.assertEqual([r['intent'] for r in runs],['plan'])
    def test_migration_14_restores_interim_resolved_decisions_as_consumed(self):
        from opendevengine.platform import migrations
        backfill=next(m for m in migrations.MIGRATIONS if m.version==14).statements[1]
        with self.store.transaction() as db:
            db.execute("INSERT INTO routing_decisions (id,conversation_id,prompt,selected_json,candidates_json,catalog_generation,status,created_at) VALUES ('route_interim',NULL,'mail delivery','[]','[]','g','resolved',7)")
            db.execute("INSERT INTO routing_decisions (id,conversation_id,prompt,selected_json,candidates_json,catalog_generation,status,created_at) VALUES ('route_real',NULL,'acme/mail-worker','[\"gh:1\"]','[]','g','resolved',8)")
            db.execute("INSERT INTO routing_decisions (id,conversation_id,prompt,selected_json,candidates_json,catalog_generation,status,created_at) VALUES ('route_purged',NULL,'','[]','[]','g','no_match',9)")
            db.execute(backfill)
            rows={r['id']:(r['status'],r['consumed_at']) for r in db.execute("SELECT id,status,consumed_at FROM routing_decisions WHERE id LIKE 'route_%'")}
        self.assertEqual(rows['route_interim'],('needs_selection',7))
        self.assertEqual(rows['route_real'],('resolved',None))
        self.assertEqual(rows['route_purged'],('no_match',None))
    def test_routing_pins_collision_missing_revoked(self):
        self.assertEqual(self.routing.route('acme/mail-worker')['selected_repository_ids'],[self.repo])
        self.assertEqual(self.routing.route('mail delivery')['status'],'needs_selection')
        self.assertEqual(self.routing.route('xylophonic nonexistent')['status'],'no_match')
        d=self.routing.route('mail',pinned_repository_ids=['missing'])
        self.assertEqual(d['status'],'needs_selection'); self.assertEqual(d['selected_repository_ids'],[])
        d=self.routing.route('mail',pinned_repository_ids=[self.repo])
        self.index.set_local_selection(self.repo,False)
        self.assertEqual(self.routing.get(d['id'])['candidates'],[])
    def test_versions_hashes_conflicts_readiness_and_delete(self):
        p=self.plans.create(self.conv,self.content); a=p['version']
        self.assertIn('blocking_questions',a['readiness']['blocking_reasons'])
        self.assertIn('implementation_not_permitted',a['readiness']['blocking_reasons'])
        self.assertIn('execution_not_configured',a['readiness']['blocking_reasons'])
        content=copy.deepcopy(self.content); content['requirements'].append('Retain telemetry')
        b=self.plans.save_version(p['id'],content,a['id'])['version']
        self.assertNotEqual(a['content_hash'],b['content_hash'])
        self.assertEqual(self.plans.get(p['id'],a['id'])['version']['content']['requirements'],self.content['requirements'])
        with self.assertRaises(InvalidTransition): self.plans.save_version(p['id'],content,a['id'])
        self.assertIn('Retain telemetry',self.plans.export(p['id'])['markdown'])
        self.service.update_scope(self.conv,[self.repo],1)
        self.assertIn('scope_changed',self.plans.get(p['id'])['version']['readiness']['blocking_reasons'])
        self.plans.delete(p['id'])
        self.assertNotIn('telemetry',self.plans.export(p['id'])['markdown'])
        with self.store.read() as db:
            self.assertIsNone(db.execute('SELECT content_json FROM plan_versions WHERE id=?',(a['id'],)).fetchone()[0])
    def test_strict_validator(self):
        for mutate in (lambda c:c.update(ready=True),lambda c:c['steps'][0]['paths'].append('../escape'),lambda c:c['citations'][0].update(revision='b'*40),lambda c:c['findings'][0].update(citation_ids=['invented']),lambda c:c.update(execution_context={'skills':['grant']})):
            c=copy.deepcopy(self.content); mutate(c)
            with self.assertRaises(ValueError): self.plans.create(self.conv,c)
    def test_durable_idempotent_generation(self):
        r=self.service.send(self.conv,'confirmation mail','request-1')
        self.assertEqual(r,self.service.send(self.conv,'confirmation mail','request-1'))
        with self.assertRaises(IdempotencyConflict): self.service.send(self.conv,'different','request-1')
        # Recreate services before queue consumption: persisted job is sufficient.
        self.service=ConversationService(self.store,self.jobs,self.index,self.providers,self.routing,self.plans)
        self.work()
        self.assertEqual(self.service.get_run(r['id'])['state'],'completed')
        self.assertEqual(self.transport.calls,1)
        messages=self.service.get(self.conv)['messages']
        self.assertEqual(len(messages),2); self.assertEqual(messages[1]['citations'][0]['path'],'worker.py')
        self.assertEqual(messages[1]['profile_version_id'],self.profile['active_version_id'])
        self.index.set_local_selection(self.repo,False)
        self.assertNotIn('worker sends',self.service.export(self.conv)['markdown'].lower())
    def test_invalid_output_repair_then_retry(self):
        self.transport.output={'answer':'secret invented','citation_ids':['invented']}
        r=self.service.send(self.conv,'confirmation','fail'); self.work()
        self.assertEqual(self.transport.calls,2)
        self.assertEqual(self.service.get_run(r['id'])['state'],'failed')
        self.transport.output={'answer':'Worker sends mail','citation_ids':['e1']}
        self.service.retry(r['id']); self.work()
        self.assertEqual(self.service.get_run(r['id'])['state'],'completed')
        self.assertEqual(len(self.service.get(self.conv)['messages']),2)
    def test_cancel_scope_and_lease_fences(self):
        for action in ('cancel','scope','lease'):
            r=self.service.send(self.conv,'confirmation',action)
            def callback():
                if action=='cancel': self.service.cancel(r['id'])
                elif action=='scope': self.service.update_scope(self.conv,[self.repo],1)
                else:
                    with self.store.transaction() as db: db.execute('UPDATE jobs SET lease_expires_at=0 WHERE id=?',(r['job_id'],))
            self.transport.callback=callback; self.work(); self.transport.callback=None
            self.assertNotEqual(self.service.get_run(r['id'])['state'],'completed')
            if action=='lease':
                self.service.cancel(r['id'])
        self.assertTrue(all(m['state']!='complete' for m in self.service.get(self.conv)['messages'] if m['role']=='assistant'))
    def test_shutdown_yields_without_failure(self):
        r=self.service.send(self.conv,'confirmation','shutdown')
        def interrupt(): raise JobInterrupted()
        self.transport.callback=interrupt; self.work()
        self.assertEqual(self.jobs.get(r['job_id']).state,'queued')
        self.assertEqual(self.service.get_run(r['id'])['state'],'running')
        self.transport.callback=None; self.work()
        self.assertEqual(self.service.get_run(r['id'])['state'],'completed')
    def test_plan_job_and_purge(self):
        self.transport.output=self.content
        r=self.service.send(self.conv,'Plan confirmation retries','plan',intent='plan'); self.work()
        self.assertEqual(self.service.get_run(r['id'])['state'],'completed')
        p=self.plans.list_plans(self.conv)['items'][0]
        self.assertEqual(p['version']['source'],'model')
        self.assertEqual(self.service.send(self.conv,'Plan confirmation retries','plan',intent='plan')['id'],r['id'])
        self.assertEqual(len(self.plans.list_plans(self.conv)['items']),1)
        self.service.purge_repository(self.repo)
        self.assertNotIn('confirmation',self.service.export(self.conv)['markdown'])
        self.assertIsNone(self.plans.get(p['id'])['version']['content'])
    def test_unassigned_role_retains_user(self):
        with self.store.transaction() as db: db.execute("DELETE FROM model_role_assignments WHERE role='ask'")
        r=self.service.send(self.conv,'confirmation','unassigned'); self.work()
        self.assertEqual(self.service.get_run(r['id'])['error_code'],'role_unassigned')
        self.assertEqual(self.service.get(self.conv)['messages'][0]['content'],'confirmation')
    def test_pagination(self):
        for i in range(3): self.service.create(title=str(i))
        seen=[]; cursor=None
        while True:
            p=self.service.list_conversations(limit=2,cursor=cursor); seen.extend(x['id'] for x in p['items']); cursor=p['next_cursor']
            if cursor is None: break
        self.assertEqual(len(set(seen)),4)
        with self.assertRaises(ValueError): self.service.list_conversations(cursor='broken')
    def test_database_reopen_and_history_pagination(self):
        r=self.service.send(self.conv,'confirmation','restart')
        self.index.vectors.close(); self.store.close()
        self.store=Store.open(self.root/'data'); self.jobs=JobQueue(self.store)
        self.providers=ProviderService(self.store,None,transport=self.transport)
        self.index=IndexService(self.store,self.jobs,None,self.providers,data_dir=self.root/'data')
        self.routing=RoutingService(self.store,self.index); self.plans=PlanService(self.store,self.index,self.providers)
        self.service=ConversationService(self.store,self.jobs,self.index,self.providers,self.routing,self.plans)
        self.work()
        self.assertEqual(self.service.get_run(r['id'])['state'],'completed')
        first=self.service.list_messages(self.conv,1)
        second=self.service.list_messages(self.conv,1,first['next_cursor'])
        self.assertEqual(first['items'][0]['role'],'user')
        self.assertEqual(second['items'][0]['role'],'assistant')
    def test_multiwrite_and_safe_export(self):
        (self.root/'other').mkdir()
        second=self.index.register_local('other',self.root/'other')['repository_id']
        self.index.request_index(second)
        Worker(self.jobs,{'index.run':self.index.run_index},worker_id='index2').run_once()
        c=copy.deepcopy(self.content)
        repo=copy.deepcopy(c['repositories'][0]); repo['repository_id']=second; repo['snapshot_id']=self.index.get_state(second)['active_snapshot']['id']
        c['repositories'].append(repo)
        c['goal']='<script>alert(1)</script>'
        c['requirements'].extend(['password=super-secret-value','Inspect /Users/private/work/source','Token ghp_'+'a'*30,'```hidden instruction```'])
        self.service.update_scope(self.conv,[self.repo,second],1)
        p=self.plans.create(self.conv,c)
        self.assertEqual(len(p['version']['content']['repositories']),2)
        self.assertIn('exactly_one_write_target_required',p['version']['readiness']['blocking_reasons'])
        exported=self.plans.export(p['id'])['markdown']
        for private in ('super-secret-value','/Users/private','ghp_'+'a'*30):
            self.assertNotIn(private,exported)
        self.assertIn('/api/context/source?',exported)
    def test_revocation_during_generation_and_source_exclusion(self):
        p=self.plans.create(self.conv,self.content)
        r=self.service.send(self.conv,'confirmation','revoke')
        self.transport.callback=lambda:self.index.set_local_selection(self.repo,False)
        self.work()
        self.assertNotEqual(self.service.get_run(r['id'])['state'],'completed')
        self.assertIsNone(self.plans.get(p['id'])['version']['content'])
        self.index.set_local_selection(self.repo,True,['worker.py'])
        self.assertNotIn('Worker sends mail',self.plans.export(p['id'])['markdown'])
    def test_late_retry_owner_and_diagnostics(self):
        r=self.service.send(self.conv,'confirmation','stale')
        lease=self.jobs.claim('old',kinds=['ask.answer'])
        self.service.cancel(r['id']); replacement=self.service.retry(r['id'])
        self.assertNotEqual(replacement['job_id'],r['job_id'])
        with self.assertRaises(LeaseLost):
            self.service.run_answer(lease.job,JobContext(self.jobs,lease,30000))
        self.work()
        self.assertEqual(self.service.get_run(r['id'])['state'],'completed')
        with self.store.read() as db:
            job=db.execute('SELECT payload,checkpoint,result,last_error_message FROM jobs WHERE id=?',(replacement['job_id'],)).fetchone()
            self.assertNotIn('confirmation',str(tuple(job)))
    def test_unscoped_explicit_route_persists_scope(self):
        c=self.service.create()['id']
        r=self.service.send(c,'Explain acme/mail-worker confirmation','route'); self.work()
        self.assertEqual(self.service.get_run(r['id'])['state'],'completed')
        self.assertEqual(self.service.get(c)['repository_ids'],[self.repo])
        self.assertEqual(self.service.get(c)['scope_revision'],2)
    def test_commit_before_queue_ack_replay_has_no_duplicate_plan(self):
        self.transport.output=self.content
        r=self.service.send(self.conv,'confirmation plan','ack',intent='plan')
        lease=self.jobs.claim('first',kinds=['ask.answer'])
        self.service.run_answer(lease.job,JobContext(self.jobs,lease,30000))
        self.assertEqual(self.service.get_run(r['id'])['state'],'completed')
        # Simulate shutdown after the domain transaction but before queue acknowledgement.
        self.jobs.release_safe(lease)
        self.work()
        self.assertEqual(self.jobs.get(r['job_id']).state,'succeeded')
        self.assertEqual(self.transport.calls,1)
        self.assertEqual(len(self.plans.list_plans(self.conv)['items']),1)

    def test_history_source_policy_remote_disallows_without_current_hits(self):
        first=self.service.send(self.conv,'confirmation','local-turn'); self.work()
        self.assertEqual(self.service.get_run(first['id'])['state'],'completed')
        self.assertEqual(self.index.search('rephrase',[self.repo])['hits'],[])
        remote=self.providers.create_profile({'name':'remote-no-source','adapter':'openai_compatible','endpoint':'https://model.example.invalid/v1','model':'remote-model','config':{'capabilities':['chat'],'allow_external_source':False}})
        self.providers.update_settings(local_only=False)
        self.providers.assign_role('ask',remote['id'])
        calls=self.transport.calls
        followup=self.service.send(self.conv,'rephrase','remote-turn'); self.work()
        self.assertEqual(self.service.get_run(followup['id'])['error_code'],'source_sharing_not_allowed')
        self.assertEqual(self.transport.calls,calls,'Policy must stop before injected remote transport')
        self.assertEqual(self.service.get_run(first['id'])['destination']['kind'],'local')

    def test_long_generation_renews_without_application_wrapper(self):
        r=self.service.send(self.conv,'confirmation','slow')
        observed=[]
        def slow():
            time.sleep(1.5)
            observed.append(self.jobs.claim('rival',kinds=['ask.answer'],lease_ms=1000))
        self.transport.callback=slow
        Worker(self.jobs,{'ask.answer':self.service.run_answer},worker_id='slow',lease_ms=1000).run_once()
        self.assertEqual(observed,[None])
        result=self.service.get_run(r['id'])
        self.assertEqual(result['state'],'completed')
        self.assertEqual(result['model'],'injected')
        self.assertEqual(result['profile_version_id'],self.profile['active_version_id'])
        self.assertEqual(result['destination'],{'kind':'local','host':'localhost:11434'})
        assistant=self.service.get(self.conv)['messages'][1]
        self.assertEqual(assistant['model'],'injected')
        self.assertEqual(assistant['destination'],result['destination'])
        self.assertFalse(any(t.name=='ask-lease-renewal' for t in threading.enumerate()))

    def test_expired_last_attempt_reconciles_without_manual_cancel(self):
        r=self.service.send(self.conv,'confirmation','expire')
        with self.store.transaction() as db:
            db.execute('UPDATE jobs SET max_attempts=1 WHERE id=?',(r['job_id'],))
        def expire():
            with self.store.transaction() as db:
                db.execute('UPDATE jobs SET lease_expires_at=0 WHERE id=?',(r['job_id'],))
            self.jobs.reconcile_expired()
        self.transport.callback=expire; self.work(); self.transport.callback=None
        self.assertEqual(self.jobs.get(r['job_id']).state,'failed')
        result=self.service.get_run(r['id'])
        self.assertEqual(result['state'],'failed'); self.assertEqual(result['error_code'],'lease_expired')
        replacement=self.service.send(self.conv,'confirmation','after-expiry'); self.work()
        self.assertEqual(self.service.get_run(replacement['id'])['state'],'completed')

    def test_send_reconciles_terminal_queue_and_queued_cancel_timestamp(self):
        r=self.service.send(self.conv,'confirmation','terminal')
        self.jobs.cancel(r['job_id'])
        next_run=self.service.send(self.conv,'confirmation','next')
        self.assertEqual(self.service.get_run(r['id'])['state'],'cancelled')
        self.service.cancel(next_run['id'])
        job=self.jobs.get(next_run['job_id'])
        self.assertEqual(job.state,'cancelled'); self.assertIsNotNone(job.finished_at)
        self.assertTrue(job.cancel_requested)

    def test_generation_reclamation_fences_old_token(self):
        r=self.service.send(self.conv,'confirmation','reclaim'); replacement=[]
        def reclaim():
            with self.store.transaction() as db:
                db.execute('UPDATE jobs SET lease_expires_at=0 WHERE id=?',(r['job_id'],))
            replacement.append(self.jobs.claim('new-owner',kinds=['ask.answer'],lease_ms=1000))
            time.sleep(.5)
        self.transport.callback=reclaim
        Worker(self.jobs,{'ask.answer':self.service.run_answer},worker_id='old-owner',lease_ms=1000).run_once()
        self.assertNotEqual(self.service.get_run(r['id'])['state'],'completed')
        self.transport.callback=None
        lease=replacement[0]
        self.service.run_answer(lease.job,JobContext(self.jobs,lease,1000))
        self.jobs.complete(lease,result={'run_id':r['id']})
        self.assertEqual(self.service.get_run(r['id'])['state'],'completed')
        self.assertEqual(len(self.service.get(self.conv)['messages']),2)

    def test_full_request_and_byte_bounded_query(self):
        prompt='confirmation '+('trace detail '*450)+' retain snake_case("quoted") requirement'
        self.assertGreater(len(prompt.encode()),4000)
        r=self.service.send(self.conv,prompt,'long-prompt'); self.work()
        self.assertEqual(self.service.get_run(r['id'])['state'],'completed')
        sent=json.loads(self.transport.requests[-1].body)['messages'][-1]['content']
        self.assertEqual(json.loads(sent)['request'],prompt)
        self.assertEqual(self.service.get(self.conv)['messages'][0]['content'],prompt)
        from opendevengine.ask.common import retrieval_query
        self.assertLessEqual(len(retrieval_query('界'*5000).encode()),3000)
        with self.assertRaises(ValueError): self.service.send(self.conv,'界'*6000,'too-large')
        self.assertEqual(len(self.service.get(self.conv)['messages']),2)

    def test_paths_do_not_override_pins_or_known_scope(self):
        for prompt in ('Fix retries in src/worker.py','Explain read/write I/O client/server','Fix lib/retry.py in acme/mail-worker','Explain acme/mail-worker and imaginary/repo'):
            d=self.routing.route(prompt,pinned_repository_ids=[self.repo])
            self.assertEqual(d['selected_repository_ids'],[self.repo],prompt)
            self.assertEqual(d['status'],'resolved',prompt)
        d=self.routing.route('Explain acme/mail-worker retry in lib/retry.py')
        self.assertEqual(d['selected_repository_ids'],[self.repo])
        d=self.routing.route('Fix src/worker.py',conversation_id=self.conv)
        self.assertEqual(d['selected_repository_ids'],[self.repo])

    def test_plain_export_cursor_and_bounded_run_history(self):
        r=self.service.send(self.conv,'confirmation snake_case("quoted")','export'); self.work()
        first=self.service.export(self.conv,limit=1)
        self.assertIn('snake_case("quoted")',first['markdown'])
        self.assertNotIn('snake\\_case',first['markdown'])
        second=self.service.export(self.conv,limit=1,cursor=first['next_cursor'])
        self.assertIn('assistant:',second['markdown']); self.assertIsNone(second['next_cursor'])
        # Minimal duplicate domain rows exercise >100 run pagination without 100 model calls.
        with self.store.transaction() as db:
            for i in range(101):
                uid='history-user-'+str(i); aid='history-assistant-'+str(i); stamp=100+i
                for mid,role in ((uid,'user'),(aid,'assistant')):
                    db.execute("INSERT INTO conversation_messages (id,conversation_id,role,content,state,created_at) VALUES (?,?,?,?,?,?)",(mid,self.conv,role,'history','complete',stamp))
                db.execute("INSERT INTO answer_runs (id,conversation_id,user_message_id,assistant_message_id,scope_revision,scope_json,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",('history-run-'+str(i),self.conv,uid,aid,1,'[]','completed',stamp,stamp))
        c=self.service.get(self.conv)
        self.assertEqual(len(c['runs']),100); self.assertTrue(c['runs_truncated'])
        remaining=self.service.list_runs(self.conv,100,c['runs_next_cursor'])
        self.assertEqual(len(remaining['items']),2); self.assertIsNone(remaining['next_cursor'])
        self.assertNotIn('secret_ref',json.dumps(c['runs']))

    def test_revocation_has_availability_code(self):
        r=self.service.send(self.conv,'confirmation','revoked-code')
        self.transport.callback=lambda:self.index.set_local_selection(self.repo,False)
        self.work()
        self.assertEqual(self.service.get_run(r['id'])['error_code'],'scope_unavailable')

    def test_allowed_remote_history_has_real_public_provenance(self):
        self.service.send(self.conv,'confirmation','local-source'); self.work()
        remote=self.providers.create_profile({'name':'remote-source-opt-in','adapter':'openai_compatible','endpoint':'https://model.example.invalid/v1','model':'remote-model','config':{'capabilities':['chat'],'allow_external_source':True}})
        self.providers.update_settings(local_only=False); self.providers.assign_role('ask',remote['id'])
        self.transport.output={'answer':'Current behavior is unknown without additional evidence.','citation_ids':[]}
        r=self.service.send(self.conv,'rephrase','allowed-remote'); self.work()
        run=self.service.get_run(r['id'])
        self.assertEqual(run['state'],'completed')
        self.assertEqual(run['model'],'remote-model')
        self.assertEqual(run['destination'],{'kind':'remote','host':'model.example.invalid'})
        self.assertEqual(run['profile_version_id'],remote['active_version_id'])
        transmitted=json.loads(self.transport.requests[-1].body)['messages'][-1]['content']
        self.assertIn('The worker sends confirmation mail',transmitted)
        messages=self.service.list_messages(self.conv)['items']
        assistant=next(m for m in messages if m['id']==run['assistant_message_id'])
        self.assertEqual(assistant['destination'],run['destination'])
        self.assertNotIn('secret_ref',json.dumps(run))

    def test_queue_success_without_domain_output_reconciles_and_retry_clears_provenance(self):
        r=self.service.send(self.conv,'confirmation','orphan-success')
        lease=self.jobs.claim('unfinished-handler',kinds=['ask.answer'])
        self.jobs.complete(lease,result={'run_id':r['id']})
        result=self.service.get_run(r['id'])
        self.assertEqual(result['state'],'failed'); self.assertEqual(result['error_code'],'job_terminated')
        self.transport.output={'answer':'Invented statement','citation_ids':['invalid']}
        self.service.retry(r['id']); self.work()
        result=self.service.get_run(r['id'])
        self.assertEqual(result['state'],'failed'); self.assertEqual(result['model'],'injected')
        self.service.retry(r['id'])
        self.assertIsNone(self.service.get_run(r['id'])['model'])

    def test_unknown_owner_never_falls_back_to_short_name(self):
        billing_path=self.root/'billing'; billing_path.mkdir()
        billing=self.index.register_local('owner/billing',billing_path)['repository_id']
        d=self.routing.route('Fix billing invoices in acme/billing')
        self.assertEqual(d['status'],'needs_selection')
        self.assertEqual(d['selected_repository_ids'],[])
        self.assertEqual(d['degradation_code'],'explicit_repository_unavailable')
        self.assertIn(billing,[c['repository_id'] for c in d['candidates']])
        d=self.routing.route('Fix billing invoices in acme/billing',pinned_repository_ids=[billing])
        self.assertEqual(d['selected_repository_ids'],[billing])
        self.assertEqual(d['status'],'resolved')
        for prompt in ('Fix mail-worker confirmation in other/mail-worker','Explain other/mail-worker'):
            d=self.routing.route(prompt)
            self.assertEqual(d['status'],'needs_selection',prompt)
            self.assertEqual(d['selected_repository_ids'],[],prompt)
            self.assertEqual(d['degradation_code'],'explicit_repository_unavailable')
            self.assertIn(self.repo,[c['repository_id'] for c in d['candidates']])
            self.assertIn('unavailable',d['question'])
        for prompt in ('Fix mail-worker confirmation in other/mail-worker','Explain read/write I/O in src/mail-worker'):
            d=self.routing.route(prompt,pinned_repository_ids=[self.repo])
            self.assertEqual(d['selected_repository_ids'],[self.repo])
            self.assertEqual(d['status'],'resolved')
        d=self.routing.route('Fix acme/mail-worker in lib/retry.py with read/write I/O')
        self.assertEqual(d['selected_repository_ids'],[self.repo])
        d=self.routing.route('Fix mail-worker in src/mail-worker with read/write')
        self.assertEqual(d['selected_repository_ids'],[self.repo])

    def test_renewal_retries_transient_storage_busy(self):
        r=self.service.send(self.conv,'confirmation','busy-once')
        heartbeat=self.jobs.heartbeat; attempts=[]; renewed=threading.Event()
        def busy_once(*args,**kwargs):
            if threading.current_thread().name=='ask-lease-renewal':
                attempts.append(1)
                if len(attempts)==1:
                    raise StorageBusy('Storage is busy.')
                result=heartbeat(*args,**kwargs); renewed.set(); return result
            return heartbeat(*args,**kwargs)
        def wait_for_renewal():
            self.assertTrue(renewed.wait(2))
            self.assertFalse(self.transport.cancel_events[-1].is_set())
        self.transport.callback=wait_for_renewal
        with patch.object(self.jobs,'heartbeat',side_effect=busy_once):
            Worker(self.jobs,{'ask.answer':self.service.run_answer},worker_id='busy',lease_ms=1000).run_once()
        self.assertEqual(len(attempts),2)
        self.assertEqual(self.service.get_run(r['id'])['state'],'completed')

    def test_renewal_retry_never_revives_expiry_or_lost_owner(self):
        for boundary in ('expiry','owner','cancel'):
            with self.subTest(boundary=boundary):
                r=self.service.send(self.conv,'confirmation','busy-'+boundary)
                heartbeat=self.jobs.heartbeat; attempts=[]
                def busy_then_fence(*args,**kwargs):
                    if threading.current_thread().name=='ask-lease-renewal':
                        attempts.append(1)
                        with self.store.transaction() as db:
                            if boundary=='expiry':
                                db.execute('UPDATE jobs SET lease_expires_at=0 WHERE id=?',(r['job_id'],))
                            elif boundary=='owner':
                                db.execute("UPDATE jobs SET lease_token='replacement-token',lease_owner='replacement' WHERE id=?",(r['job_id'],))
                            else:
                                db.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?',(r['job_id'],))
                        raise StorageBusy('Storage is busy.')
                    return heartbeat(*args,**kwargs)
                def await_cancel():
                    self.assertTrue(self.transport.cancel_events[-1].wait(2))
                self.transport.callback=await_cancel
                with patch.object(self.jobs,'heartbeat',side_effect=busy_then_fence):
                    Worker(self.jobs,{'ask.answer':self.service.run_answer},worker_id='busy-fence',lease_ms=1000).run_once()
                self.assertEqual(len(attempts),1,'Retry must fence before any second heartbeat')
                self.assertNotEqual(self.service.get_run(r['id'])['state'],'completed')
                if boundary=='expiry':
                    self.assertEqual(self.jobs.get(r['job_id']).lease_expires_at,0)
                # Reconcile test-owned abandoned leases; no active run leaks into the next case.
                if self.service.get_run(r['id'])['state'] in ('queued','running'):
                    with self.store.transaction() as db:
                        db.execute('UPDATE jobs SET lease_expires_at=0,max_attempts=1 WHERE id=?',(r['job_id'],))
                    self.jobs.reconcile_expired()
                    self.service.get_run(r['id'])

    def test_renewal_storage_busy_retry_is_bounded(self):
        r=self.service.send(self.conv,'confirmation','busy-exhausted')
        heartbeat=self.jobs.heartbeat; attempts=[]
        def always_busy(*args,**kwargs):
            if threading.current_thread().name=='ask-lease-renewal':
                attempts.append(1); raise StorageBusy('Storage is busy.')
            return heartbeat(*args,**kwargs)
        self.transport.callback=lambda:self.assertTrue(self.transport.cancel_events[-1].wait(2))
        with patch.object(self.jobs,'heartbeat',side_effect=always_busy):
            Worker(self.jobs,{'ask.answer':self.service.run_answer},worker_id='busy-exhausted',lease_ms=1000).run_once()
        self.assertEqual(len(attempts),2)
        result=self.service.get_run(r['id'])
        self.assertEqual(result['state'],'failed')
        self.assertEqual(result['error_code'],'storage_busy')

    def test_export_preserves_literal_markup_for_text_content_ui(self):
        literal='<script>alert("snake_case")</script> **literal** ```hidden instruction``` & <b>quoted</b>'
        c=copy.deepcopy(self.content); c['goal']=literal; c['requirements'].append(literal)
        p=self.plans.create(self.conv,c)
        exported=self.plans.export(p['id'])['markdown']
        self.assertIn(literal,exported)
        self.assertNotIn('&lt;script&gt;',exported)
        self.assertNotIn('snake\\_case',exported)
        self.service.send(self.conv,literal,'literal-export')
        self.assertIn(literal,self.service.export(self.conv,limit=1)['markdown'])
