"""Precise R1 regressions: real storage/vec and real provider error classification."""
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
import test_indexing_service as fixtures
from opendevengine.index import Snapshot
from opendevengine.indexing import IndexService, IndexError
from opendevengine.platform.clock import now_ms
from opendevengine.platform.worker import Worker
from opendevengine.providers import ProviderService
from opendevengine.providers.transport import TransportResponse, TransportTimeout


class ReviewRegressions(TestCase):
    def setUp(self):
        self.f = fixtures.IndexingTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)

    def worker(self,service=None):
        f = self.f
        return Worker(f.jobs,{'index.run':(service or f.service).run_index},worker_id='review')

    def require_vectors(self):
        if not self.f.service.vectors.available:
            self.skipTest('requires installed APSW/sqlite-vec')

    def test_default_half_megabyte_hybrid_and_free_cache_under_one_token_cap(self):
        self.require_vectors()
        f = self.f
        f.files = {f'module{i}.py':(('identifier%d = '%i + 'x'*135+'\n')*440).encode() for i in range(8)}
        first = f.index()
        state = f.service.get_state(f.repo)
        coverage = state['active_snapshot']['coverage']
        self.assertGreater(coverage['source_bytes'],500000)
        self.assertEqual(state['status'],'ready')
        self.assertEqual(state['active_snapshot']['mode'],'hybrid')
        self.assertEqual(coverage['semantic_chunks'],coverage['chunks'])
        self.assertGreater(coverage['embedding_tokens'],0)
        self.assertLess(coverage['embedding_tokens'],coverage['source_bytes'])
        calls = len(f.providers.calls)
        f.index(limits={'embedding_tokens':1,'monetary_cap':0})
        cached = f.service.get_state(f.repo)['active_snapshot']
        self.assertEqual(cached['mode'],'hybrid')
        self.assertEqual(cached['coverage']['embedding_tokens'],0)
        self.assertEqual(len(f.providers.calls),calls)
        self.assertTrue(f.service.source(f.repo,first['snapshot_id'],'module0.py',1,2)['lines'])

    def test_nine_kilobyte_window_and_one_oversized_line_keep_other_semantics(self):
        self.require_vectors()
        f = self.f
        f.files['window.py'] = (('x'*150+'\n')*60).encode()
        f.files['huge_line.py'] = b'z'*80000 + b'\n'
        run = f.index()
        coverage = f.service.get_state(f.repo)['active_snapshot']['coverage']
        self.assertEqual(coverage['mode'],'hybrid')
        self.assertTrue(coverage['partial'])
        self.assertEqual(coverage['skipped']['embedding_token_limit'],1)
        self.assertEqual(coverage['semantic_chunks'],coverage['chunks']-1)
        self.assertTrue(f.service.search('delivery',[f.repo])['hits'])
        with f.store.read() as db:
            windows = db.execute("SELECT text FROM code_chunks WHERE snapshot_id=? AND path='window.py'",(run['snapshot_id'],)).fetchall()
        self.assertGreater(len(windows),1)
        self.assertTrue(all(len(r[0].encode())<=8192 for r in windows))
        self.assertNotIn('z'*80000,f.providers.calls)

    def test_token_budget_skips_unaffordable_chunk_but_preserves_existing_vectors(self):
        self.require_vectors()
        f = self.f
        f.files = {'a.py':b'identifier = 1\n','b.py':b'x'*400,'c.py':b'ok = 1\n'}
        f.index(limits={'embedding_tokens':10})
        coverage = f.service.get_state(f.repo)['active_snapshot']['coverage']
        self.assertEqual(coverage['semantic_chunks'],2)
        self.assertEqual(coverage['mode'],'hybrid')
        self.assertEqual(coverage['embedding_tokens'],6) # ceil(14/4) + ceil(6/4)
        self.assertEqual(coverage['skipped']['embedding_token_limit'],1)
        self.assertEqual(f.providers.calls,['identifier = 1','ok = 1'])

    def test_real_provider_429_timeout_and_503_continue_durably(self):
        self.require_vectors()
        f = self.f
        now = [now_ms()]
        f.jobs.clock = lambda:now[0]
        for failure,code in [('429','rate_limited'),('timeout','timeout'),('503','upstream_unavailable')]:
            with self.subTest(failure=failure):
                calls = []
                class Transport:
                    def send(self,request,**kwargs):
                        self_test.assertFalse(f.store.conn.in_transaction)
                        calls.append(request)
                        if len(calls)==1:
                            if failure=='timeout':
                                now[0] += 60000 # exceeds the ordinary 30s worker lease
                                raise TransportTimeout('synthetic')
                            return TransportResponse(int(failure),{'Retry-After':'7'},b'{}')
                        texts = json.loads(request.body)['input']
                        return TransportResponse(200,{},json.dumps({'embeddings':[[1.,0.,0.] for _ in texts]}).encode())
                self_test = self
                real = ProviderService(f.store,None,transport=Transport(),clock=lambda:now[0])
                service = IndexService(f.store,f.jobs,None,real,vector_store=f.service.vectors,snapshot_reader=f.reader,clock=lambda:now[0])
                root = f.root/failure
                root.mkdir()
                repo = service.register_local(failure,root)['repository_id']
                run = service.request_index(repo,embedding_version_id=f.version)
                self.worker(service).run_once()
                waiting = service.get_run(run['id'])
                self.assertEqual(waiting['state'],'waiting',waiting)
                self.assertEqual(waiting['error_code'],code)
                self.assertNotEqual(waiting['job_id'],run['job_id'])
                self.assertGreater(waiting['retry_at'],now[0])
                if failure=='429':
                    self.assertEqual(waiting['retry_at'],now[0]+7000)
                self.assertFalse(self.worker(service).run_once())
                # Reopen both databases to prove continuation isn't process state.
                f.service.vectors.close()
                f.store.close()
                from opendevengine.platform.db import Store
                from opendevengine.platform.jobs import JobQueue
                f.store = Store.open(f.root/'data')
                f.jobs = JobQueue(f.store,clock=lambda:now[0])
                real = ProviderService(f.store,None,transport=Transport(),clock=lambda:now[0])
                service = IndexService(f.store,f.jobs,None,real,data_dir=f.root/'data',snapshot_reader=f.reader,clock=lambda:now[0])
                f.service = service
                now[0] = waiting['retry_at']+1
                self.worker(service).run_once()
                self.assertEqual(service.get_run(run['id'])['state'],'completed',service.get_run(run['id']))
                self.assertEqual(len(calls),2)

    def test_policy_skips_ready_but_size_caps_partial(self):
        f = self.f
        f.files.update({'.gitignore':b'dist','vendor/x.py':b'generated','secrets/keys.txt':b'private','image.py':b'\0','custom.py':b'custom'})
        f.service.set_exclusions(f.repo,['custom.py'])
        f.index()
        state = f.service.get_state(f.repo)
        self.assertEqual(state['status'],'ready')
        coverage = state['active_snapshot']['coverage']
        self.assertFalse(coverage['partial'])
        self.assertEqual(sum(coverage['skipped'].values()),5)
        f.index(limits={'files':1})
        self.assertEqual(f.service.get_state(f.repo)['status'],'partial')

    def test_exclusion_change_retries_in_clean_staging_and_old_citations_hide(self):
        self.require_vectors()
        f = self.f
        old = f.index()
        f.files['worker.py'] = b'changed = 1\n'
        f.providers.callback = lambda:f.service.set_exclusions(f.repo,['worker.py'])
        run = f.service.request_index(f.repo,embedding_version_id=f.version)
        self.worker().run_once()
        failed = f.service.get_run(run['id'])
        self.assertEqual(failed['state'],'failed')
        self.assertEqual(failed['error_code'],'selection_changed')
        stale_snapshot = failed['snapshot_id']
        with self.assertRaises(IndexError):
            f.service.source(f.repo,old['snapshot_id'],'worker.py',1,1)
        f.providers.callback = None
        f.service.retry(run['id'])
        self.assertIsNone(f.service.get_run(run['id'])['snapshot_id'])
        self.worker().run_once()
        completed = f.service.get_run(run['id'])
        self.assertEqual(completed['state'],'completed',completed)
        self.assertNotEqual(completed['snapshot_id'],stale_snapshot)
        with f.store.read() as db:
            self.assertIsNone(db.execute('SELECT id FROM repository_snapshots WHERE id=?',(stale_snapshot,)).fetchone())
            self.assertEqual(db.execute("SELECT count(*) FROM snapshot_files WHERE snapshot_id=? AND path='worker.py'",(completed['snapshot_id'],)).fetchone()[0],0)
        self.assertTrue(f.service.source(f.repo,old['snapshot_id'],'billing.py',1,2)['lines'])

    def test_time_slices_advance_ingestion_and_embedding_without_manual_resume(self):
        self.require_vectors()
        f = self.f
        now = [now_ms()]
        f.service.clock = f.jobs.clock = lambda:now[0]
        # Each boundary advances a synthetic elapsed clock, without real sleeps.
        original = f.service._boundary
        elapsed = [0.0]
        def boundary(*args):
            elapsed[0] += 0.002
            return original(*args)
        f.service._boundary = boundary
        run = f.service.request_index(f.repo,embedding_version_id=f.version,limits={'elapsed_ms':3,'batch_size':1})
        continuations = []
        with patch('opendevengine.indexing.service.time.monotonic',side_effect=lambda:elapsed[0]):
            for _ in range(15):
                self.worker().run_once()
                state = f.service.get_run(run['id'])
                if state['state']=='completed':
                    break
                self.assertEqual(state['state'],'waiting',state)
                self.assertEqual(state['error_code'],'elapsed_limit')
                continuations.append(state['job_id'])
                now[0] = state['retry_at']+1
        self.assertEqual(state['state'],'completed',state)
        self.assertGreater(len(continuations),2)
        self.assertEqual(len(set(continuations)),len(continuations))
        self.assertEqual(state['progress']['files'],3)
        self.assertEqual(state['progress']['semantic_chunks'],3)
        self.assertFalse(state['progress']['partial'])

    def test_worker_capacity_schedules_continuation_and_then_completes(self):
        f = self.f
        now = [now_ms()]
        f.service.clock = f.jobs.clock = lambda:now[0]
        held = []
        for i in range(2):
            ident = f.service.register_demo(Snapshot('busy'+str(i),'old',{'x.py':('x = 1',)},True))['repository_id']
            run = f.service.request_index(ident)
            lease = f.jobs.claim('busy'+str(i),kinds=['index.run'])
            with f.store.transaction() as db:
                db.execute("UPDATE index_runs SET state='running' WHERE id=?",(run['id'],))
            held.append((run,lease))
        target = f.service.request_index(f.repo)
        self.worker().run_once()
        waiting = f.service.get_run(target['id'])
        self.assertEqual(waiting['state'],'waiting')
        self.assertEqual(waiting['error_code'],'worker_limit')
        for run,lease in held:
            f.jobs.complete(lease)
            with f.store.transaction() as db:
                db.execute("UPDATE index_runs SET state='cancelled' WHERE id=?",(run['id'],))
        now[0] = waiting['retry_at']+1
        self.worker().run_once()
        self.assertEqual(f.service.get_run(target['id'])['state'],'completed')

    def test_stale_retry_conflict_and_purged_demo_registration(self):
        f = self.f
        run = f.service.request_index(f.repo)
        with f.store.transaction() as db:
            db.execute("UPDATE index_runs SET state='failed' WHERE id=?",(run['id'],))
        f.jobs.cancel(run['job_id'])
        newer = f.service.request_index(f.repo)
        with self.assertRaises(IndexError) as raised:
            f.service.retry(run['id'])
        self.assertEqual(raised.exception.code,'active_run_conflict')
        self.assertEqual(f.service.get_run(newer['id'])['state'],'queued')
        snapshot = Snapshot('purged','old',{'x.py':('x = 1',)},True)
        demo = f.service.register_demo(snapshot)['repository_id']
        f.service.purge_repository(demo)
        with self.assertRaises(IndexError) as raised:
            f.service.set_local_selection(demo,True)
        self.assertEqual(raised.exception.code,'demo_registration_required')
        f.service.register_demo(snapshot, restore=True)
        f.service.set_local_selection(demo,True)
        target = f.service.request_index(demo)
        while self.worker().run_once():
            pass
        self.assertEqual(f.service.get_run(target['id'])['state'],'completed')

    def test_set_exclusions_validates_paths_hides_demo_and_requires_eligibility(self):
        f = self.f
        demo = f.service.register_demo(Snapshot('settings','old',{'x.py':('public_identifier = 1',)},True))['repository_id']
        run = f.service.request_index(demo)
        self.worker().run_once()
        self.assertTrue(f.service.search('public_identifier',[demo],semantic=False)['hits'])
        for pattern in ('../x.py','/etc','a/../x','a\\x','bad\npath'):
            with self.assertRaises(ValueError):
                f.service.set_exclusions(demo,[pattern])
        state = f.service.set_exclusions(demo,['x.py'])
        self.assertTrue(state['eligible'])
        self.assertFalse(state['implementation_enabled'])
        self.assertFalse(f.service.search('public_identifier',[demo],semantic=False)['hits'])
        with self.assertRaises(IndexError):
            f.service.source(demo,f.service.get_run(run['id'])['snapshot_id'],'x.py',1,1)
        f.service.set_local_selection(demo,False)
        with self.assertRaises(IndexError):
            f.service.set_exclusions(demo,[])

    def test_abandoned_staging_cleanup_preserves_active_history_and_cache(self):
        self.require_vectors()
        f = self.f
        active = f.index()
        f.files = {'a.py':b'a_identifier = 1\n','b.py':b'b_identifier = 1\n','c.py':b'c_identifier = 1\n'}
        count = [0]
        def fail_second_batch():
            count[0] += 1
            if count[0]==2:
                raise RuntimeError('synthetic outage after vector staging')
        f.providers.callback = fail_second_batch
        run = f.service.request_index(f.repo,embedding_version_id=f.version,limits={'batch_size':1})
        self.worker().run_once()
        failed = f.service.get_run(run['id'])
        self.assertEqual(failed['state'],'failed')
        with f.store.read() as db:
            namespace = db.execute('SELECT namespace_id FROM repository_snapshots WHERE id=?',(failed['snapshot_id'],)).fetchone()[0]
        self.assertEqual(f.service.vectors.count(namespace,failed['snapshot_id']),1)
        f.providers.callback = None
        fresh = f.service.request_index(f.repo,embedding_version_id=f.version)
        self.assertEqual(f.service.vectors.count(namespace,failed['snapshot_id']),0)
        with f.store.read() as db:
            self.assertIsNone(db.execute('SELECT id FROM repository_snapshots WHERE id=?',(failed['snapshot_id'],)).fetchone())
            self.assertEqual(db.execute('SELECT count(*) FROM code_chunks_fts WHERE chunk_id NOT IN (SELECT id FROM code_chunks)').fetchone()[0],0)
        self.assertTrue(f.service.source(f.repo,active['snapshot_id'],'worker.py',1,1)['lines'])
        calls = len(f.providers.calls)
        self.worker().run_once()
        self.assertEqual(f.service.get_run(fresh['id'])['state'],'completed')
        self.assertEqual(len(f.providers.calls)-calls,2) # first staged chunk cache survived

    def test_real_provider_cancellation_preserves_selection_changed_for_clean_retry(self):
        self.require_vectors()
        f = self.f
        f.index()
        f.files['worker.py'] = b'changed_identifier = 2\n'
        class Transport:
            def send(self,request,**kwargs):
                f.service.set_exclusions(f.repo,['worker.py'])
                return TransportResponse(200,{},b'{"embeddings":[[1,0,0]]}')
        real = ProviderService(f.store,None,transport=Transport())
        service = IndexService(f.store,f.jobs,None,real,vector_store=f.service.vectors,snapshot_reader=f.reader)
        run = service.request_index(f.repo,embedding_version_id=f.version)
        self.worker(service).run_once()
        failed = service.get_run(run['id'])
        self.assertEqual(failed['state'],'failed')
        self.assertEqual(failed['error_code'],'selection_changed')
        service.retry(run['id'])
        self.worker(service).run_once()
        self.assertEqual(service.get_run(run['id'])['state'],'completed')

    def test_context_budget_uses_token_estimate_not_bytes(self):
        f = self.f
        f.files = {'test.py':b'identifier '+b'x'*89+b'\n'} # 100 bytes of chunk text
        f.index()
        self.assertTrue(f.service.search('identifier',[f.repo],semantic=False,context_token_budget=25)['hits'])
        self.assertFalse(f.service.search('identifier',[f.repo],semantic=False,context_token_budget=24)['hits'])

    def test_chunker_upgrade_restarts_staging_but_old_active_remains_queryable(self):
        self.require_vectors()
        f = self.f
        with patch('opendevengine.indexing.service.CHUNKER','lines-symbols-v1'):
            active = f.index()
            run = f.service.request_index(f.repo,embedding_version_id=f.version)
            f.providers.fail = True
            # A changed chunk ensures the staged replacement actually calls a model.
            f.files['worker.py'] = b'upgrade_identifier = 1\n'
            self.worker().run_once()
        f.providers.fail = False
        failed = f.service.get_run(run['id'])
        self.assertEqual(failed['state'],'failed')
        old = f.service.search('delivery',[f.repo],{f.repo:active['snapshot_id']})
        self.assertEqual(old['mode'],'hybrid')
        self.assertEqual(old['hits'][0]['path'],'worker.py')
        f.service.retry(run['id'])
        self.worker().run_once()
        completed = f.service.get_run(run['id'])
        self.assertEqual(completed['state'],'completed')
        self.assertNotEqual(completed['snapshot_id'],failed['snapshot_id'])
        with f.store.read() as db:
            snapshot = db.execute('SELECT chunker_version,revision FROM repository_snapshots WHERE id=?',(completed['snapshot_id'],)).fetchone()
        self.assertEqual(snapshot['chunker_version'],'lines-symbols-v2')
        self.assertEqual(snapshot['revision'],'a'*40)
