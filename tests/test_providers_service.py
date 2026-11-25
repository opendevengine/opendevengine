"""Synthetic provider contract checks; no accounts, network or optional packages."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from opendevengine.platform.db import Store
from opendevengine.platform.secrets import BackendStatus, SecretService, UnavailableSecretStore
from opendevengine.providers import (
    ProviderService, TransportResponse, TransportTimeout, ProviderError,
    UrllibTransport, TransportRequest,
)
from opendevengine.providers.transport import _read_bounded, ResponseTooLarge, TransportCancelled


class MemoryVault:
    """Tests only. Never used as a product credential backend."""
    name = 'test-only'
    def __init__(self):
        self.values = {}
    def status(self):
        return BackendStatus(self.name, True, None)
    def put(self, key, value):
        self.values[key] = value
    def get(self, key):
        return self.values[key]
    def delete(self, key):
        self.values.pop(key, None)


def response(payload, status=200, headers=None):
    return TransportResponse(status, headers or {}, json.dumps(payload).encode())


class FakeTransport:
    def __init__(self):
        self.requests = []
        self.queue = []
        self.callback = None
    def send(self, request, *, cancel_event=None):
        self.requests.append(request)
        if self.callback:
            self.callback()
        item = self.queue.pop(0) if self.queue else response({
            'done': True, 'message': {'content': 'ok'}, 'prompt_eval_count': 4, 'eval_count': 2})
        if isinstance(item, Exception):
            raise item
        return item


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store.open(self.directory.name)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(lambda: self.store.close())
        self.vault = MemoryVault()
        self.secrets = SecretService(self.store, self.vault)
        self.transport = FakeTransport()
        self.service = ProviderService(self.store, self.secrets, transport=self.transport, clock=lambda: 100000)
    def spec(self, **changes):
        spec = {'name': 'local', 'adapter': 'ollama', 'endpoint': 'http://localhost:11434',
                'model': 'synthetic', 'config': {'capabilities': ['chat', 'structured_output', 'embeddings'],
                                                'embedding_dimensions': 2}}
        spec.update(changes)
        return spec
    def profile(self, **changes):
        return self.service.create_profile(self.spec(**changes))
    def assign(self, **changes):
        p = self.profile(**changes)
        self.service.assign_role('ask', p['id'])
        return p
    def generate(self, **kwargs):
        return self.service.generate('ask', [{'role': 'user', 'content': 'private synthetic input'}], **kwargs)
    def code(self, expected, call):
        with self.assertRaises(ProviderError) as caught:
            call()
        self.assertEqual(expected, caught.exception.code)
        self.assertNotIn('private synthetic input', str(caught.exception))
        return caught.exception
    def test_assign_roles_sets_several(self):
        p = self.profile()
        roles = self.service.assign_roles({'ask': p['id'], 'planning': p['id'], 'summary': p['id']})
        self.assertEqual({roles[r]['profile_id'] for r in ('ask', 'planning', 'summary')}, {p['id']})

    def test_assign_roles_is_all_or_nothing(self):
        chat = self.profile()
        embedding_only = self.profile(name='embed', config={'capabilities': ['embeddings'], 'embedding_dimensions': 2})
        self.service.assign_roles({'ask': chat['id']})
        other = self.profile(name='other')
        with self.assertRaises(ProviderError):
            self.service.assign_roles({'ask': other['id'], 'planning': embedding_only['id']})
        self.assertEqual(self.service.roles()['ask']['profile_id'], chat['id'])
        self.assertIsNone(self.service.roles()['planning']['profile_id'])

    def test_trusted_host_is_local(self):
        from opendevengine.providers import validation
        self.addCleanup(validation.set_trusted_endpoints, [])
        self.assertEqual(validation.destination_for('http://100.64.0.9:1234/v1')['kind'], 'remote')
        validation.set_trusted_endpoints(['http://100.64.0.9:1234', 'http://192.168.1.20:1234/v1'])
        self.assertEqual(validation.destination_for('http://100.64.0.9:1234/v1')['kind'], 'local')
        self.assertEqual(validation.destination_for('http://192.168.1.20:1234/v1')['kind'], 'local')
        self.assertEqual(validation.validate_endpoint('http://192.168.1.20:1234/v1'), 'http://192.168.1.20:1234/v1')

    def test_trusted_hosts_are_reference_counted(self):
        from opendevengine.providers import validation
        self.addCleanup(validation.set_trusted_endpoints, [])
        validation.set_trusted_endpoints(['http://100.64.0.9:1234'])
        validation.set_trusted_endpoints([])
        self.assertEqual(validation.destination_for('http://100.64.0.9:1234/v1')['kind'], 'remote')

    def test_versions_roles_and_restart(self):
        p = self.assign()
        old = copy.deepcopy(self.service.get_version(p['active_version_id']))
        self.service.assign_role('summary', p['id'])
        q = self.service.update_profile(p['id'], {'model': 'replacement'})
        self.assertEqual(old, self.service.get_version(old['id']))
        self.assertEqual(2, q['version']['version'])
        self.assertEqual(q['active_version_id'], self.generate()['profile_version_id'])
        self.assertEqual(q['active_version_id'], self.service.roles()['summary']['version_id'])
        self.service.update_settings(local_only=False)
        self.store.close()
        self.store = Store.open(self.directory.name)
        self.service = ProviderService(self.store, SecretService(self.store, self.vault), transport=self.transport)
        self.assertFalse(self.service.settings()['local_only'])
        self.assertEqual('replacement', self.generate()['model'])
    def test_namespace_changes_only_with_embedding_identity(self):
        p = self.profile()
        a = self.service.embedding_namespace(p['active_version_id'])
        q = self.service.update_profile(p['id'], {'name': 'renamed'})
        self.assertEqual(a, self.service.embedding_namespace(q['active_version_id']))
        q = self.service.update_profile(p['id'], {'config': {'capabilities': ['embeddings'], 'embedding_dimensions': 3}})
        self.assertNotEqual(a['key'], self.service.embedding_namespace(q['active_version_id'])['key'])
        self.assertEqual(a, self.service.embedding_namespace(p['active_version_id']))
    def test_secret_prerequisites_and_failed_creation_cleanup(self):
        memory = Store.open(None)
        self.addCleanup(memory.close)
        for store, backend in ((memory, self.vault), (self.store, UnavailableSecretStore())):
            service = ProviderService(store, SecretService(store, backend), transport=self.transport)
            self.code('persistence_required', lambda: service.create_profile(self.spec(), secret_value='synthetic-key'))
            self.assertEqual([], service.list_profiles())
        self.profile()
        self.code('profile_name_conflict', lambda: self.service.create_profile(self.spec(), secret_value='synthetic-key'))
        self.assertEqual({}, self.vault.values)
    def test_rotation_deletion_and_nonsecret_history(self):
        p = self.service.create_profile(self.spec(), secret_value='synthetic-old')
        self.service.assign_role('ask', p['id'])
        carried = self.service.update_profile(p['id'], {'model': 'carried'})
        self.generate()
        self.assertEqual('Bearer synthetic-old', self.transport.requests[-1].headers['Authorization'])
        rotated = self.service.update_profile(p['id'], {}, secret_value='synthetic-new')
        self.code('credential_unavailable', lambda: self.service.embed(carried['active_version_id'], ['x']))
        self.generate()
        self.assertEqual('Bearer synthetic-new', self.transport.requests[-1].headers['Authorization'])
        deleted = self.service.delete_profile(p['id'])
        self.assertFalse(deleted['version']['credential_configured'])
        self.assertEqual({}, self.vault.values)
        self.assertEqual('unassigned', self.service.roles()['ask']['status'])
        self.code('profile_deleted', lambda: self.service.embed(rotated['active_version_id'], ['x']))
        self.service.delete_profile(p['id'])
        public = json.dumps(self.service.get_version(p['active_version_id']))
        self.assertNotIn('secret_ref', public)
        self.assertNotIn('synthetic-old', public)
        with self.store.read() as conn:
            dump = '\n'.join(conn.iterdump())
        self.assertNotIn('synthetic-old', dump)
        self.assertNotIn('synthetic-new', dump)
    def test_rotation_revokes_durably_when_cleanup_is_busy(self):
        from opendevengine.platform.errors import StorageBusy, SecretRevoked
        p = self.service.create_profile(self.spec(), secret_value='synthetic-old')
        other = self.service.create_profile(self.spec(name='other'), secret_value='synthetic-other')
        with self.store.read() as conn:
            old_ref = conn.execute('SELECT secret_ref FROM provider_profile_versions WHERE id=?',
                                   (p['active_version_id'],)).fetchone()[0]
        with patch.object(self.secrets, 'revoke', side_effect=StorageBusy('busy')):
            rotated = self.service.update_profile(p['id'], {}, secret_value='synthetic-new')
        self.assertIn(old_ref, self.vault.values)  # cleanup failed, policy still durable
        with self.assertRaises(SecretRevoked):
            self.secrets.resolve(old_ref)
        before = len(self.transport.requests)
        self.code('credential_unavailable', lambda: self.service.embed(p['active_version_id'], ['synthetic']))
        self.assertEqual(before, len(self.transport.requests))
        self.assertFalse(self.service.get_version(p['active_version_id'])['credential_configured'])
        self.assertTrue(self.service.get_version(other['active_version_id'])['credential_configured'])
        self.transport.queue = [response({'embeddings': [[1, 2]]})]
        self.service.embed(rotated['active_version_id'], ['synthetic'])
        self.assertEqual('Bearer synthetic-new', self.transport.requests[-1].headers['Authorization'])
        # A new service after reopening storage must enforce the tombstone too.
        self.store.close()
        self.store = Store.open(self.directory.name)
        self.service = ProviderService(self.store, SecretService(self.store, self.vault), transport=self.transport)
        self.code('credential_unavailable', lambda: self.service.embed(p['active_version_id'], ['synthetic']))

    def test_delete_revokes_owned_refs_even_when_cleanup_is_busy(self):
        from opendevengine.platform.errors import StorageBusy, SecretRevoked
        p = self.service.create_profile(self.spec(), secret_value='synthetic-old')
        carried = self.service.update_profile(p['id'], {'model': 'carried'})
        other = self.service.create_profile(self.spec(name='other'), secret_value='synthetic-other')
        self.service.assign_role('ask', p['id'])
        with self.store.read() as conn:
            owned = [r[0] for r in conn.execute(
                'SELECT DISTINCT secret_ref FROM provider_profile_versions WHERE profile_id=?', (p['id'],))]
        with patch.object(self.secrets, 'revoke', side_effect=StorageBusy('busy')):
            deleted = self.service.delete_profile(p['id'])
        self.assertFalse(deleted['version']['credential_configured'])
        self.assertEqual('unassigned', self.service.roles()['ask']['status'])
        for ref in owned:
            self.assertIn(ref, self.vault.values)
            with self.assertRaises(SecretRevoked):
                self.secrets.resolve(ref)
        for version in (p['active_version_id'], carried['active_version_id']):
            self.code('profile_deleted', lambda: self.service.embed(version, ['synthetic']))
        self.assertEqual([], self.transport.requests)
        self.assertTrue(self.service.get_version(other['active_version_id'])['credential_configured'])

    def test_rotation_rollback_preserves_old_credential_and_active_version(self):
        p = self.service.create_profile(self.spec(), secret_value='synthetic-old')
        other = self.profile(name='occupied')
        self.code('profile_name_conflict', lambda: self.service.update_profile(
            p['id'], {'name': other['name']}, secret_value='synthetic-new'))
        self.assertTrue(self.service.get_version(p['active_version_id'])['credential_configured'])
        self.assertEqual(1, len(self.vault.values))
        self.transport.queue = [response({'embeddings': [[1, 2]]})]
        self.service.embed(p['active_version_id'], ['synthetic'])
        self.assertEqual('Bearer synthetic-old', self.transport.requests[-1].headers['Authorization'])
        self.assertEqual(p['active_version_id'], next(
            profile for profile in self.service.list_profiles() if profile['id'] == p['id'])['active_version_id'])

    def test_later_auth_failure_clears_current_and_previous_verification(self):
        p = self.assign()
        version = p['active_version_id']
        self.transport.queue = [response({'message': {'content': 'ok'}}),
                                response({'embeddings': [[1, 2]]}),
                                response({'message': {'content': '{"ok":true}'}})]
        self.assertEqual('passed', self.service.test_profile(version)['status'])
        self.assertEqual('ready', self.service.roles()['ask']['status'])
        self.transport.queue = [response({'message': {'content': 'ok'}}), response({}, 401)]
        checked = self.service.test_profile(version, capabilities=['chat', 'embeddings'])
        self.assertEqual([], self.transport.queue)
        self.assertEqual('failed', checked['status'])
        self.assertEqual([], checked['verified_capabilities'])
        self.assertEqual('provider_auth', checked['error_code'])
        self.assertEqual('failed', checked['capabilities']['chat']['status'])
        self.assertEqual('provider_auth', checked['capabilities']['embeddings']['error_code'])
        self.assertEqual('failed', self.service.roles()['ask']['status'])
        self.assertEqual([], self.service.get_version(version)['check']['verified_capabilities'])

    def test_local_only_and_source_policy(self):
        p = self.profile(endpoint='https://example.invalid')
        self.code('destination_not_allowed', lambda: self.service.assign_role('ask', p['id']))
        self.code('destination_not_allowed', lambda: self.service.test_profile(p['active_version_id']))
        self.service.update_settings(local_only=False)
        self.service.assign_role('ask', p['id'])
        self.code('destination_not_allowed', lambda: self.generate(local_only=True))
        self.code('source_sharing_not_allowed', self.generate)
        self.code('source_sharing_not_allowed', lambda: self.service.embed(p['active_version_id'], ['source']))
        self.assertEqual([], self.transport.requests)
        self.generate(contains_source=False)
        q = self.service.update_profile(p['id'], {'config': {'capabilities': ['chat'], 'allow_external_source': True}})
        self.generate()
        self.service.update_settings(local_only=True)
        self.assertEqual('blocked', self.service.roles()['ask']['status'])
        self.code('destination_not_allowed', self.generate)
    def test_validation_is_strict_and_content_free(self):
        for endpoint in ('http://example.invalid', 'http://localhost.evil', 'http://127.1',
                         'https://u:p@example.invalid', 'http://localhost/?x', 'http://localhost/#x',
                         'http://localhost/%2e', 'file:///tmp/x'):
            with self.subTest(endpoint=endpoint):
                self.code('validation_failed', lambda: self.profile(endpoint=endpoint))
        for config in ({'capabilities': [[]]}, {'capabilities': ['chat'], 'timeout_seconds': True},
                       {'capabilities': ['embeddings'], 'embedding_dimensions': True},
                       {'capabilities': ['chat'], 'input_price_per_million': float('nan')},
                       {'capabilities': ['chat'], 'timeout_seconds': 10**1000},
                       {'capabilities': ['streaming']}, {'capabilities': ['structured_output']},
                       {'capabilities': ['chat'], 7: 'unknown'}):
            self.code('validation_failed', lambda: self.profile(config=config))
        self.code('validation_failed', lambda: self.profile(adapter='openai_compatible', endpoint='https://example.invalid'))
        p = self.profile()
        self.code('validation_failed', lambda: self.service.test_profile(p['active_version_id'], capabilities=[{}]))
        self.code('validation_failed', lambda: self.service.update_settings(local_only=1))
    def test_checks_are_synthetic_separate_and_invalidate_on_auth_failure(self):
        p = self.assign()
        v = p['active_version_id']
        self.assertIsNone(p['version']['check'])
        self.transport.queue = [response({'message': {'content': 'ok'}}),
                                response({'embeddings': [[1, 2]]}),
                                response({'message': {'content': '{"ok":true}'}})]
        checked = self.service.test_profile(v, capabilities=['chat', 'structured_output', 'embeddings'])
        self.assertEqual('passed', checked['status'])
        self.assertEqual('ready', self.service.roles()['ask']['status'])
        for request in self.transport.requests:
            self.assertIn('test', request.body.decode().lower())
            self.assertNotIn('private synthetic input', request.body.decode())
        self.transport.queue = [response({'error': 'sensitive body'}, 401)]
        checked = self.service.test_profile(v, capabilities=['chat'])
        self.assertEqual([], checked['verified_capabilities'])
        self.assertEqual('failed', self.service.roles()['ask']['status'])
        self.assertEqual(p['version']['config'], self.service.get_version(v)['config'])
    def test_json_parsing_is_not_domain_validation(self):
        self.assign()
        self.transport.queue = [response({'message': {'content': '{"unexpected":42}'}})]
        result = self.generate(schema={'type': 'object', 'required': ['missing']})
        self.assertEqual({'unexpected': 42}, result['json'])
        self.transport.queue = [response({'message': {'content': 'not json'}})]
        self.code('invalid_output', lambda: self.generate(schema={}))
    def test_usage_unknown_and_estimated(self):
        p = self.assign()
        self.assertEqual({'input_tokens': 4, 'output_tokens': 2, 'estimated_cost': None,
                          'cost_status': 'unknown', 'currency': 'USD'}, self.generate()['usage'])
        self.service.update_profile(p['id'], {'config': {'capabilities': ['chat'],
            'input_price_per_million': 2, 'output_price_per_million': 3}})
        self.assertAlmostEqual(14 / 1000000, self.generate()['usage']['estimated_cost'])
        self.transport.queue = [response({'message': {'content': 'ok'}, 'prompt_eval_count': True})]
        self.assertEqual('unknown', self.generate()['usage']['cost_status'])
    def test_finite_vectors_count_and_dimensions(self):
        p = self.profile()
        for vectors in ([[1]], [[1, 2], [3, 4]], [[True, 2]], [[1e309, 2]], [[10**1000, 2]]):
            self.transport.queue = [response({'embeddings': vectors})]
            # Nonfinite JSON itself is invalid; finite malformed vectors mismatch.
            expected = 'invalid_output' if vectors == [[1e309, 2]] else 'embedding_mismatch'
            self.code(expected, lambda: self.service.embed(p['active_version_id'], ['synthetic']))
        self.transport.queue = [response({'embeddings': [[0.5, -2]], 'prompt_eval_count': 3})]
        self.assertEqual([[0.5, -2.0]], self.service.embed(p['active_version_id'], ['synthetic'])['vectors'])
    def test_errors_cancel_limits_and_no_retry(self):
        self.assign()
        for item, code in ((response({}, 401), 'provider_auth'), (response({}, 403), 'provider_auth'),
                           (response({}, 404), 'model_not_found'), (response({}, 500), 'provider_unavailable'),
                           (response({}, 302, {'location': 'https://example.invalid'}), 'upstream_redirect'),
                           (TransportTimeout('secret text'), 'provider_timeout'),
                           (RuntimeError('secret text'), 'provider_unavailable'),
                           (response({'error': {'code': 'context_length_exceeded'}}, 400), 'context_overflow'),
                           (response({'error': {'type': 'tokens', 'code': 'rate_limit_exceeded',
                                                'message': 'Request too large for model on tokens per minute (TPM): '
                                                           'Limit 8000, Requested 9755, please reduce your message size'}}, 413),
                            'context_overflow'),
                           (response({'error': {'code': 'payload_too_large'}}, 413), 'provider_rejected'),
                           (TransportResponse(200, {}, b'x' * (4*1024*1024+1)), 'invalid_output'),
                           (TransportResponse(200, {}, b'{bad'), 'invalid_output')):
            self.transport.queue = [item]
            before = len(self.transport.requests)
            error = self.code(code, self.generate)
            self.assertIsNone(error.__context__)
            self.assertNotIn('secret text', str(error))
            self.assertEqual(before + 1, len(self.transport.requests))
        self.transport.queue = [response({}, 429, {'retry-after': '2'})]
        self.assertEqual(102000, self.code('provider_rate_limited', self.generate).retry_at)
        for retry in ('²', '9' * 5000):
            self.transport.queue = [response({}, 429, {'Retry-After': retry})]
            self.assertIsInstance(self.code('provider_rate_limited', self.generate).retry_at, int)
        event = threading.Event()
        event.set()
        before = len(self.transport.requests)
        self.code('cancelled', lambda: self.generate(cancel_event=event))
        self.assertEqual(before, len(self.transport.requests))
        event.clear()
        self.transport.callback = event.set
        self.code('cancelled', lambda: self.generate(cancel_event=event))
    def test_no_store_lock_during_transport(self):
        self.assign()
        def callback():
            completed = threading.Event()
            def read():
                self.service.settings()
                completed.set()
            thread = threading.Thread(target=read)
            thread.start()
            self.assertTrue(completed.wait(1), 'network held Store lock')
            thread.join(1)
        self.transport.callback = callback
        self.generate()
    def test_compatible_mapping_and_fixtures(self):
        p = self.assign(adapter='openai_compatible', endpoint='http://127.0.0.1:1234/v1')
        fixtures = Path(__file__).parent / 'fixtures/providers'
        self.transport.queue = [TransportResponse(200, {}, (fixtures/'openai_chat.json').read_bytes())]
        self.generate()
        request = self.transport.requests[-1]
        self.assertTrue(request.url.endswith('/v1/chat/completions'))
        self.assertIs(False, json.loads(request.body)['stream'])
        self.transport.queue = [response({'data': [{'index': 1, 'embedding': [3, 4]},
                                                  {'index': 0, 'embedding': [1, 2]}]})]
        self.assertEqual([[1, 2], [3, 4]], self.service.embed(p['active_version_id'], ['a', 'b'])['vectors'])
        self.transport.queue = [response({'choices': [{'finish_reason': 'length', 'message': {'content': 'partial'}}]})]
        self.code('invalid_output', self.generate)

    def test_all_synthetic_response_fixtures(self):
        fixtures = Path(__file__).parent / 'fixtures/providers'
        used = set()
        def load(name, status=200):
            used.add(name)
            return TransportResponse(status, {}, (fixtures / name).read_bytes())
        for adapter, prefix, dimensions, endpoint in (
                ('ollama', 'ollama', 8, 'http://localhost:11434'),
                ('openai_compatible', 'openai', 4, 'http://localhost:1234/v1')):
            p = self.profile(name=prefix, adapter=adapter, endpoint=endpoint,
                             config={'capabilities': ['chat', 'structured_output', 'embeddings'],
                                     'embedding_dimensions': dimensions})
            self.service.assign_role('ask', p['id'])
            self.transport.queue = [load(prefix + '_chat.json')]
            self.assertEqual('ok', self.generate()['text'])
            self.transport.queue = [load(prefix + '_chat_structured.json')]
            self.assertEqual({'ok': True}, self.generate(schema={})['json'])
            name = 'ollama_embed.json' if adapter == 'ollama' else 'openai_embeddings.json'
            self.transport.queue = [load(name)]
            vectors = self.service.embed(p['active_version_id'], ['synthetic a', 'synthetic b'])['vectors']
            self.assertEqual([dimensions, dimensions], [len(v) for v in vectors])
        for name, status, code in (
                ('ollama_error_model_not_found.json', 404, 'model_not_found'),
                ('openai_error_auth.json', 401, 'provider_auth'),
                ('openai_error_rate_limit.json', 429, 'provider_rate_limited'),
                ('openai_error_context_length.json', 400, 'context_overflow')):
            self.transport.queue = [load(name, status)]
            error = self.code(code, self.generate)
            self.assertNotIn('SYNTH', str(error))
        self.assertEqual({p.name for p in fixtures.glob('*.json')}, used)

    def test_storage_errors_are_sanitized(self):
        import sqlite3
        from contextlib import contextmanager
        @contextmanager
        def broken():
            raise sqlite3.OperationalError('private path or source')
            yield
        with patch.object(self.store, 'read', broken):
            error = self.code('provider_unavailable', self.service.list_profiles)
        self.assertEqual(503, error.http_status)
        self.assertIsNone(error.__context__)
        self.assertNotIn('private path', str(error))

    def test_structured_output_rejects_exponent_overflow(self):
        self.assign()
        self.transport.queue = [response({'message': {'content': '{"number":1e999}'}})]
        self.code('invalid_output', lambda: self.generate(schema={}))

    def test_role_capability_and_missing_boundaries(self):
        p = self.profile(config={'capabilities': ['embeddings'], 'embedding_dimensions': 2})
        self.code('unsupported_capability', lambda: self.service.assign_role('ask', p['id']))
        self.code('role_unassigned', self.generate)
        self.code('not_found', lambda: self.service.get_version('missing'))
        self.code('not_found', lambda: self.service.update_profile('missing', {}))
        self.service.assign_role('embedding', p['id'])
        self.service.assign_role('embedding', None)
        self.assertEqual('unassigned', self.service.roles()['embedding']['status'])
        self.assertEqual([], self.transport.requests)

    def test_malformed_responses_and_duplicate_embedding_indexes(self):
        p = self.assign(adapter='openai_compatible', endpoint='http://[::1]:1234/v1')
        for payload in ({}, {'choices': []}, {'choices': [{'message': {'content': None}}]}):
            self.transport.queue = [response(payload)]
            self.code('invalid_output', self.generate)
        self.transport.queue = [response({'data': [{'index': 0, 'embedding': [1, 2]},
                                                  {'index': 0, 'embedding': [3, 4]}]})]
        self.code('embedding_mismatch', lambda: self.service.embed(p['active_version_id'], ['a', 'b']))
        self.transport.queue = [TransportResponse(200, None, b'{}')]
        self.code('provider_unavailable', self.generate)



class TransportTests(unittest.TestCase):
    def test_proxy_disabled_tls_default_redirect_handler(self):
        with patch('opendevengine.providers.transport.build_opener') as build:
            UrllibTransport()
        handlers = build.call_args.args
        self.assertEqual({}, handlers[0].proxies)
        self.assertIsNone(handlers[1].redirect_request(None, None, None, None, None, None))
        self.assertTrue(handlers[2]._context.check_hostname)
    def test_bounded_reader_and_cancellation(self):
        import io
        import time
        with self.assertRaises(ResponseTooLarge):
            _read_bounded(io.BytesIO(b'12345'), 4, time.monotonic()+1, None)
        event = threading.Event()
        event.set()
        with self.assertRaises(TransportCancelled):
            _read_bounded(io.BytesIO(b'x'), 4, time.monotonic()+1, event)


if __name__ == '__main__':
    unittest.main()
