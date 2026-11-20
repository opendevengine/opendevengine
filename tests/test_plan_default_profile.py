"""Default selection uses active checked profiles; generation saves the choice."""
import unittest
from types import SimpleNamespace
import test_ask_services as ask_fixtures
from opendevengine.execution.profiles import ExecutionProfileService
from opendevengine.platform import Worker, Platform
from opendevengine.services import ApplicationServices
from test_publication_api import Context, Conversations, Sessions, FakePublications


class DefaultProfileTests(unittest.TestCase):
    setUp = ask_fixtures.AskTests.setUp
    tearDown = ask_fixtures.AskTests.tearDown
    work = ask_fixtures.AskTests.work

    def checked_profile(self, status):
        p = self.profiles.create({'name': 'Coding ' + str(len(self.profiles.list_profiles()['items'])), 'adapter': 'claude_code',
                                 'auth_mode': 'cli_login', 'image': 'local:cached',
                                 'allow_external_source': True})
        job = self.jobs.enqueue('fixture.check', {})
        Worker(self.jobs, {'fixture.check': lambda *_: {}}, worker_id='check').run_once()
        with self.store.transaction() as db:
            db.execute('INSERT INTO execution_profile_checks(version_id,job_id,status) VALUES(?,?,?)',
                       (p['active_version_id'], job.id, status))
        return p

    def test_generation_selection_and_saved_versions(self):
        self.providers.update_settings(local_only=False)
        self.profiles = ExecutionProfileService(SimpleNamespace(store=self.store, jobs=self.jobs), self.providers)
        self.plans.default_execution_profile = self.profiles.default_profile_id
        self.plans.execution_preflight = lambda _: {'blocking_reasons': [], 'warnings': []}
        get_state = self.index.get_state
        self.index.get_state = lambda ident: {**get_state(ident), 'implementation_enabled': True}
        self.content['open_questions'] = []
        self.transport.output = self.content
        saved = []
        for label, status, expected in [('zero', None, None), ('failed', 'failed', None),
                                         ('one', 'passed', 'selected'), ('two', 'passed', None)]:
            if status:
                profile = self.checked_profile(status)
            chosen = profile['id'] if expected else None
            self.assertEqual(self.profiles.default_profile_id(), chosen)
            run = self.service.send(self.conv, 'Plan confirmation retries', label, intent='plan')
            self.work()
            self.assertEqual(self.service.get_run(run['id'])['state'], 'completed')
            # Identify the new immutable version independently of list ordering.
            candidates = self.plans.list_plans(self.conv)['items']
            plan = next(p for p in candidates if p['id'] not in {x['id'] for x in saved})
            version = plan['version']
            self.assertEqual(version['content']['execution_context']['execution_profile_id'], chosen)
            self.assertEqual('execution_not_configured' in version['readiness']['blocking_reasons'], chosen is None)
            if chosen:
                self.assertEqual(version['readiness']['state'], 'ready')
            saved.append(plan)
        for plan in saved:
            self.assertEqual(self.plans.get(plan['id'])['version']['content'], plan['version']['content'])

    def test_deleted_and_outdated_checks_are_not_defaults(self):
        self.providers.update_settings(local_only=False)
        self.profiles = ExecutionProfileService(SimpleNamespace(store=self.store, jobs=self.jobs), self.providers)
        profile = self.checked_profile('passed')
        self.assertEqual(self.profiles.default_profile_id(), profile['id'])
        # Renaming preserves the active version's effective checked status.
        self.profiles.update(profile['id'], {'name': 'Renamed'})
        self.assertEqual(self.profiles.default_profile_id(), profile['id'])
        self.profiles.update(profile['id'], {'image': 'different:cached'})
        self.assertIsNone(self.profiles.default_profile_id())
        second = self.checked_profile('passed')
        self.assertEqual(self.profiles.default_profile_id(), second['id'])
        self.profiles.delete(second['id'])
        self.assertIsNone(self.profiles.default_profile_id())

    def test_composition_sets_only_unset_callbacks(self):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                platform = Platform.open(None)
                try:
                    services = ApplicationServices(platform)
                    services.enable_context(Context())
                    callback = lambda *_: 'explicit'
                    plans = SimpleNamespace(execution_preflight=callback if explicit else None,
                                            default_execution_profile=callback if explicit else None)
                    profiles = SimpleNamespace(default_profile_id=lambda: 'profile')
                    sessions = Sessions()
                    sessions.preflight = lambda _: {}
                    services.enable_ask(object(), plans, Conversations())
                    services.enable_execution(profiles, sessions, publications=FakePublications())
                    self.assertIs(plans.default_execution_profile, callback if explicit else profiles.default_profile_id)
                    self.assertIs(plans.execution_preflight, callback if explicit else sessions.preflight)
                finally:
                    platform.close()
