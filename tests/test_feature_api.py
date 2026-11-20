"""Verify the actual HTTP boundary, secret separation and diagnostic pagination."""
import http.client
import json
from threading import Thread
from http.server import ThreadingHTTPServer
import unittest

from opendevengine.__main__ import RequestHandler, ROOT
from opendevengine.api import FeatureAPI
from opendevengine.index import CodeIndex, load_demos
from opendevengine.platform import Platform
from opendevengine.platform.errors import PlatformError


class DomainValidation(PlatformError, ValueError):
    code = 'validation_failed'
    http_status = 400


class GitHubStub:
    def __init__(self):
        self.calls = []
    def configure(self, **args):
        self.calls.append(args)
        return {'id': 'con_test', 'status': 'configured'}
    def connections(self):
        return []
    def status_summary(self):
        return {'status': 'not_configured'}
    def list_repositories(self, **args):
        self.calls.append(args)
        return {'items': [], 'next_cursor': None, 'total': 0}
    def finish_connect(self, state, installation_id, *, setup_action=None):
        self.calls.append((state, installation_id))
        if state != 'valid':
            error = PlatformError('private upstream content must not be rendered')
            error.code = 'setup_state_invalid'
            raise error
        return {'status': 'connected'}


class ProviderStub:
    def __init__(self):
        self.calls = []
        self.local_only = True
    def create_profile(self, spec, *, secret_value=None):
        self.calls.append((spec, secret_value))
        if spec['name'] == 'bad':
            raise DomainValidation('private-source-or-token')
        return {'id': 'pro_test', 'credential_configured': secret_value is not None}
    def list_profiles(self):
        return []
    def settings(self):
        return {'local_only': self.local_only}
    def update_settings(self, *, local_only):
        self.local_only = local_only
    def roles(self):
        return {}


class FeatureAPITests(unittest.TestCase):
    def setUp(self):
        self.platform = Platform.open(None)
        self.addCleanup(self.platform.close)
        self.github, self.providers = GitHubStub(), ProviderStub()
        self.api = FeatureAPI(self.platform, self.github, self.providers)

    def test_profile_secret_is_separate_from_saved_spec_and_unknown_fields_rejected(self):
        data = dict(name='local', adapter='ollama', endpoint='http://127.0.0.1:11434', model='test', secret_value='synthetic-secret')
        response = self.api.dispatch('POST', '/api/provider-profiles', body=data)
        spec, secret = self.providers.calls[0]
        self.assertNotIn('secret_value', spec)
        self.assertEqual(secret, 'synthetic-secret')
        self.assertNotIn('synthetic-secret', json.dumps(response.body))
        with self.assertRaises(ValueError):
            self.api.dispatch('POST', '/api/provider-profiles', body={**data, 'execute': 'arbitrary'})

    def test_repository_query_validation_and_fixed_callback_redirect(self):
        response = self.api.dispatch('GET', '/api/repositories', 'index_enabled=true&limit=50')
        self.assertEqual(response.status, 200)
        self.assertIs(self.github.calls[-1]['index_enabled'], True)
        for query in ['limit=0', 'limit=100000', 'owner=a&owner=b', 'index_enabled=maybe', 'execute=1']:
            with self.assertRaises(ValueError):
                self.api.dispatch('GET', '/api/repositories', query)
        for state, expected in [('valid', 'connected'), ('missing-secret-state', 'setup_state_invalid')]:
            response = self.api.dispatch('GET', '/api/github/callback', 'state='+state+'&installation_id=1')
            self.assertEqual(response.status, 303)
            self.assertEqual(response.headers['Location'], '/#/settings?github='+expected)
            self.assertNotIn(state, response.headers['Location'])

    def test_job_pages_are_bounded_and_exclude_payload_checkpoint_and_lease(self):
        for number in range(5):
            self.platform.jobs.enqueue('test.private', {'source': 'private-secret-payload', 'number': number})
        self.platform.jobs.claim('private-owner')
        first = self.api.dispatch('GET', '/api/jobs', 'limit=2').body
        second = self.api.dispatch('GET', '/api/jobs', 'limit=2&cursor='+first['next_cursor']).body
        self.assertEqual(first['total'], 5)
        self.assertEqual(len(first['items']), 2)
        self.assertFalse({j['id'] for j in first['items']} & {j['id'] for j in second['items']})
        text = json.dumps([first, second])
        for sensitive in ['private-secret-payload', 'private-owner', 'lease_token', 'checkpoint', 'payload']:
            self.assertNotIn(sensitive, text)

    def test_role_patch_rejects_mixed_partial_operations_before_policy_change(self):
        with self.assertRaises(ValueError):
            self.api.dispatch('PATCH', '/api/settings/model-roles', body={'local_only': False, 'assignments': {'ask':'missing'}})
        self.assertTrue(self.providers.local_only)


class FeatureHTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), RequestHandler)
        self.server.platform = Platform.open(None)
        self.server.index = CodeIndex(load_demos(ROOT/'examples'))
        self.server.model = None
        self.github, self.providers = GitHubStub(), ProviderStub()
        self.server.features = FeatureAPI(self.server.platform, self.github, self.providers)
        self.thread = Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01})
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown(); self.thread.join(3); self.server.server_close()
        self.server.index.db.close(); self.server.platform.close()

    def request(self, method, path, data=None, **headers):
        headers.setdefault('Content-Type', 'application/json')
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            conn.request(method, path, body=json.dumps(data) if data is not None else None, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            conn.close()

    def test_new_mutations_check_origin_and_media_before_any_service_call(self):
        args = dict(app_id='1', slug='app', private_key='synthetic-secret')
        self.assertEqual(self.request('POST','/api/github/configure',args, Origin='https://attacker.invalid')[0],403)
        self.assertEqual(self.request('PATCH','/api/repositories/gh:1',{}, Origin='https://attacker.invalid')[0],403)
        self.assertEqual(self.request('POST','/api/github/configure',args, **{'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.request('POST','/api/github/configure',args)[0],201)

    def test_domain_errors_keep_stable_code_without_private_message(self):
        status, body, _ = self.request('POST','/api/provider-profiles', dict(name='bad',adapter='ollama',endpoint='http://127.0.0.1',model='test'))
        self.assertEqual(status,400)
        self.assertEqual(body['code'],'validation_failed')
        self.assertNotIn('private-source-or-token',json.dumps(body))
        self.assertEqual(self.request('GET','/api/repositories?limit=0')[0],400)
        status, body, headers = self.request('GET','/api/github/callback?state=invalid&installation_id=1')
        self.assertEqual((status,headers['Location']),(303,'/#/settings?github=setup_state_invalid'))


if __name__ == '__main__':
    unittest.main()
