"""A configured Ollama address is the one non-loopback host treated as local; nothing else widens."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opendevengine.platform.db import Store
from opendevengine.platform.secrets import SecretService
from opendevengine.providers import ProviderError, ProviderService
from opendevengine.providers.discovery import discover_ollama, probe_embedding_dimensions
from opendevengine.providers.validation import (OLLAMA_URL_ENV, destination_for, is_local_host, ollama_url,
                                                parse_ollama_url, validate_endpoint)
from test_provider_discovery import PS, TAGS, FakeTransport as DiscoveryTransport
from test_providers_service import FakeTransport, MemoryVault

TAILNET = 'http://100.102.234.29:11434'


def configured(value):
    return patch.dict('os.environ', {OLLAMA_URL_ENV: value})


class ParseTests(unittest.TestCase):
    def test_accepts_literal_addresses_and_normalizes(self):
        for value, expected in (('http://100.102.234.29:11434', 'http://100.102.234.29:11434'),
                                ('HTTP://100.102.234.29:11434/', 'http://100.102.234.29:11434'),
                                ('https://[fd7a:115c:a1e0::1]:11434', 'https://[fd7a:115c:a1e0::1]:11434'),
                                ('http://localhost:11434', 'http://localhost:11434')):
            with self.subTest(value=value):
                self.assertEqual(expected, parse_ollama_url(value))

    def test_rejects_names_paths_credentials_and_wildcards(self):
        for value in ('http://ryzen-ai:11434', 'http://ollama.example.com', 'http://100.102.234.29:11434/api',
                      'http://u:p@100.102.234.29:11434', 'http://100.102.234.29:11434?x=1', 'ftp://100.102.234.29',
                      'http://0.0.0.0:11434', 'http://224.0.0.1:11434', ' http://100.102.234.29:11434', '', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_ollama_url(value)


class TrustTests(unittest.TestCase):
    def test_unset_means_loopback_only(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual('http://127.0.0.1:11434', ollama_url())
            self.assertFalse(is_local_host('100.102.234.29'))
            with self.assertRaises(ProviderError):
                validate_endpoint(TAILNET)
            self.assertEqual('remote', destination_for('https://100.102.234.29:11434')['kind'])

    def test_configured_ip_is_local_on_any_port_and_nothing_else_is(self):
        with configured(TAILNET):
            self.assertTrue(is_local_host('100.102.234.29'))
            self.assertEqual(TAILNET, validate_endpoint(TAILNET))
            self.assertEqual('local', destination_for('http://100.102.234.29:8080')['kind'])
            for other in ('http://100.102.234.30:11434', 'http://10.0.0.5:11434', 'http://ryzen-ai:11434'):
                with self.subTest(other=other), self.assertRaises(ProviderError):
                    validate_endpoint(other)
            self.assertFalse(is_local_host('ryzen-ai'))

    def test_invalid_setting_fails_closed(self):
        with configured('http://ryzen-ai:11434'):
            self.assertEqual('http://127.0.0.1:11434', ollama_url())
            self.assertFalse(is_local_host('ryzen-ai'))

    def test_ipv6_matches_by_address_not_spelling(self):
        with configured('http://[fd7a:115c:a1e0:0::1]:11434'):
            self.assertTrue(is_local_host('fd7a:115c:a1e0::1'))


class DiscoveryTests(unittest.TestCase):
    def test_discovery_defaults_to_configured_address_and_reports_it(self):
        transport = DiscoveryTransport({'/api/tags': TAGS, '/api/ps': PS})
        with configured(TAILNET):
            out = discover_ollama(transport=transport)
        self.assertEqual(TAILNET, out['base_url'])
        self.assertTrue(all(r.url.startswith(TAILNET + '/') for r in transport.requests))

    def test_discovery_refuses_other_hosts_even_when_configured(self):
        with configured(TAILNET), self.assertRaises(ValueError):
            discover_ollama('http://100.102.234.30:11434', transport=DiscoveryTransport({}))
        with configured(TAILNET), self.assertRaises(ValueError):
            probe_embedding_dimensions('http://10.0.0.5:11434', 'nomic-embed-text:latest', transport=DiscoveryTransport({}))


class LocalOnlyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store.open(directory.name)
        self.addCleanup(self.store.close)
        self.service = ProviderService(self.store, SecretService(self.store, MemoryVault()),
                                       transport=FakeTransport(), clock=lambda: 100000)

    def spec(self, endpoint):
        return {'name': 'tailnet', 'adapter': 'ollama', 'endpoint': endpoint, 'model': 'synthetic',
                'config': {'capabilities': ['chat', 'structured_output']}}

    def test_configured_host_is_usable_in_local_only_mode(self):
        self.assertTrue(self.service.settings()['local_only'])
        with configured(TAILNET):
            profile = self.service.create_profile(self.spec(TAILNET))
            self.service.assign_role('ask', profile['id'])
            self.assertEqual({'kind': 'local', 'host': '100.102.234.29:11434'}, self.service.roles()['ask']['destination'])

    def test_removing_the_setting_blocks_a_saved_profile(self):
        with configured(TAILNET):
            profile = self.service.create_profile(self.spec(TAILNET))
            self.service.assign_role('ask', profile['id'])
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual('remote', self.service.roles()['ask']['destination']['kind'])
            self.assertEqual('blocked', self.service.roles()['ask']['status'])


class CommandLineTests(unittest.TestCase):
    def test_invalid_address_stops_startup(self):
        result = subprocess.run([sys.executable, '-m', 'opendevengine', '--ollama-url', 'http://ryzen-ai:11434',
                                 '--port', '1'], capture_output=True, text=True, timeout=60,
                                cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(2, result.returncode)
        self.assertIn('Use an IP address for Ollama, not a host name.', result.stderr)


if __name__ == '__main__':
    unittest.main()
