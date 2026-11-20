"""Local Ollama discovery lists installed models and probes embedding size, loopback only."""
import json
from unittest import TestCase
from opendevengine.api import FeatureAPI
from opendevengine.platform import Platform
from opendevengine.providers.discovery import discover_ollama, probe_embedding_dimensions
from opendevengine.providers.transport import TransportResponse, TransportUnavailable


class FakeTransport:
    def __init__(self, routes=None, *, raise_connection=False):
        self.routes, self.raise_connection, self.requests = routes or {}, raise_connection, []

    def send(self, request, *, cancel_event=None):
        self.requests.append(request)
        if self.raise_connection:
            raise TransportUnavailable('connection refused')
        path = '/' + request.url.split('/', 3)[3]
        return TransportResponse(200, {}, json.dumps(self.routes[path]).encode())


TAGS = {'models': [
    {'name': 'llama3.2:latest', 'details': {'family': 'llama', 'families': ['llama'], 'parameter_size': '3.2B'}},
    {'name': 'nomic-embed-text:latest', 'details': {'family': 'nomic-bert', 'families': ['nomic-bert'], 'parameter_size': '137M'}}]}
PS = {'models': [{'name': 'llama3.2:latest', 'size_vram': 0, 'context_length': 8192}]}


class DiscoveryTests(TestCase):
    def test_discovery_lists_models_and_flags_embeddings(self):
        out = discover_ollama(transport=FakeTransport({'/api/tags': TAGS, '/api/ps': PS}))
        self.assertTrue(out['reachable'])
        self.assertIsNone(out['error_code'])
        by = {m['name']: m for m in out['models']}
        self.assertTrue(by['nomic-embed-text:latest']['embedding'])
        self.assertFalse(by['llama3.2:latest']['embedding'])
        self.assertEqual(by['llama3.2:latest']['gpu'], False)
        self.assertTrue(by['llama3.2:latest']['loaded'])
        self.assertEqual(by['llama3.2:latest']['context_length'], 8192)
        self.assertIsNone(by['nomic-embed-text:latest']['gpu'])
        self.assertFalse(by['nomic-embed-text:latest']['loaded'])
        self.assertEqual(by['llama3.2:latest']['parameter_size'], '3.2B')

    def test_discovery_unreachable_is_not_an_exception(self):
        out = discover_ollama(transport=FakeTransport(raise_connection=True))
        self.assertEqual(out, {'reachable': False, 'models': [], 'error_code': 'provider_unreachable'})

    def test_discovery_rejects_non_loopback(self):
        for url in ('http://example.com:11434', 'https://10.0.0.5:11434', 'file:///etc/passwd'):
            with self.assertRaises(ValueError):
                discover_ollama(url, transport=FakeTransport({}))

    def test_probe_embedding_dimensions(self):
        t = FakeTransport({'/api/embed': {'embeddings': [[0.0] * 768]}})
        self.assertEqual(probe_embedding_dimensions('http://127.0.0.1:11434', 'nomic-embed-text:latest', transport=t), 768)
        body = json.loads(t.requests[0].body)
        self.assertEqual(body, {'model': 'nomic-embed-text:latest', 'input': 'dimension probe'})

    def test_routes_use_provider_transport(self):
        platform = Platform.open(None)
        self.addCleanup(platform.close)

        class Providers:
            transport = FakeTransport({'/api/tags': TAGS, '/api/ps': PS, '/api/embed': {'embeddings': [[0.1] * 4]}})

        api = FeatureAPI(platform, None, Providers())
        listed = api.dispatch('GET', '/api/provider-profiles/discover', '').body
        self.assertEqual(len(listed['models']), 2)
        dims = api.dispatch('POST', '/api/provider-profiles/discover/dimensions',
                            body={'base_url': 'http://127.0.0.1:11434', 'model': 'nomic-embed-text:latest'}).body
        self.assertEqual(dims, {'dimensions': 4})
        with self.assertRaises(ValueError):
            api.dispatch('GET', '/api/provider-profiles/discover', 'base_url=http%3A%2F%2Fexample.com')

    def test_probe_failure_is_a_validation_error(self):
        with self.assertRaises(ValueError):
            probe_embedding_dimensions('http://127.0.0.1:11434', 'llama3.2:latest', transport=FakeTransport(raise_connection=True))
