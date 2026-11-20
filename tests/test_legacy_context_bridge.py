"""Persistent selection/purge policy also applies to the original source explorer."""
from threading import Thread
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode
import http.client,json
from unittest import TestCase
from opendevengine.__main__ import RequestHandler,ROOT
from opendevengine.index import CodeIndex,Snapshot,load_demos
from opendevengine.platform import Platform
from opendevengine.services import ApplicationServices
from opendevengine.indexing import IndexService


class LegacyContextBridgeTests(TestCase):
    def setUp(self):
        self.platform=Platform.open(None)
        self.services=ApplicationServices(self.platform)
        self.snapshots=load_demos(ROOT/'examples')
        self.legacy=CodeIndex(self.snapshots)
        self.services.legacy_index=self.legacy
        self.context=IndexService(self.platform.store,self.platform.jobs,self.services.github,self.services.providers)
        self.services.enable_context(self.context)
        self.services.bootstrap_context(self.snapshots,{})
        self.server=ThreadingHTTPServer(('127.0.0.1',0),RequestHandler)
        self.server.index=self.legacy;self.server.platform=self.platform;self.server.services=self.services
        self.server.features=self.services.api;self.server.model=None
        self.thread=Thread(target=self.server.serve_forever,kwargs={'poll_interval':0.01});self.thread.start()
        self.addCleanup(self.stop)
    def stop(self):
        self.server.shutdown();self.thread.join(3);self.server.server_close()
        self.services.close();self.legacy.db.close();self.platform.close()
    def request(self,method,path,body=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            connection.request(method,path,body=json.dumps(body) if body is not None else None,headers={'Content-Type':'application/json'})
            response=connection.getresponse();return response.status,json.loads(response.read())
        finally:connection.close()
    def test_current_exclusions_hide_legacy_search_and_exact_source(self):
        snapshot=self.legacy.snapshots['orders-api']
        path='src/main/java/example/orders/OrderService.java'
        self.context.set_exclusions('demo:orders-api',[path])
        status,body=self.request('GET','/api/source?'+urlencode({'repo':'orders-api','revision':snapshot.revision,'path':path,'start':1,'end':2}))
        self.assertEqual(status,404)
        status,body=self.request('POST','/api/ask',{'repos':['orders-api'],'question':'checkout order created event'})
        self.assertEqual(status,200)
        self.assertNotIn(path,[c['path'] for c in body['sources']])
    def test_disabled_source_is_not_returned_or_usable_through_old_endpoints(self):
        self.context.set_local_selection('demo:orders-api',False)
        status,body=self.request('GET','/api/repos')
        self.assertNotIn('orders-api',[r['name'] for r in body['repos']])
        self.assertEqual(self.request('POST','/api/ask',{'repos':['orders-api'],'question':'checkout'})[0],400)
    def test_purge_removes_old_memory_and_fts_mirror_only_for_target(self):
        status,body=self.request('POST','/api/repositories/demo%3Aorders-api/purge',{})
        self.assertEqual(status,200)
        self.assertNotIn('orders-api',self.legacy.snapshots)
        self.assertEqual(self.legacy.db.execute("SELECT count(*) FROM chunks WHERE repo='orders-api'").fetchone()[0],0)
        self.assertIn('orders-db',self.legacy.snapshots)
        self.assertFalse(self.context.get_state('demo:orders-api')['eligible'])
    def test_inline_secret_policy_applies_before_legacy_evidence_is_built(self):
        secret='ghp_'+'A'*30
        snapshot=Snapshot('sensitive-fixture','a'*64,{'src/app.py':(f'TOKEN = "{secret}"','def checkout(): pass')},demo=True)
        other=CodeIndex([snapshot])
        self.addCleanup(other.db.close)
        self.services.legacy_index=other
        self.services.bootstrap_context([snapshot],{})
        self.assertFalse(self.services.legacy_source_allowed('sensitive-fixture','src/app.py'))
        self.assertEqual(other.search(['sensitive-fixture'],'checkout',allowed_path=self.services.legacy_source_allowed),[])

    def test_pending_cleanup_does_not_block_other_sources_during_bootstrap(self):
        with self.platform.store.transaction() as c:
            c.execute("UPDATE index_sources SET metadata_json='{\"purging\":true}',index_enabled=0 WHERE id='demo:orders-api'")
        self.services.bootstrap_context(self.snapshots,{})
        self.assertFalse(self.services.legacy_source_allowed('orders-api'))
        self.assertNotIn('orders-api',self.legacy.snapshots)
        self.assertTrue(self.services.legacy_source_allowed('orders-db'))
