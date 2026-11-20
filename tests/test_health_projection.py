"""Content-free operational projection with real SQLite/provider/index services."""
import json
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from opendevengine.platform import Platform
from opendevengine.platform.workspace import build_health, apply_service_health
from opendevengine.services import ApplicationServices
from opendevengine.indexing import IndexService


class HealthProjectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.platform = Platform.open(self.directory.name)
        self.addCleanup(self.platform.close)
        self.services = ApplicationServices(self.platform)
        self.index = SimpleNamespace(snapshots={'legacy-private-name': None})

    def health(self, services=True):
        result = build_health(self.index, SimpleNamespace(model='legacy-secret-model'), self.platform, now=100)
        return apply_service_health(result, self.services) if services else result

    def assert_private(self, result):
        serialized = json.dumps(result)
        for secret in ('legacy-private-name', 'private-repository', '/private/source', 'SECRET', self.directory.name, 'legacy-secret-model'):
            self.assertNotIn(secret, serialized)

    def test_legacy_only(self):
        result = self.health(False)
        self.assertFalse(result['components']['index']['persistent'])
        self.assertEqual(result['components']['model']['adapter'], 'ollama')
        self.assert_private(result)

    def test_persistent_counts_and_latest_failure(self):
        index = IndexService(self.platform.store, self.platform.jobs, self.services.github, self.services.providers)
        self.addCleanup(index.vectors.close)
        self.services.enable_context(index)
        with self.platform.store.transaction() as db:
            for n in range(4):
                db.execute("INSERT INTO index_sources(id,kind,name,created_at,updated_at) VALUES(?,'demo','private-repository',0,0)", (str(n),))
            db.execute("INSERT INTO repository_snapshots(id,repository_id,revision,revision_kind,state,chunker_version,parser_version,created_at) VALUES('snap','0','SECRET','git','ready','x','x',0)")
            db.execute("UPDATE index_sources SET active_snapshot_id='snap' WHERE id='0'")
            for ident, state, stamp, code in [('1','queued',1,None), ('2','failed',2,'timeout'), ('old','failed',0,'upstream_unavailable')]:
                db.execute("INSERT INTO index_runs(id,repository_id,state,error_code,created_at,updated_at) VALUES(?,?,?,?,?,?)", (ident,'2' if ident=='old' else ident,state,code,stamp,stamp))
        result = self.health()
        self.assertEqual(result['components']['index']['counts'], dict(ready=1,indexing=1,failed=1,not_indexed=1))
        self.assertEqual(result['components']['index']['latest_failure_code'], 'timeout')
        self.assertTrue(result['components']['index']['persistent'])
        self.assertEqual(result['components']['model']['status'], 'not_configured')
        self.assert_private(result)
        # Exercise the real GET composition without requiring a sandbox socket.
        from opendevengine.__main__ import RequestHandler
        handler = object.__new__(RequestHandler)
        handler.path = '/api/health'
        handler.allowed_host = lambda: True
        handler.server = SimpleNamespace(index=self.index, model=None, platform=self.platform, services=self.services)
        responses = []
        handler.send_json = lambda status, body: responses.append((status, body))
        handler.do_GET()
        self.assertEqual(responses[0][0], 200)
        self.assertEqual(responses[0][1]['components']['index']['counts'], result['components']['index']['counts'])
        self.assert_private(responses[0][1])
        with self.platform.store.transaction() as db:
            db.execute("UPDATE index_sources SET index_enabled=0 WHERE id='0'")
        self.assertEqual(self.health()['components']['index']['counts'], dict(ready=0,indexing=1,failed=1,not_indexed=2))

    def test_provider_roles_and_execution_enabled(self):
        providers = self.services.providers
        profile = providers.create_profile(dict(name='SECRET',adapter='ollama',endpoint='http://127.0.0.1:11434',model='SECRET',config={'capabilities':['chat']}))
        providers.assign_role('ask', profile['id'])
        self.services.sessions = SimpleNamespace(store=self.platform.store, runtime_status=lambda: dict(status='running', docker_code='/private/source', agent_code='SECRET', last_recovery={'path':'/private/source','SECRET':'SECRET'}))
        result = self.health()
        model = result['components']['model']
        self.assertEqual(model['roles']['ask'], dict(adapter='ollama',destination='local',status='untested'))
        self.assertEqual(model['roles']['planning']['status'], 'not_configured')
        self.assertEqual(result['components']['execution'], dict(status='available',runtime_status='running',docker_code='not_checked',agent_code='not_checked',active_session_count=0))
        self.assert_private(result)


import test_sessions_fixture as session_fixtures


class ActiveSessionHealthTests(session_fixtures.SessionCase):
    def test_count_changes_when_real_session_leaves_active_states(self):
        _, version = self.ready_plan()
        session = self.sessions.launch(version['id'], version['content_hash'], 'health-count')
        services = ApplicationServices(self.platform)
        services.indexing = self.index
        services.sessions = self.sessions
        legacy = SimpleNamespace(snapshots={})
        def health():
            return apply_service_health(build_health(legacy, None, self.platform, now=100), services)
        result = health()
        self.assertEqual(result['components']['execution']['active_session_count'], 1)
        for value in (self.repo, session['session_id'], 'calc.py', 'Fix add', str(self.data)):
            self.assertNotIn(value, json.dumps(result))
        self.sessions.stop(session['session_id'])
        self.assertEqual(health()['components']['execution']['active_session_count'], 0)
