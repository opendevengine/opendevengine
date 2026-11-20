"""Publication HTTP boundary and application composition."""
from unittest import TestCase
from opendevengine.platform import Platform
from opendevengine.platform.errors import NotFound
from opendevengine.publication import PublicationError, PublicationService
from opendevengine.publication_api import PublicationAPI
from opendevengine.services import ApplicationServices


class FakePublications:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {'called': name}
        return call


class Context:
    vectors = type('Vectors', (), {'close': lambda self: None})()
    def run_index(self, job, context): return {}


class Conversations:
    def run_answer(self, job, context): return {}
    def purge_repository(self, ident): return None


class Sessions:
    def start(self): return {'status': 'running'}
    def close(self): return {'status': 'stopped'}
    def runtime_status(self): return {'status': 'running'}


def compose(platform, **execution):
    services = ApplicationServices(platform)
    services.enable_context(Context())
    services.enable_ask(object(), object(), Conversations())
    services.enable_execution(object(), Sessions(), **execution)
    return services


class PublicationAPITests(TestCase):
    def setUp(self):
        self.platform = Platform.open(None)
        self.addCleanup(self.platform.close)
        self.fake = FakePublications()
        self.api = PublicationAPI(self.platform, self.fake)

    def test_routes_map_to_service_with_exact_fields(self):
        body = {'title': 'T', 'body': '', 'expected_diff_hash': 'a' * 64, 'intent_version': 1, 'idempotency_key': 'k1'}
        self.assertEqual(self.api.dispatch('POST', '/api/sessions/s%2F1/publication-preview', body={}).status, 200)
        self.assertEqual(self.api.dispatch('POST', '/api/sessions/s1/pull-requests', body=body).status, 202)
        self.assertEqual(self.api.dispatch('GET', '/api/publications/pub_1').status, 200)
        self.assertEqual(self.api.dispatch('POST', '/api/publications/pub_1/retry', body={'idempotency_key': 'k2'}).status, 202)
        self.assertEqual(self.api.dispatch('POST', '/api/publications/pub_1/refresh', body={}).status, 200)
        self.assertEqual([c[0] for c in self.fake.calls], ['preview', 'publish', 'get', 'retry', 'refresh'])
        self.assertEqual(self.fake.calls[0][1], ('s/1',))
        self.assertEqual(self.fake.calls[1][2], body)
        self.assertEqual(self.fake.calls[3], ('retry', ('pub_1',), {'idempotency_key': 'k2'}))

    def test_bodies_and_queries_are_exact(self):
        full = {'title': 'T', 'body': '', 'expected_diff_hash': 'a' * 64, 'intent_version': 1, 'idempotency_key': 'k'}
        for path, body in (('/api/sessions/s1/publication-preview', {'x': 1}),
                           ('/api/sessions/s1/pull-requests', {**full, 'base_branch': 'main'}),
                           ('/api/sessions/s1/pull-requests', {k: v for k, v in full.items() if k != 'title'}),
                           ('/api/publications/p/retry', {}),
                           ('/api/settings/publication', {'commit_name': 'A'})):
            method = 'PATCH' if path.endswith('publication') else 'POST'
            with self.assertRaises(ValueError, msg=path):
                self.api.dispatch(method, path, body=body)
        with self.assertRaises(ValueError):
            self.api.dispatch('GET', '/api/publications/p', 'token=1')
        self.assertEqual(self.fake.calls, [])

    def test_unknown_paths_fall_through(self):
        self.assertIsNone(self.api.dispatch('GET', '/api/sessions/s1'))
        self.assertIsNone(self.api.dispatch('DELETE', '/api/publications/p'))
        self.assertIsNone(PublicationAPI(self.platform, None).dispatch('GET', '/api/publications/p'))

    def test_session_lookup_is_not_found_without_an_intent(self):
        with self.assertRaises(NotFound):
            self.api.dispatch('GET', '/api/sessions/s1/publication')


class PublicationCompositionTests(TestCase):
    def test_execution_composes_the_reviewed_publication_service(self):
        platform = Platform.open(None)
        self.addCleanup(platform.close)
        services = compose(platform)
        self.assertIsInstance(services.publications, PublicationService)
        self.assertEqual(services.handlers['publication.run'], services.publications.run)
        self.assertEqual(services.github.purge_hooks['publications'], services.publications.purge_repository)
        saved = services.api.dispatch('PATCH', '/api/settings/publication',
                                      body={'commit_name': 'OpenDevEngine Pilot', 'commit_email': 'pilot@example.test'})
        self.assertEqual(saved.body, {'commit_name': 'OpenDevEngine Pilot', 'commit_email': 'pilot@example.test'})
        self.assertEqual(services.api.dispatch('GET', '/api/settings/publication').body['commit_name'], 'OpenDevEngine Pilot')
        with self.assertRaises(PublicationError) as error:
            services.api.dispatch('PATCH', '/api/settings/publication', body={'commit_name': 'A', 'commit_email': 'nope'})
        self.assertEqual(error.exception.http_status, 400)

    def test_injected_publications_are_used(self):
        platform = Platform.open(None)
        self.addCleanup(platform.close)
        fake = FakePublications()
        services = compose(platform, publications=fake)
        self.assertIs(services.publications, fake)
        self.assertEqual(services.api.dispatch('GET', '/api/publications/p').body, {'called': 'get'})


class PublicationRecoveryTests(TestCase):
    def test_start_recovers_publications_before_workers_run(self):
        platform = Platform.open(None)
        self.addCleanup(platform.close)
        fake = FakePublications()
        services = compose(platform, publications=fake)
        services.start()
        try:
            self.assertIn('recover', [c[0] for c in fake.calls])
        finally:
            services.close()


class PlanPreflightWiringTests(TestCase):
    def test_execution_connects_plan_readiness_to_session_preflight(self):
        platform = Platform.open(None)
        self.addCleanup(platform.close)
        class Plans:
            execution_preflight = None
        class PreflightSessions(Sessions):
            def preflight(self, content): return {'blocking_reasons': [], 'warnings': []}
        services = ApplicationServices(platform)
        services.enable_context(Context())
        plans, sessions = Plans(), PreflightSessions()
        services.enable_ask(object(), plans, Conversations())
        services.enable_execution(object(), sessions, publications=FakePublications())
        self.assertEqual(plans.execution_preflight, sessions.preflight)

    def test_an_explicit_plan_preflight_is_kept(self):
        platform = Platform.open(None)
        self.addCleanup(platform.close)
        explicit = lambda content: {'blocking_reasons': ['x'], 'warnings': []}
        class Plans:
            pass
        class PreflightSessions(Sessions):
            def preflight(self, content): return {}
        services = ApplicationServices(platform)
        services.enable_context(Context())
        plans = Plans()
        plans.execution_preflight = explicit
        services.enable_ask(object(), plans, Conversations())
        services.enable_execution(object(), PreflightSessions(), publications=FakePublications())
        self.assertIs(plans.execution_preflight, explicit)
