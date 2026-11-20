"""Real GitHub service/client + indexing with synthetic transport, no live account."""
import base64
import unittest
from opendevengine.github import Response
from opendevengine.indexing import IndexService, IndexError
from opendevengine.platform.worker import Worker
import test_github_service as github_fixture


class GitHubIndexingTests(unittest.TestCase):
    def test_api_snapshot_and_authoritative_selection_hooks(self):
        fixture = github_fixture.GitHubTests(methodName='runTest')
        fixture.setUpClass()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.remote.repos['11'] = [github_fixture.repo(1)]
        fixture.service.refresh(fixture.connection['id'])
        fixture.worker.run_once()
        original = fixture.service.client.transport
        calls = []
        source = b'api_identifier = 1\n'
        def transport(request):
            calls.append(request.path)
            if '/commits/' in request.path:
                return Response(200,{}, {'sha':'a'*40})
            if '/git/trees/' in request.path:
                return Response(200,{}, {'tree':[
                    dict(path='api.py',type='blob',mode='100644',sha='b'*40,size=len(source)),
                    dict(path='.env',type='blob',mode='100644',sha='c'*40,size=5),
                    dict(path='link.py',type='blob',mode='120000',sha='d'*40,size=5),
                    dict(path='module',type='commit',mode='160000',sha='e'*40)]})
            if '/git/blobs/' in request.path:
                self.assertTrue(request.path.endswith('b'*40))
                self.assertFalse(fixture.platform.store.conn.in_transaction)
                return Response(200,{}, {'size':len(source),'encoding':'base64','content':base64.b64encode(source).decode()})
            return original(request)
        fixture.service.client.transport = transport
        service = IndexService(fixture.platform.store,fixture.platform.jobs,fixture.service,None)
        self.addCleanup(service.vectors.close)
        run = service.request_index('gh:1')
        Worker(fixture.platform.jobs,{'index.run':service.run_index},worker_id='index').run_once()
        self.assertEqual(service.get_run(run['id'])['state'],'completed',service.get_run(run['id']))
        result = service.search('api_identifier',['gh:1'],semantic=False)
        self.assertEqual(result['hits'][0]['revision'],'a'*40)
        self.assertEqual(sum('/git/blobs/' in p for p in calls),1)
        self.assertEqual(service.get_state('gh:1')['active_snapshot']['coverage']['skipped']['submodule'],1)
        before = fixture.service.get_repository('gh:1')
        service.set_exclusions('gh:1',['api.py'])
        self.assertFalse(service.search('api_identifier',['gh:1'],semantic=False)['hits'])
        with self.assertRaises(IndexError):
            service.source('gh:1',result['hits'][0]['snapshot_id'],'api.py',1,1)
        after = fixture.service.get_repository('gh:1')
        for key in ('index_enabled','implementation_enabled'):
            self.assertEqual(after[key],before[key])
        service.set_exclusions('gh:1',[])
        fixture.service.update_selection('gh:1',index_enabled=False,implementation_enabled=False)
        with self.assertRaises(IndexError):
            service.search('api_identifier',['gh:1'])
        fixture.service.update_selection('gh:1',index_enabled=True,implementation_enabled=False)
        self.assertTrue(service.search('api_identifier',['gh:1'],semantic=False)['hits'])
        fixture.service.purge_repository('gh:1')
        fixture.worker.run_once()
        with fixture.platform.store.read() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM repository_snapshots').fetchone()[0],0)
