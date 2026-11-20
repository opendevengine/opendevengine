"""Completed erasure survives normal startup until explicit source re-enablement."""
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest import TestCase
from opendevengine.index import CodeIndex, Snapshot, load_git, load_demos
from opendevengine.indexing import IndexService
from opendevengine.indexing.service import IndexError
from opendevengine.platform import Platform, Worker
from opendevengine.services import ApplicationServices


class ContextPurgeRecoveryTests(TestCase):
    def open_app(self, directory, snapshots, local_roots=None):
        platform = Platform.open(directory)
        services = ApplicationServices(platform)
        index = IndexService(platform.store,platform.jobs,services.github,services.providers)
        services.enable_context(index)
        legacy = CodeIndex([])
        services.legacy_index = legacy
        services.bootstrap_context(snapshots, local_roots or {})
        return platform, services, index, legacy

    def close_app(self, app):
        platform, services, index, legacy = app
        services.close(); legacy.db.close(); platform.close()

    def assert_erased(self, app, repository_id, name):
        platform, services, index, legacy = app
        state = index.get_state(repository_id)
        self.assertTrue(state['purged']); self.assertFalse(state['eligible'])
        self.assertNotIn(name, legacy.snapshots)
        self.assertEqual(legacy.db.execute('SELECT count(*) FROM chunks WHERE repo=?',(name,)).fetchone()[0],0)
        self.assertNotIn(repository_id,services.scheduler.local_markers)
        with platform.store.read() as conn:
            self.assertEqual(json.loads(conn.execute('SELECT metadata_json FROM index_sources WHERE id=?',(repository_id,)).fetchone()[0]),{'purged':True})
            self.assertEqual(conn.execute('SELECT count(*) FROM repository_snapshots WHERE repository_id=?',(repository_id,)).fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT count(*) FROM code_chunks').fetchone()[0],0)

    def test_demo_purge_survives_restart_and_only_explicit_enable_reloads_source(self):
        snapshots = load_demos(Path(__file__).resolve().parent.parent/'opendevengine'/'examples')
        self.assertTrue(all(snapshot.files for snapshot in snapshots))
        original = next(snapshot for snapshot in snapshots if snapshot.name == 'orders-api')
        publisher = 'src/main/java/example/orders/OutboxPublisher.java'
        self.assertEqual(len(original.files), 5)
        self.assertIn('public class OutboxPublisher', '\n'.join(original.files[publisher]))
        with TemporaryDirectory() as directory:
            app = self.open_app(Path(directory), snapshots)
            platform, services, index, legacy = app
            target = 'demo:orders-api'
            index.request_index(target)
            Worker(platform.jobs,{'index.run':index.run_index},worker_id='purge-fixture',lease_ms=600000).run_once()
            self.assertIsNotNone(index.get_state(target)['active_snapshot'])
            services.purge_context(target)
            self.assert_erased(app,target,'orders-api')
            self.assertIn('orders-db',legacy.snapshots)
            self.close_app(app)
            app = self.open_app(Path(directory),snapshots)
            try:
                platform,services,index,legacy = app
                self.assert_erased(app,target,'orders-api')
                self.assertIn('orders-db',legacy.snapshots)
                # Registration alone, even repeated, must not re-cache source.
                index.register_demo(next(s for s in snapshots if s.name=='orders-api'))
                self.assert_erased(app,target,'orders-api')
                response=services.api.dispatch('PATCH','/api/repositories/demo%3Aorders-api/context',body={'index_enabled':True})
                self.assertTrue(response.body['eligible']); self.assertFalse(response.body['purged'])
                self.assertEqual(legacy.snapshots['orders-api'].files, original.files)
                self.assertIn(target,services.scheduler.local_markers)
                with platform.store.read() as conn:
                    cached = json.loads(conn.execute('SELECT metadata_json FROM index_sources WHERE id=?',(target,)).fetchone()[0])['files']
                    self.assertEqual(len(cached), len(original.files))
                    self.assertEqual(cached[publisher], '\n'.join(original.files[publisher]))
                index.request_index(target)
                Worker(platform.jobs,{'index.run':index.run_index},worker_id='restored-fixture',lease_ms=600000).run_once()
                restored = index.get_state(target)['active_snapshot']
                source = index.source(target,restored['id'],publisher,1,20)
                self.assertIn('public class OutboxPublisher', '\n'.join(source['lines']))
                self.assertIn('public void publishPending()', '\n'.join(source['lines']))
            finally:self.close_app(app)

    def test_local_purge_restart_and_enable_use_registered_path_and_committed_source(self):
        with TemporaryDirectory() as directory:
            root=Path(directory); checkout=root/'checkout';checkout.mkdir()
            subprocess.run(['git','init','-q',str(checkout)],check=True)
            (checkout/'app.py').write_text('def checkout_order():\n    return "committed"\n')
            subprocess.run(['git','-C',str(checkout),'add','app.py'],check=True)
            subprocess.run(['git','-C',str(checkout),'-c','user.name=Fixture','-c','user.email=fixture@example.test','commit','-qm','fixture'],check=True)
            snapshot=load_git('local-orders',checkout)
            app=self.open_app(root/'data',[snapshot],{'local-orders':checkout})
            target=app[1].legacy_sources['local-orders']
            app[1].purge_context(target);self.close_app(app)
            # An uncommitted change never becomes restored source.
            (checkout/'app.py').write_text('PRIVATE_UNCOMMITTED = "do not cache"\n')
            app=self.open_app(root/'data',[load_git('local-orders',checkout)],{'local-orders':checkout})
            try:
                self.assert_erased(app,target,'local-orders')
                state=app[1].select_context(target,True)
                self.assertTrue(state['eligible'])
                self.assertIn('committed','\n'.join(app[3].snapshots['local-orders'].files['app.py']))
                self.assertNotIn('PRIVATE_UNCOMMITTED',str(app[3].snapshots))
                self.assertIn(target,app[1].scheduler.local_markers)
                for data in ({'index_enabled':True,'path':'/etc'}, {'index_enabled':True,'local_path':'/etc'}):
                    with self.assertRaises(ValueError):
                        app[1].api.dispatch('PATCH',f'/api/repositories/{target}/context',body=data)
            finally:self.close_app(app)

    def test_failed_cleanup_stays_blocked_and_can_retry_without_restoring_other_source(self):
        snapshots=load_demos(Path(__file__).resolve().parent.parent/'opendevengine'/'examples')
        app=self.open_app(None,snapshots)
        try:
            platform,services,index,legacy=app
            original=index.vectors.purge
            index.vectors.purge=lambda ident: (_ for _ in ()).throw(RuntimeError('synthetic cleanup failure'))
            with self.assertRaises(RuntimeError):services.purge_context('demo:orders-api')
            self.assertTrue(index.get_state('demo:orders-api')['purging'])
            with self.assertRaises(IndexError) as error:services.select_context('demo:orders-api',True)
            self.assertEqual(error.exception.code,'purge_pending')
            services.bootstrap_context(snapshots,{})
            self.assertNotIn('orders-api',legacy.snapshots)
            self.assertIn('orders-db',legacy.snapshots)
            index.vectors.purge=original
            services.purge_context('demo:orders-api')
            self.assert_erased(app,'demo:orders-api','orders-api')
        finally:self.close_app(app)

    def test_unconfigured_purged_source_and_invalid_patterns_cannot_restore_cached_content(self):
        snapshot=Snapshot('not-bundled','a'*64,{'app.py':('def orders(): pass',)},demo=True)
        app=self.open_app(None,[snapshot])
        try:
            platform,services,index,legacy=app
            services.purge_context('demo:not-bundled')
            with self.assertRaises(IndexError):services.select_context('demo:not-bundled',True)
            with self.assertRaises(ValueError):services.select_context('demo:not-bundled',True,['/private'])
            self.assert_erased(app,'demo:not-bundled','not-bundled')
        finally:self.close_app(app)

    def test_github_explicit_reselection_clears_completed_marker_but_not_pending_cleanup(self):
        class GitHub:
            def register_eligibility_listener(self,callback):pass
            def register_purge_hook(self,name,callback):pass
            def is_eligible(self,repository_id):return True
            def get_repository(self,repository_id):
                return {'name':'orders','full_name':'team/orders','implementation_enabled':False}
        platform=Platform.open(None)
        with platform.store.transaction() as conn:
            conn.execute("INSERT INTO repositories(id,github_id,owner,name,full_name,default_branch,visibility,clone_url,created_at,updated_at) VALUES('gh:1',1,'team','orders','team/orders','main','private','https://github.com/team/orders.git',1,1)")
        index=IndexService(platform.store,platform.jobs,GitHub(),None)
        try:
            index.get_state('gh:1');index.purge_repository('gh:1')
            self.assertTrue(index.get_state('gh:1')['purged'])
            index._eligibility_changed('gh:1',True)
            self.assertTrue(index.get_state('gh:1')['eligible'])
            with platform.store.transaction() as conn:
                conn.execute("UPDATE index_sources SET metadata_json='{\"purging\":true}',index_enabled=0 WHERE id='gh:1'")
            index._eligibility_changed('gh:1',True)
            self.assertFalse(index.get_state('gh:1')['eligible'])
            self.assertTrue(index.get_state('gh:1')['purging'])
        finally:index.vectors.close();platform.close()

    def test_disabled_source_can_restore_mirror_after_empty_mirror_startup(self):
        snapshots=load_demos(Path(__file__).resolve().parent.parent/'opendevengine'/'examples')
        with TemporaryDirectory() as directory:
            app=self.open_app(Path(directory),snapshots)
            app[1].select_context('demo:orders-api',False)
            self.close_app(app)
            app=self.open_app(Path(directory),snapshots)
            try:
                self.assertNotIn('orders-api',app[3].snapshots)
                self.assertTrue(app[1].select_context('demo:orders-api',True)['eligible'])
                self.assertIn('orders-api',app[3].snapshots)
            finally:self.close_app(app)
