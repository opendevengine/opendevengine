"""Context HTTP mapping preserves snapshot identity and rejects host-path inputs."""
from unittest import TestCase
from opendevengine.api import FeatureAPI
from opendevengine.platform import Platform


class IndexStub:
    def __init__(self):
        self.calls=[]
    def source(self,**kwargs):
        self.calls.append(kwargs)
        return {'snapshot_id':kwargs['snapshot_id'],'revision':'old-immutable-commit','lines':['original']}
    def request_index(self,repository_id,**kwargs):
        self.calls.append((repository_id,kwargs))
        return {'id':'run-1','repository_id':repository_id,'state':'queued'}
    def pause(self,run_id):
        return {'id':run_id,'state':'paused'}
    def set_exclusions(self,repository_id,exclusions):
        self.calls.append((repository_id,exclusions));return {'repository_id':repository_id,'status':'stale'}
    def get_state(self,repository_id):
        return {'repository_id':repository_id,'kind':'demo','name':'sample','status':'ready'}


class ContextAPITests(TestCase):
    def setUp(self):
        self.platform=Platform.open(None)
        self.addCleanup(self.platform.close)
        self.index=IndexStub()
        self.api=FeatureAPI(self.platform,None,None,indexing=self.index)

    def test_exact_snapshot_source_query_and_no_hostpath_or_duplicate_fields(self):
        query='repository_id=demo%3Ax&snapshot_id=old&path=src%2Fx.py&start_line=2&end_line=3'
        response=self.api.dispatch('GET','/api/context/source',query)
        self.assertEqual(response.body['snapshot_id'],'old')
        self.assertEqual(self.index.calls[-1]['start_line'],2)
        self.assertEqual(self.index.calls[-1]['path'],'src/x.py')
        for invalid in (query+'&local_path=/etc/passwd',query+'&snapshot_id=new',query.replace('start_line=2','start_line=abc')):
            with self.assertRaises(ValueError):
                self.api.dispatch('GET','/api/context/source',invalid)

    def test_index_controls_are_explicit_and_do_not_register_arbitrary_local_paths(self):
        response=self.api.dispatch('POST','/api/repositories/gh%3A42/index',body={'priority':10})
        self.assertEqual(response.status,202)
        self.assertEqual(self.index.calls[-1],('gh:42',{'priority':10}))
        self.assertEqual(self.api.dispatch('POST','/api/index-jobs/run-1/pause',body={}).body['state'],'paused')
        for body in ({'path':'/etc'},{'command':'rm -rf /'},{'token':'secret'}):
            with self.assertRaises(ValueError):
                self.api.dispatch('POST','/api/repositories/gh:42/index',body=body)
        self.assertIsNone(self.api.dispatch('POST','/api/context/register-local',body={'path':'/etc'}))

    def test_context_catalog_is_bounded_and_never_reads_local_path_into_response(self):
        with self.platform.store.transaction() as c:
            for n in range(3):
                c.execute("INSERT INTO index_sources(id,kind,name,local_path,created_at,updated_at) VALUES(?,'local','sample',?,1,1)",(f'local:{n}',f'/private/root/{n}'))
        first=self.api.dispatch('GET','/api/context/repositories','limit=2').body
        second=self.api.dispatch('GET','/api/context/repositories','limit=2&cursor='+first['next_cursor']).body
        self.assertEqual(len(first['items']),2)
        self.assertEqual(len(second['items']),1)
        self.assertFalse({v['repository_id'] for v in first['items']}&{v['repository_id'] for v in second['items']})
        self.assertNotIn('/private',str(first))

    def test_exclusions_return_only_explicit_patterns_and_purge_uses_composed_callback(self):
        with self.platform.store.transaction() as c:
            c.execute("INSERT INTO index_sources(id,kind,name,local_path,exclusions_json,metadata_json,created_at,updated_at) VALUES('local:x','local','sample','/private/root','[\"vendor/**\"]','{\"private_source\":\"secret\"}',1,1)")
        response=self.api.dispatch('GET','/api/repositories/local%3Ax/exclusions')
        self.assertEqual(response.body,{'exclusions':['vendor/**']})
        response=self.api.dispatch('PATCH','/api/repositories/local%3Ax/exclusions',body={'exclusions':['new/**']})
        self.assertEqual(self.index.calls[-1],('local:x',['new/**']))
        with self.assertRaises(ValueError):
            self.api.dispatch('PATCH','/api/repositories/local:x/exclusions',body={'exclusions':[],'local_path':'/etc'})
        called=[]
        api=FeatureAPI(self.platform,None,None,indexing=self.index,purge_context=lambda ident:called.append(ident) or {'state':'completed'})
        self.assertEqual(api.dispatch('POST','/api/repositories/local:x/purge',body={}).body['state'],'completed')
        self.assertEqual(called,['local:x'])


class ContextCatalogDemoTests(TestCase):
    def setUp(self):
        self.platform=Platform.open(None)
        self.addCleanup(self.platform.close)
        self.api=FeatureAPI(self.platform,None,None,indexing=IndexStub())
        with self.platform.store.transaction() as c:
            c.execute("INSERT INTO index_sources(id,kind,name,created_at,updated_at) VALUES('demo:orders-api','demo','orders-api',1,1)")
            c.execute("INSERT INTO index_sources(id,kind,name,local_path,created_at,updated_at) VALUES('local:tool','local','tool','/x',1,1)")

    def ids(self):
        return {v['repository_id'] for v in self.api.dispatch('GET','/api/context/repositories','').body['items']}

    def connect(self,status):
        with self.platform.store.transaction() as c:
            c.execute("INSERT INTO secret_refs(id,backend,purpose,label,created_at) VALUES('sr1','test','github_app_private_key','GitHub App',1)")
            c.execute("INSERT INTO github_connections(id,app_id,slug,private_key_ref,status,created_at,updated_at) VALUES('ghc1','1','s','sr1',?,1,1)",(status,))

    def test_demo_rows_listed_without_github_connection(self):
        self.assertEqual(self.ids(),{'demo:orders-api','local:tool'})

    def test_demo_rows_hidden_once_github_connected(self):
        self.connect('connected')
        self.assertEqual(self.ids(),{'local:tool'})

    def test_demo_rows_kept_while_github_only_configured(self):
        self.connect('configured')
        self.assertIn('demo:orders-api',self.ids())
