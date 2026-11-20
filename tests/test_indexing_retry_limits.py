"""R2 bounded retries use real ProviderService, persistent SQLite and APSW/vec."""
import json
from unittest import TestCase
from unittest.mock import patch
import test_indexing_service as fixtures
from opendevengine.indexing import IndexService
from opendevengine.platform.clock import now_ms
from opendevengine.platform.db import Store
from opendevengine.platform.jobs import JobQueue
from opendevengine.platform.worker import Worker
from opendevengine.providers import ProviderService
from opendevengine.providers.transport import TransportResponse, TransportTimeout, TransportUnavailable


class RetryLimitTests(TestCase):
    def setUp(self):
        self.f = fixtures.IndexingTests(methodName='runTest')
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.now = [now_ms()]
        self.f.jobs.clock = lambda:self.now[0]

    def vectors(self):
        if not self.f.service.vectors.available:
            self.skipTest('requires APSW/sqlite-vec')

    def worker(self,service):
        return Worker(self.f.jobs,{'index.run':service.run_index},worker_id='bounded-retry')

    def real_service(self,transport):
        f = self.f
        real = ProviderService(f.store,None,transport=transport,clock=lambda:self.now[0])
        return IndexService(f.store,f.jobs,None,real,vector_store=f.service.vectors,
                            snapshot_reader=f.reader,clock=lambda:self.now[0])

    def reopen(self,transport):
        f = self.f
        f.service.vectors.close()
        f.store.close()
        f.store = Store.open(f.root/'data')
        f.jobs = JobQueue(f.store,clock=lambda:self.now[0])
        real = ProviderService(f.store,None,transport=transport,clock=lambda:self.now[0])
        f.service = IndexService(f.store,f.jobs,None,real,data_dir=f.root/'data',
                                 snapshot_reader=f.reader,clock=lambda:self.now[0])
        return f.service

    def persistent_failure(self,kind):
        self.vectors()
        f = self.f
        old = f.index()
        old_source = f.service.source(f.repo,old['snapshot_id'],'worker.py',1,3)
        f.files['worker.py'] = b'new_identifier = 1\n'
        calls = []
        outer = self
        class Transport:
            def send(self,request,**kwargs):
                outer.assertFalse(f.store.conn.in_transaction)
                calls.append(request)
                if kind=='timeout':
                    raise TransportTimeout('synthetic timeout')
                raise TransportUnavailable('synthetic network failure')
        transport = Transport()
        service = self.real_service(transport)
        run = service.request_index(f.repo,embedding_version_id=f.version)
        job_ids = [run['job_id']]
        for failure in range(1,4):
            self.assertTrue(self.worker(service).run_once())
            current = service.get_run(run['id'])
            self.assertEqual(current['error_code'],'timeout' if kind=='timeout' else 'upstream_unavailable')
            self.assertEqual(current['progress']['retry']['transient_failures'],failure)
            self.assertEqual(service.get_state(f.repo)['active_snapshot']['id'],old['snapshot_id'])
            self.assertEqual(service.source(f.repo,old['snapshot_id'],'worker.py',1,3),old_source)
            if failure<3:
                self.assertEqual(current['state'],'waiting')
                self.assertEqual(current['retry_at']-self.now[0],2000*2**(failure-1))
                job_ids.append(current['job_id'])
                self.now[0] = current['retry_at']+1
                if failure==1:
                    # Neither the counter nor active source can be process-local.
                    service = self.reopen(transport)
            else:
                self.assertEqual(current['state'],'failed')
                self.assertIsNone(current['retry_at'])
        self.assertEqual(len(calls),3)
        self.assertEqual(len(set(job_ids)),3) # initial job + exactly two continuations
        self.now[0] += 1000000
        self.assertFalse(self.worker(service).run_once())
        self.assertIsNone(service.get_state(f.repo)['active_run'])
        # Failure releases the unique active-run slot for a new explicit request.
        replacement = service.request_index(f.repo,embedding_version_id=f.version)
        self.assertNotEqual(replacement['id'],run['id'])
        self.assertEqual(replacement['state'],'queued')

    def test_persistent_real_unavailable_fails_on_third_call_and_keeps_active(self):
        self.persistent_failure('unavailable')

    def test_persistent_real_timeout_fails_on_third_call_and_keeps_active(self):
        self.persistent_failure('timeout')

    def test_ingestion_network_failures_also_stop_after_three(self):
        f = self.f
        calls = []
        def reader(*args):
            calls.append(1)
            # GitHubClient uses this same stable network code.
            from opendevengine.github.errors import GitHubError
            raise GitHubError('upstream_unavailable')
        service = IndexService(f.store,f.jobs,None,None,vector_store=f.service.vectors,
                                snapshot_reader=reader,clock=lambda:self.now[0])
        run = service.request_index(f.repo)
        for failure in range(3):
            self.worker(service).run_once()
            current = service.get_run(run['id'])
            if failure<2:
                self.assertEqual(current['state'],'waiting')
                self.now[0] = current['retry_at']+1
        self.assertEqual(current['state'],'failed')
        self.assertEqual(current['error_code'],'upstream_unavailable')
        self.assertEqual(len(calls),3)
        self.assertFalse(self.worker(service).run_once())

    def test_real_rate_limit_has_ten_retry_ceiling_and_honors_retry_after(self):
        self.vectors()
        f = self.f
        calls = []
        class Transport:
            def send(self,request,**kwargs):
                calls.append(request)
                return TransportResponse(429,{'Retry-After':'600'},b'{}')
        service = self.real_service(Transport())
        run = service.request_index(f.repo,embedding_version_id=f.version)
        jobs = {run['job_id']}
        for attempt in range(11):
            self.worker(service).run_once()
            current = service.get_run(run['id'])
            self.assertEqual(current['error_code'],'rate_limited')
            if attempt<10:
                self.assertEqual(current['state'],'waiting')
                self.assertEqual(current['progress']['retry']['rate_limit_retries'],attempt+1)
                self.assertEqual(current['retry_at'],self.now[0]+600000)
                jobs.add(current['job_id'])
                self.assertFalse(self.worker(service).run_once())
                self.now[0] = current['retry_at']+1
                if attempt==4:
                    service = self.reopen(Transport())
            else:
                self.assertEqual(current['state'],'failed')
                self.assertEqual(current['progress']['retry']['rate_limit_retries'],10)
                self.assertIsNone(current['retry_at'])
        self.assertEqual(len(calls),11) # initial request + ten automatic retries
        self.assertEqual(len(jobs),11)
        self.now[0] += 1000000
        self.assertFalse(self.worker(service).run_once())

    def test_real_small_context_skips_only_oversized_vector_before_call(self):
        self.vectors()
        f = self.f
        profile = f.real.create_profile(dict(name='small',adapter='ollama',endpoint='http://localhost:11434',
                model='small-injected',config=dict(capabilities=['embeddings'],embedding_dimensions=3,context_limit=512)))
        version = profile['active_version_id']
        f.files = {'small.py':b'small_identifier = 1\n','exact.py':b'x'*2048+b'\n','over.py':b'y'*2049+b'\n'}
        sent = []
        class Transport:
            def send(self,request,**kwargs):
                texts = json.loads(request.body)['input']
                sent.extend(texts)
                return TransportResponse(200,{},json.dumps({'embeddings':[[1.,0.,0.] for _ in texts]}).encode())
        service = self.real_service(Transport())
        run = service.request_index(f.repo,embedding_version_id=version)
        self.worker(service).run_once()
        done = service.get_run(run['id'])
        self.assertEqual(done['state'],'completed',done)
        coverage = done['progress']
        self.assertTrue(coverage['partial'])
        self.assertEqual(coverage['semantic_chunks'],2)
        self.assertEqual(coverage['chunks'],3)
        self.assertEqual(coverage['skipped']['embedding_context_limit'],1)
        self.assertEqual(coverage['mode'],'hybrid')
        self.assertEqual(coverage['degradation_code'],'embedding_context_limit')
        self.assertEqual(sorted(map(len,sent)),[20,2048])
        self.assertNotIn('y'*2049,sent)
        self.assertEqual(service.source(f.repo,done['snapshot_id'],'over.py',1,1)['lines'],['y'*2049])

    def test_successful_vector_progress_resets_consecutive_failures(self):
        self.vectors()
        f = self.f
        f.files = {'a.py':b'a = 1\n','b.py':b'b = 2\n'}
        calls = []
        class Transport:
            def send(self,request,**kwargs):
                calls.append(request)
                # Two failures, progress, two failures, progress: never three consecutive.
                if len(calls) in (1,2,4,5):
                    raise TransportUnavailable('synthetic')
                return TransportResponse(200,{},b'{"embeddings":[[1,0,0]]}')
        service = self.real_service(Transport())
        run = service.request_index(f.repo,embedding_version_id=f.version,limits={'batch_size':1})
        expected = [1,2,1,2]
        for failures in expected:
            self.worker(service).run_once()
            current = service.get_run(run['id'])
            self.assertEqual(current['state'],'waiting',current)
            self.assertEqual(current['progress']['retry']['transient_failures'],failures)
            self.assertEqual(current['retry_at']-self.now[0],2000*2**(failures-1))
            self.now[0] = current['retry_at']+1
        self.worker(service).run_once()
        self.assertEqual(service.get_run(run['id'])['state'],'completed')
        self.assertEqual(len(calls),6)

    def test_worker_and_time_waits_do_not_change_failure_counter_or_backoff(self):
        self.vectors()
        f = self.f
        now = self.now
        calls = []
        class Transport:
            def send(self,request,**kwargs):
                calls.append(request)
                raise TransportUnavailable('synthetic')
        service = self.real_service(Transport())
        run = service.request_index(f.repo,embedding_version_id=f.version,limits={'batch_size':1,'elapsed_ms':1})
        # Real worker-capacity scheduling, without fake continuation-count writes.
        held = []
        target_lease = f.jobs.claim('target',kinds=['index.run'])
        for i in range(2):
            root = f.root/('occupied'+str(i))
            root.mkdir()
            ident = service.register_local('occupied',root)['repository_id']
            other = service.request_index(ident)
            lease = f.jobs.claim('occupied'+str(i),kinds=['index.run'])
            with f.store.transaction() as db:
                db.execute("UPDATE index_runs SET state='running' WHERE id=?",(other['id'],))
            held.append((other,lease))
        from opendevengine.platform.worker import JobContext
        service.run_index(target_lease.job,JobContext(f.jobs,target_lease,30000))
        f.jobs.complete(target_lease)
        waiting = service.get_run(run['id'])
        self.assertEqual(waiting['error_code'],'worker_limit')
        self.assertEqual(waiting['retry_at']-now[0],30000)
        self.assertNotIn('retry',waiting['progress'])
        for other,lease in held:
            f.jobs.complete(lease)
            with f.store.transaction() as db:
                db.execute("UPDATE index_runs SET state='cancelled' WHERE id=?",(other['id'],))
        now[0] = waiting['retry_at']+1
        # Force ingestion time continuations before any provider invocation.
        elapsed = [0.0]
        boundary = service._boundary
        def advance(*args):
            elapsed[0] += 0.002
            return boundary(*args)
        service._boundary = advance
        time_waits = 0
        with patch('opendevengine.indexing.service.time.monotonic',side_effect=lambda:elapsed[0]):
            for _ in range(10):
                self.worker(service).run_once()
                current = service.get_run(run['id'])
                if current['error_code']=='upstream_unavailable':
                    break
                self.assertEqual(current['error_code'],'elapsed_limit')
                self.assertEqual(current['retry_at']-now[0],5000)
                time_waits += 1
                now[0] = current['retry_at']+1
        self.assertGreaterEqual(time_waits,1)
        self.assertGreater(current['continuation'],1)
        self.assertEqual(current['progress']['retry']['transient_failures'],1)
        self.assertEqual(current['retry_at']-now[0],2000)
        self.assertEqual(len(calls),1)
