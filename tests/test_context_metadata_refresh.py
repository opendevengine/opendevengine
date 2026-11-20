"""Startup and repository renames preserve meaningful context freshness/identity."""
from unittest import TestCase
from opendevengine.index import Snapshot
from opendevengine.indexing import IndexService
from opendevengine.platform import Platform,Worker


class ContextMetadataRefreshTests(TestCase):
    def test_reregistering_unchanged_demo_does_not_mark_usable_snapshot_stale(self):
        now=[1];clock=lambda:now[0]
        p=Platform.open(None,clock=clock);index=IndexService(p.store,p.jobs,None,None,clock=clock)
        try:
            snapshot=Snapshot('fixture','a'*64,{'src/main.py':('def checkout():','    return True')},demo=True)
            state=index.register_demo(snapshot);index.request_index(state['repository_id'])
            Worker(p.jobs,{'index.run':index.run_index},worker_id='metadata',lease_ms=600000).run_once()
            self.assertEqual(index.get_state('demo:fixture')['status'],'ready')
            now[0]=1000
            self.assertEqual(index.register_demo(snapshot)['status'],'ready')
        finally:index.vectors.close();p.close()

    def test_github_rename_changes_public_name_and_routing_without_changing_id(self):
        class GitHub:
            repo={'id':'gh:1','name':'repo','full_name':'old/repo','description':'Checkout service','topics':[],'implementation_enabled':False}
            def register_eligibility_listener(self,fn):pass
            def register_purge_hook(self,name,fn):pass
            def get_repository(self,ident):return self.repo
            def is_eligible(self,ident):return True
            def list_repositories(self,**kwargs):return {'items':[self.repo],'next_cursor':None}
        p=Platform.open(None);github=GitHub()
        with p.store.transaction() as c:
            c.execute("INSERT INTO repositories(id,github_id,owner,name,full_name,default_branch,visibility,clone_url,created_at,updated_at) VALUES('gh:1',1,'old','repo','old/repo','main','private','https://github.com/old/repo.git',1,1)")
        index=IndexService(p.store,p.jobs,github,None)
        try:
            self.assertEqual(index.get_state('gh:1')['name'],'old/repo')
            github.repo={**github.repo,'name':'renamed','full_name':'new/renamed'}
            state=index.get_state('gh:1')
            self.assertEqual((state['repository_id'],state['name']),('gh:1','new/renamed'))
            self.assertEqual(index.catalog_candidates('new renamed')[0]['name'],'new/renamed')
        finally:index.vectors.close();p.close()
