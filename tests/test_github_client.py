"""Authentication, transport and bounded source helper failures."""
import base64
import json
import unittest
from unittest.mock import patch
from opendevengine.github import GitHubClient, GitHubError, Request, Response
from opendevengine.github.client import private_key, transport


class ClientTests(unittest.TestCase):
    def client(self, response):
        return GitHubClient(transport=lambda request: response, clock=lambda: 100000)

    def test_status_failures_are_content_free_and_retry_has_time(self):
        for status, expected in [(302, 'malformed_output'), (401, 'auth'), (403, 'auth'), (404, 'not_found'), (502, 'upstream_unavailable')]:
            with self.subTest(status=status), self.assertRaises(GitHubError) as caught:
                self.client(Response(status, {}, {'secret': 'NEVER_ECHO'})).app('SECRET')
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn('NEVER_ECHO', str(caught.exception))
        with self.assertRaises(GitHubError) as caught:
            self.client(Response(429, {'Retry-After': '2'}, {})).app('SECRET')
        self.assertEqual(caught.exception.retry_at, 102000)

    def test_timeout_and_arbitrary_transport_exceptions_are_sanitized(self):
        for failure, code in [(TimeoutError('SECRET'), 'timeout'), (RuntimeError('SECRET'), 'upstream_unavailable')]:
            def fail(request): raise failure
            with self.assertRaises(GitHubError) as caught:
                GitHubClient(transport=fail, clock=lambda: 0).app('SECRET')
            self.assertEqual(caught.exception.code, code)
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn('SECRET', str(caught.exception))

    def test_blob_size_tree_truncation_and_sha_validation(self):
        client = self.client(Response(200, {}, {'size': 3, 'encoding': 'base64', 'content': base64.b64encode(b'abc').decode()}))
        self.assertEqual(client.blob('token', 'owner', 'repo', 'a'*40), b'abc')
        with self.assertRaises(GitHubError): client.blob('token', 'owner', 'repo', 'a'*40, max_bytes=2)
        with self.assertRaises(GitHubError): client.blob('token', 'owner', 'repo', '../bad')
        with self.assertRaises(GitHubError): self.client(Response(200, {}, {'truncated': True, 'tree': []})).tree('token', 'owner', 'repo', 'a'*40)
        self.assertEqual(self.client(Response(200, {}, {'sha': 'a'*40})).default_branch_sha('token', 'owner', 'repo', 'feature/name'), 'a'*40)

    def test_bad_keys_and_repr(self):
        for pem in ('secret', '', 'a'*16385, None):
            with self.assertRaises(GitHubError): private_key(pem)
        self.assertNotIn('SECRET', repr(Request('GET', '/app', {'Authorization': 'SECRET'})))
        self.assertNotIn('SECRET', repr(Response(401, {}, {'token': 'SECRET'})))

    def test_no_redirect_proxy_and_bounded_body(self):
        class Body:
            code = 200
            headers = {}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b'x'*size
        with patch('opendevengine.github.client.request.build_opener') as build:
            build.return_value.open.return_value = Body()
            with self.assertRaises(GitHubError): transport(Request('GET', '/app', {}, max_bytes=10))
            proxy, redirect = build.call_args.args
            self.assertEqual(proxy.proxies, {})
            self.assertIsNone(redirect.redirect_request(None, None, 302, '', {}, 'https://evil.invalid'))

    def test_arbitrary_paths_cannot_retarget_credentials(self):
        client = self.client(Response(200, {}, {}))
        for path in ('https://evil.invalid', '//evil.invalid', '/app\r\nsecret', '/\\evil.invalid'):
            with self.assertRaises(GitHubError): client.request('GET', path, 'SECRET')

    def test_publication_uses_base_tree_new_ref_and_draft_pr_without_force(self):
        requests=[]
        values=iter([
            Response(200,{}, {'sha':'a'*40}),
            Response(200,{}, {'sha':'b'*40}),
            Response(201,{}, {'object':{'sha':'b'*40}}),
            Response(201,{}, {'number':7}),
        ])
        def send(req):requests.append(req);return next(values)
        client=GitHubClient(transport=send,clock=lambda:0)
        self.assertEqual(client.create_blob('SECRET','acme','repo',b'content'),'a'*40)
        entries=[{'path':'bin/run','mode':'100755','type':'blob','sha':'a'*40}]
        self.assertEqual(client.create_tree('SECRET','acme','repo','c'*40,entries),'b'*40)
        self.assertTrue(client.create_ref('SECRET','acme','repo','opendevengine/ses-one','b'*40))
        client.create_pull_request('SECRET','acme','repo','Title','Body','opendevengine/ses-one','main')
        self.assertEqual(requests[1].json['base_tree'],'c'*40)
        self.assertEqual(requests[2].json,{'ref':'refs/heads/opendevengine/ses-one','sha':'b'*40})
        self.assertNotIn('force',requests[2].json)
        self.assertEqual(requests[3].json['draft'],True)
        self.assertNotIn('SECRET',repr(requests))

    def test_publication_bounds_and_300_file_comparison_fail_closed(self):
        files=[{'filename':f'f{i}'} for i in range(300)]
        result=self.client(Response(200,{}, {'files':files,'total_commits':1})).compare(
            'token','acme','repo','a'*40,'b'*40)
        self.assertFalse(result['complete'])
        bad=[{'path':'x','mode':'100644','type':'blob','sha':'bad'}]
        with self.assertRaises(GitHubError):self.client(Response(200,{},{})).create_tree('t','a','b','c'*40,bad)
        with self.assertRaises(GitHubError):self.client(Response(200,{},{})).create_ref('t','a','b','main','bad')

    def test_publication_compare_requires_ancestor_status(self):
        for status in ('ahead','identical','behind','diverged',None):
            with self.subTest(status=status):
                result=self.client(Response(200,{},{'files':[], 'total_commits':1,'status':status})).compare(
                    'token','acme','repo','a'*40,'b'*40)
                self.assertEqual(result['complete'],status in ('ahead','identical'))

    def test_publication_compare_previous_filename_shape(self):
        for previous in (False,123,[],{}):
            with self.subTest(previous=previous), self.assertRaises(GitHubError) as caught:
                self.client(Response(200,{}, {'files':[{'filename':'new','previous_filename':previous}],
                    'total_commits':1,'status':'ahead'})).compare('t','a','b','a'*40,'b'*40)
            self.assertEqual(caught.exception.code,'malformed_output')
        for entry in ({'filename':'new'},{'filename':'new','previous_filename':None},
                      {'filename':'new','previous_filename':'old'}):
            self.assertTrue(self.client(Response(200,{}, {'files':[entry],'total_commits':1,'status':'ahead'}))
                            .compare('t','a','b','a'*40,'b'*40)['complete'])
