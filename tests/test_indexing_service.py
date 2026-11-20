"""Behavior checks use real migrated SQLite, APSW/vec and injected source/model I/O."""
import json
from pathlib import Path
import tempfile
import unittest
from opendevengine.platform.db import Store
from opendevengine.platform.jobs import JobQueue
from opendevengine.platform.worker import Worker, JobContext
from opendevengine.platform.errors import LeaseLost
from opendevengine.index import Snapshot
from opendevengine.indexing import IndexService, IndexError, VectorStore
from opendevengine.providers import ProviderService


class Providers:
    """Semantic fixture: fulfillment/delivery/mail share an intent vector."""
    def __init__(self, real):
        self.real, self.calls, self.fail, self.callback = real, [], False, None
    def roles(self):
        return self.real.roles()
    def embedding_namespace(self, version):
        return self.real.embedding_namespace(version)
    def get_version(self, version):
        return self.real.get_version(version)
    def embed(self, version, texts, **kwargs):
        if self.callback:
            self.callback()
        if self.fail:
            raise RuntimeError('Synthetic outage')
        self.calls.extend(texts)
        vectors = []
        for text in texts:
            text = text.lower()
            vectors.append([1.,0.,0.] if any(t in text for t in ('delivery','fulfillment','mail','send_order')) else [0.,1.,0.] if any(t in text for t in ('invoice','payment','billing')) else [0.,0.,1.])
        return dict(vectors=vectors,namespace=self.real.embedding_namespace(version),profile_version_id=version)


class IndexingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store.open(self.root/'data')
        self.jobs = JobQueue(self.store)
        self.real = ProviderService(self.store,None)
        profile = self.real.create_profile(dict(name='synthetic',adapter='ollama',endpoint='http://localhost:11434',model='injected',config=dict(capabilities=['embeddings'],embedding_dimensions=3)))
        self.version = profile['active_version_id']
        self.providers = Providers(self.real)
        self.files = {'worker.py':b'def send_order():\n    """Send fulfillment mail."""\n    return True\n', 'billing.py':b'def invoice():\n    return "payment"\n', 'README.md':b'# Synthetic fixture\n'}
        self.revision = 'a'*40
        self.reader_callback = None
        def reader(source,revision,caps):
            if self.reader_callback:
                self.reader_callback()
            files = dict(self.files)
            return dict(revision=revision or self.revision,revision_kind='git',files=({'path':p,'content':v} if isinstance(v,bytes) else {'path':p,'kind':v} for p,v in files.items()))
        self.reader = reader
        self.service = IndexService(self.store,self.jobs,None,self.providers,data_dir=self.root/'data',snapshot_reader=reader)
        self.repo = self.service.register_local('orders',self.root)['repository_id']

    def tearDown(self):
        self.service.vectors.close()
        self.store.close()
        self.tmp.cleanup()

    def index(self, **kwargs):
        run = self.service.request_index(self.repo,embedding_version_id=self.version,**kwargs)
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='test').run_once()
        result = self.service.get_run(run['id'])
        self.assertEqual(result['state'],'completed',result)
        return result

    def test_real_vector_scoping_and_immutable_history(self):
        if not self.service.vectors.available:
            self.skipTest('APSW/sqlite-vec unavailable in this interpreter')
        first = self.index()
        hybrid = self.service.search('delivery',[self.repo])
        self.assertEqual(hybrid['mode'],'hybrid',hybrid)
        self.assertEqual(hybrid['hits'][0]['path'],'worker.py')
        self.assertEqual(self.service.search('delivery',[self.repo],semantic=False)['hits'],[])
        old = self.service.source(self.repo,first['snapshot_id'],'worker.py',1,3)
        calls = len(self.providers.calls)
        self.revision = 'b'*40
        self.files.pop('worker.py')
        self.files['new.py'] = b'new_identifier = 2\n'
        second = self.index()
        self.assertLess(len(self.providers.calls)-calls,len(self.files))
        self.assertEqual(self.service.search('send_order',[self.repo],semantic=False)['hits'],[])
        self.assertEqual(self.service.source(self.repo,first['snapshot_id'],'worker.py',1,3),old)
        self.assertNotEqual(first['snapshot_id'],second['snapshot_id'])
        # Closer vectors from retained old snapshot cannot leak into active KNN.
        result = self.service.search('delivery',[self.repo])
        self.assertTrue(all(h['snapshot_id']==second['snapshot_id'] for h in result['hits']))

    def test_exclusions_binary_symlinks_and_selection(self):
        self.files.update({'.env':b'SECRET', 'key.pem':b'private', 'secrets.py':b'hidden', 'secrets/config.py':b'hidden directory', 'binary.py':b'a\0b', 'link.py':'symlink','vendor/pkg.py':b'vendored', 'custom.py':b'custom', 'creds.txt':b'-----BEGIN PRIVATE KEY-----\nsecret'})
        self.service.set_local_selection(self.repo,True,['custom.py'])
        first = self.index()
        transmitted = '\n'.join(self.providers.calls)
        for hidden in ('SECRET','private','hidden','a\0b','vendored','custom','BEGIN PRIVATE'):
            self.assertNotIn(hidden,transmitted)
        state = self.service.get_state(self.repo)
        self.assertEqual(state['status'],'ready')
        self.assertFalse(state['active_snapshot']['coverage']['partial'])
        self.assertGreater(sum(state['active_snapshot']['coverage']['skipped'].values()),5)
        self.service.set_local_selection(self.repo,True,['worker.py'])
        self.assertEqual(self.service.search('send_order',[self.repo],semantic=False)['hits'],[])
        with self.assertRaises(IndexError):
            self.service.source(self.repo,first['snapshot_id'],'worker.py',1,1)
        count = len(self.providers.calls)
        self.service.set_local_selection(self.repo,False)
        with self.assertRaises(IndexError):
            self.service.search('delivery',[self.repo])
        self.assertEqual(len(self.providers.calls),count)

    def test_pause_resume_restart_and_fencing(self):
        run = self.service.request_index(self.repo,embedding_version_id=self.version)
        self.service.pause(run['id'])
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='pause').run_once()
        self.assertEqual(self.service.get_run(run['id'])['state'],'paused')
        resumed = self.service.resume(run['id'])
        self.assertNotEqual(run['job_id'],resumed['job_id'])
        # Restart with persisted queue/run and vectors.
        self.service.vectors.close()
        self.service = IndexService(self.store,self.jobs,None,self.providers,data_dir=self.root/'data',snapshot_reader=self.reader)
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='resume').run_once()
        self.assertEqual(self.service.get_run(run['id'])['state'],'completed')
        second = self.service.request_index(self.repo,embedding_version_id=self.version)
        lease = self.jobs.claim('stale',kinds=['index.run'])
        with self.store.transaction() as db:
            db.execute('UPDATE jobs SET lease_expires_at=0 WHERE id=?',(lease.job.id,))
        with self.assertRaises(LeaseLost):
            self.service.run_index(lease.job,JobContext(self.jobs,lease,30000))
        self.assertEqual(self.service.get_run(second['id'])['state'],'queued')

    def test_failed_replacement_retains_context_and_retry(self):
        if not self.service.vectors.available:
            self.skipTest('APSW/sqlite-vec unavailable in this interpreter')
        first = self.index()
        self.files['worker.py'] = b'def send_order():\n    return "changed"\n'
        self.revision = 'b'*40
        self.providers.fail = True
        run = self.service.request_index(self.repo,embedding_version_id=self.version)
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='failure').run_once()
        self.assertEqual(self.service.get_run(run['id'])['state'],'failed')
        self.assertEqual(self.service.get_state(self.repo)['active_snapshot']['id'],first['snapshot_id'])
        self.providers.fail = False
        self.service.retry(run['id'])
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='retry').run_once()
        self.assertEqual(self.service.get_run(run['id'])['state'],'completed')

    def test_disk_cap_pauses_ingestion(self):
        from opendevengine.indexing.safety import DEFAULTS
        # Reference workload: 100,000 chunks must fit under the default cap.
        self.assertGreaterEqual(DEFAULTS['disk_bytes'], 4*1024**3)
        run = self.service.request_index(self.repo,limits={'disk_bytes':1})
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='cap').run_once()
        result = self.service.get_run(run['id'])
        self.assertEqual((result['state'],result['error_code']),('paused','disk_limit'))

    def test_purged_rows_free_cap_headroom(self):
        # Enough rows to occupy whole pages; a purge leaves them on the freelist.
        self.files.update({f'pkg/m{i}.py': ('\n'.join(f'value_{i}_{n} = {n}' for n in range(200))).encode()
                           for i in range(40)})
        self.index()
        used = self.store.used_bytes()
        self.service.purge_repository(self.repo)
        self.assertLess(self.store.used_bytes(), used)
        self.assertLessEqual(self.store.used_bytes(), self.store.size_bytes())

    def test_resume_adopts_current_default_for_superseded_cap(self):
        from opendevengine.indexing import safety
        old = 512*1024*1024
        self.assertIn(old, safety.SUPERSEDED_DEFAULTS['disk_bytes'])
        run = self.service.request_index(self.repo,limits={'disk_bytes':1})
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='old').run_once()
        self.assertEqual(self.service.get_run(run['id'])['state'],'paused')
        # Simulate a run persisted before the default was raised (no provenance marker).
        with self.store.transaction() as db:
            caps = json.loads(db.execute('SELECT limits_json FROM index_runs WHERE id=?',(run['id'],)).fetchone()[0])
            caps.pop('defaults_version')
            db.execute('UPDATE index_runs SET limits_json=? WHERE id=?',(json.dumps({**caps,'disk_bytes':old}),run['id']))
        self.service.resume(run['id'])
        with self.store.read() as db:
            caps = json.loads(db.execute('SELECT limits_json FROM index_runs WHERE id=?',(run['id'],)).fetchone()[0])
        self.assertEqual(caps['disk_bytes'],safety.DEFAULTS['disk_bytes'])
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='new').run_once()
        self.assertEqual(self.service.get_run(run['id'])['state'],'completed')

    def test_user_lowered_cap_survives_resume(self):
        run = self.service.request_index(self.repo,limits={'disk_bytes':1})
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='low').run_once()
        self.service.resume(run['id'])
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='low2').run_once()
        result = self.service.get_run(run['id'])
        self.assertEqual((result['state'],result['error_code']),('paused','disk_limit'))

    def test_explicit_512_mib_cap_survives_resume(self):
        cap = 512*1024*1024
        self.store.used_bytes = lambda: 600*1024*1024
        run = self.service.request_index(self.repo,limits={'disk_bytes':cap})
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='explicit').run_once()
        self.assertEqual(self.service.get_run(run['id'])['state'],'paused')
        self.service.resume(run['id'])
        with self.store.read() as db:
            caps = json.loads(db.execute('SELECT limits_json FROM index_runs WHERE id=?',(run['id'],)).fetchone()[0])
        self.assertEqual(caps['disk_bytes'],cap)
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='explicit2').run_once()
        result = self.service.get_run(run['id'])
        self.assertEqual((result['state'],result['error_code']),('paused','disk_limit'))
        self.assertNotIn('defaults_version',result['progress'].get('limits',{}))

    def test_unreadable_free_space_pauses_instead_of_failing(self):
        def broken():
            raise FileNotFoundError('data directory vanished')
        self.store.free_bytes = broken
        run = self.service.request_index(self.repo)
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='probe').run_once()
        result = self.service.get_run(run['id'])
        self.assertEqual((result['state'],result['error_code']),('paused','disk_limit'))
        del self.store.free_bytes
        self.service.resume(run['id'])
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='probe2').run_once()
        self.assertEqual(self.service.get_run(run['id'])['state'],'completed')

    def test_free_space_reserve_pauses_until_space_returns(self):
        from opendevengine.indexing.safety import DISK_RESERVE
        free = self.store.free_bytes()
        self.assertIsInstance(free,int)
        self.store.free_bytes = lambda: DISK_RESERVE
        run = self.service.request_index(self.repo)
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='full').run_once()
        result = self.service.get_run(run['id'])
        self.assertEqual((result['state'],result['error_code']),('paused','disk_limit'))
        self.store.free_bytes = lambda: free
        self.service.resume(run['id'])
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='freed').run_once()
        self.assertEqual(self.service.get_run(run['id'])['state'],'completed')

    def test_lexical_partial_demo_purge_and_namespace(self):
        run = self.service.request_index(self.repo,limits={'files':1})
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='lexical').run_once()
        self.assertEqual(self.service.get_run(run['id'])['state'],'completed')
        self.assertEqual(self.service.search('send_order',[self.repo])['mode'],'lexical_only')
        self.assertEqual(self.service.get_state(self.repo)['status'],'partial')
        demo = self.service.register_demo(Snapshot('fixture','legacy',{'demo.py':('demo_identifier = 1',)},True))
        d = self.service.request_index(demo['repository_id'])
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='demo').run_once()
        self.assertEqual(self.service.get_run(d['id'])['state'],'completed')
        self.assertEqual(self.service.get_state(demo['repository_id'])['active_snapshot']['revision_kind'],'content_hash')
        self.assertEqual(self.service.register_local('renamed',self.root)['repository_id'],self.repo)
        self.service.purge_repository(self.repo)
        self.service.purge_repository(self.repo)
        self.assertEqual(self.service.search('demo_identifier',[demo['repository_id']])['hits'][0]['repository_id'],demo['repository_id'])
        with self.store.read() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM repository_snapshots WHERE repository_id=?',(self.repo,)).fetchone()[0],0)

    def test_revocation_after_provider_call_prevents_activation(self):
        if not self.service.vectors.available:
            self.skipTest('vectors unavailable')
        self.providers.callback = lambda:self.service.set_local_selection(self.repo,False)
        run = self.service.request_index(self.repo,embedding_version_id=self.version)
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='revoke').run_once()
        self.assertIsNone(self.service.get_state(self.repo)['active_snapshot'])
        self.assertEqual(self.service.get_run(run['id'])['error_code'],'repository_unavailable')

    def test_unknown_cost_pauses_before_provider(self):
        if not self.service.vectors.available:
            self.skipTest('APSW/sqlite-vec unavailable in this interpreter')
        run = self.service.request_index(self.repo,embedding_version_id=self.version,limits={'monetary_cap':1})
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='cost').run_once()
        self.assertEqual(self.service.get_run(run['id'])['state'],'paused')
        self.assertEqual(self.service.get_run(run['id'])['error_code'],'cost_unavailable')
        self.assertEqual(self.providers.calls,[])

    def test_source_range_and_scope_validation_before_models(self):
        run = self.index()
        for path in ('../worker.py','/worker.py','a/../worker.py','a\\worker.py'):
            with self.assertRaises(ValueError):
                self.service.source(self.repo,run['snapshot_id'],path,1,1)
        count = len(self.providers.calls)
        with self.assertRaises(Exception):
            self.service.search('delivery',[self.repo,'local:missing'])
        self.assertEqual(len(self.providers.calls),count)

    def test_mid_ingest_pause_reopen_and_idempotent_replay(self):
        initial = self.files.copy()
        def reader(source,revision,caps):
            def entries():
                for i,(path,raw) in enumerate(initial.items()):
                    if i == 1 and not getattr(self,'paused_once',False):
                        self.paused_once = True
                        self.service.pause(run['id'])
                    yield dict(path=path,content=raw)
            return dict(revision=revision or self.revision,revision_kind='git',files=entries())
        self.service.snapshot_reader = reader
        run = self.service.request_index(self.repo,embedding_version_id=self.version)
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='interrupt').run_once()
        self.assertEqual(self.service.get_run(run['id'])['state'],'paused')
        self.assertEqual(self.service.get_run(run['id'])['progress']['files'],1)
        self.service.vectors.close()
        self.store.close()
        self.store = Store.open(self.root/'data')
        self.jobs = JobQueue(self.store)
        self.real = ProviderService(self.store,None)
        self.providers.real = self.real
        self.service = IndexService(self.store,self.jobs,None,self.providers,data_dir=self.root/'data',snapshot_reader=reader)
        self.service.resume(run['id'])
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='reopened').run_once()
        completed = self.service.get_run(run['id'])
        self.assertEqual(completed['state'],'completed',completed)
        with self.store.read() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM code_chunks WHERE snapshot_id=?',(completed['snapshot_id'],)).fetchone()[0],len(initial))
        self.assertEqual(len(self.service.search('send_order',[self.repo],semantic=False)['hits']),1)

    def test_lease_expiry_during_provider_call_fences_vectors_and_activation(self):
        if not self.service.vectors.available:
            self.skipTest('vectors unavailable')
        run = self.service.request_index(self.repo,embedding_version_id=self.version)
        def expire():
            with self.store.transaction() as db:
                db.execute('UPDATE jobs SET lease_expires_at=0 WHERE id=?',(run['job_id'],))
        self.providers.callback = expire
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='expire').run_once()
        self.assertIsNone(self.service.get_state(self.repo)['active_snapshot'])
        with self.store.read() as db:
            ns = db.execute('SELECT namespace_id FROM repository_snapshots').fetchone()[0]
        self.assertEqual(self.service.vectors.count(ns,self.service.get_run(run['id'])['snapshot_id']),0)
        self.providers.callback = None
        self.jobs.reconcile_expired()
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='recover').run_once()
        self.assertEqual(self.service.get_run(run['id'])['state'],'completed')

    def test_cancelled_queue_job_persists_domain_cancellation(self):
        run = self.service.request_index(self.repo)
        lease = self.jobs.claim('cancel',kinds=['index.run'])
        self.jobs.cancel(lease.job.id)
        from opendevengine.platform.worker import CancelledError
        with self.assertRaises(CancelledError):
            self.service.run_index(lease.job,JobContext(self.jobs,lease,30000))
        self.assertEqual(self.service.get_run(run['id'])['state'],'cancelled')

    def test_lowered_embedding_budget_is_explicit_partial_lexical(self):
        if not self.service.vectors.available:
            self.skipTest('vectors unavailable')
        self.index(limits={'embedding_tokens':1})
        state = self.service.get_state(self.repo)
        self.assertEqual(state['status'],'partial')
        self.assertEqual(state['active_snapshot']['mode'],'lexical_only')
        self.assertEqual(self.providers.calls,[])

    def test_namespaces_do_not_mix_and_purge_recovers_after_restart(self):
        if not self.service.vectors.available:
            self.skipTest('vectors unavailable')
        first = self.index()
        profile = self.real.list_profiles()[0]
        updated = self.real.update_profile(profile['id'],dict(name='synthetic',adapter='ollama',endpoint='http://localhost:11434',model='injected-v2',config=dict(capabilities=['embeddings'],embedding_dimensions=3)))
        self.version = updated['active_version_id']
        second = self.index()
        with self.store.read() as db:
            ns = [r[0] for r in db.execute('SELECT namespace_id FROM repository_snapshots ORDER BY rowid')]
        self.assertNotEqual(ns[0],ns[1])
        self.assertEqual(self.service.search('delivery',[self.repo],{self.repo:first['snapshot_id']})['hits'][0]['snapshot_id'],first['snapshot_id'])
        other = self.service.register_demo(Snapshot('other','old',{'other.py':('other_identifier = 1',)},True))['repository_id']
        r = self.service.request_index(other,embedding_version_id=self.version)
        Worker(self.jobs,{'index.run':self.service.run_index},worker_id='other').run_once()
        self.assertEqual(self.service.get_run(r['id'])['state'],'completed')
        purge = self.service.vectors.purge
        def interrupted(repository):
            purge(repository)
            raise OSError('synthetic interruption after vector commit')
        self.service.vectors.purge = interrupted
        with self.assertRaises(OSError):
            self.service.purge_repository(self.repo)
        with self.assertRaises(IndexError):
            self.service.set_local_selection(self.repo,True)
        with self.assertRaises(IndexError):
            self.service.search('delivery',[self.repo])
        self.service.vectors.close()
        self.store.close()
        self.store = Store.open(self.root/'data')
        self.jobs = JobQueue(self.store)
        self.real = ProviderService(self.store,None)
        self.providers.real = self.real
        self.service = IndexService(self.store,self.jobs,None,self.providers,data_dir=self.root/'data',snapshot_reader=self.reader)
        self.service.purge_repository(self.repo)
        self.assertEqual(self.service.vectors.count(ns[0],first['snapshot_id']),0)
        self.assertEqual(self.service.vectors.count(ns[1],second['snapshot_id']),0)
        self.assertTrue(self.service.search('other_identifier',[other],semantic=False)['hits'])
        with self.store.read() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM repository_snapshots WHERE repository_id=?',(self.repo,)).fetchone()[0],0)

    def test_real_local_git_reader_ignores_worktree_and_hooks(self):
        import subprocess
        root = self.root/'checkout'
        root.mkdir()
        def git(*args):
            return subprocess.run(['git','-C',str(root),*args],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE).stdout
        git('init')
        git('config','user.email','synthetic@example.invalid')
        git('config','user.name','Synthetic')
        (root/'tracked.py').write_text('immutable_identifier = 1\n')
        (root/'secret.pem').write_text('excluded private source')
        (root/'link.py').symlink_to('tracked.py')
        git('add','.')
        git('commit','-m','synthetic fixture')
        (root/'tracked.py').write_text('uncommitted_identifier = 2\n')
        (root/'.git/hooks/post-checkout').write_text('#!/bin/sh\ntouch should-not-exist\n')
        (root/'.git/hooks/post-checkout').chmod(0o700)
        service = IndexService(self.store,self.jobs,None,None,vector_store=self.service.vectors)
        ident = service.register_local('real-git',root)['repository_id']
        run = service.request_index(ident)
        Worker(self.jobs,{'index.run':service.run_index},worker_id='git-reader').run_once()
        self.assertEqual(service.get_run(run['id'])['state'],'completed',service.get_run(run['id']))
        self.assertTrue(service.search('immutable_identifier',[ident],semantic=False)['hits'])
        self.assertFalse(service.search('uncommitted_identifier',[ident],semantic=False)['hits'])
        self.assertFalse((root/'should-not-exist').exists())
        self.assertEqual(service.get_state(ident)['active_snapshot']['coverage']['skipped']['symlink'],1)

    def test_real_provider_service_injected_transport_policy(self):
        from opendevengine.providers.transport import TransportResponse
        calls = []
        class Transport:
            def send(self,request,**kwargs):
                calls.append(request)
                texts = json.loads(request.body)['input']
                return TransportResponse(200,{},json.dumps({'embeddings':[[1.,0.,0.] for _ in texts]}).encode())
        # Real provider validates every request and result; no live model/network.
        real = ProviderService(self.store,None,transport=Transport())
        service = IndexService(self.store,self.jobs,None,real,vector_store=self.service.vectors,snapshot_reader=self.reader)
        if not service.vectors.available:
            self.skipTest('vectors unavailable')
        run = service.request_index(self.repo,embedding_version_id=self.version)
        Worker(self.jobs,{'index.run':service.run_index},worker_id='provider').run_once()
        self.assertEqual(service.get_run(run['id'])['state'],'completed',service.get_run(run['id']))
        self.assertTrue(calls)
        self.assertEqual(service.search('delivery',[self.repo])['mode'],'hybrid')


    def test_labeled_retrieval_evaluation(self):
        if not self.service.vectors.available:
            self.skipTest('vectors unavailable')
        self.index()
        fixture = json.loads((Path(__file__).parent/'fixtures/indexing/retrieval.json').read_text())
        measured = {}
        for mode in ('lexical','hybrid'):
            for kind in ('exact','paraphrase','ambiguous','absent'):
                tasks = [t for t in fixture if t['kind']==kind]
                correct = 0
                for task in tasks:
                    result = self.service.search(task['query'],[self.repo],semantic=mode=='hybrid',limit=10)
                    paths = {h['path'] for h in result['hits']}
                    if task['paths']:
                        correct += int(set(task['paths']) <= paths)
                    else:
                        correct += int(not paths)
                measured[mode+':'+kind] = [correct,len(tasks)]
        self.assertEqual(measured,{
            'lexical:exact':[3,3],'lexical:paraphrase':[2,3],
            'lexical:ambiguous':[1,1],'lexical:absent':[1,1],
            'hybrid:exact':[3,3],'hybrid:paraphrase':[3,3],
            'hybrid:ambiguous':[1,1],'hybrid:absent':[0,1]})


    def test_rate_limit_schedules_unique_durable_continuation(self):
        if not self.service.vectors.available:
            self.skipTest('vectors unavailable')
        from opendevengine.platform.clock import now_ms
        now = [now_ms()]
        self.service.clock = self.jobs.clock = lambda:now[0]
        def limited():
            error = IndexError('rate_limited')
            error.retry_at = now[0]+5000
            raise error
        self.providers.callback = limited
        run = self.service.request_index(self.repo,embedding_version_id=self.version)
        worker = Worker(self.jobs,{'index.run':self.service.run_index},worker_id='rate-limit')
        worker.run_once()
        waiting = self.service.get_run(run['id'])
        self.assertEqual(waiting['state'],'waiting',waiting)
        self.assertEqual(waiting['continuation'],1)
        self.assertNotEqual(waiting['job_id'],run['job_id'])
        self.assertFalse(worker.run_once())
        now[0] += 5001
        self.providers.callback = None
        worker.run_once()
        self.assertEqual(self.service.get_run(run['id'])['state'],'completed')

    def test_vector_outage_preserves_lexical_search_and_context_budget(self):
        run = self.index()
        if self.service.vectors.available:
            self.service.vectors.close()
        result = self.service.search('send_order',[self.repo])
        self.assertEqual(result['mode'],'lexical_only')
        self.assertTrue(result['hits'])
        result = self.service.search('send_order',[self.repo],semantic=False,context_token_budget=1)
        self.assertEqual(result['hits'],[])
        self.assertTrue(result['truncated'])

    def test_metadata_candidates_do_not_collide_or_leak_excluded_profile(self):
        self.index()
        first = self.service.register_demo(Snapshot('same-name','old',{'README.md':('shared_identifier',)},True))['repository_id']
        second = self.service.register_demo(Snapshot('same_name','old',{'README.md':('shared_identifier',)},True))['repository_id']
        candidates = self.service.catalog_candidates('same')
        self.assertEqual([c['repository_id'] for c in candidates],[first])
        self.assertEqual(candidates[0]['reasons'],[{'field':'name','terms':['same']}])
        candidates = self.service.catalog_candidates('',[first,second])
        self.assertEqual({c['repository_id'] for c in candidates},{first,second})
        self.assertEqual(self.service.catalog_candidates('Synthetic')[0]['repository_id'],self.repo)
        self.service.set_local_selection(self.repo,True,['README.md'])
        self.assertEqual(self.service.catalog_candidates('README'),[])
        self.assertEqual(self.service.catalog_candidates('Synthetic'),[])



if __name__ == '__main__':
    unittest.main()
