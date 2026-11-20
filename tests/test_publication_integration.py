"""Real session snapshot -> real publisher -> real GitHubService over a synthetic transport."""
import base64
import hashlib
import json
from unittest import mock
from urllib.parse import unquote

from opendevengine.github import Response
from opendevengine.platform.worker import Worker
from opendevengine.publication import PublicationService
from opendevengine.publication_api import PublicationAPI
import test_sessions_publication_snapshot as snapshot_tests
import test_sessions_fixture
from test_sessions_fixture import Remote, SessionCase

BASE_TREE = 'b' * 40


def object_sha(kind, value):
    return hashlib.sha1((kind + ':' + json.dumps(value, sort_keys=True)).encode()).hexdigest()


class PublishingRemote(Remote):
    """Adds the Git data and pull request endpoints the publisher writes to, recording every write."""

    def __init__(self):
        super().__init__()
        self.blobs, self.trees, self.commits, self.refs, self.pulls = {}, [], {}, {}, []
        self.writes = []
        self.on_write = None

    def _write(self, kind, body):
        self.writes.append((kind, body))
        if self.on_write:
            self.on_write(kind)

    def __call__(self, request):
        path, method, body = request.path, request.method, request.json or {}
        prefix = '/repos/owner/repo1/'
        if not path.startswith(prefix):
            return super().__call__(request)
        rest = path[len(prefix):]
        if method == 'GET' and rest.startswith('git/commits/'):
            sha = rest.rsplit('/', 1)[1]
            if sha == self.commit:
                return Response(200, {}, {'sha': sha, 'tree': {'sha': BASE_TREE}})
            if sha in self.commits:
                return Response(200, {}, {'sha': sha, 'tree': {'sha': self.commits[sha]['tree']}})
            return Response(404, {}, {})
        if method == 'GET' and rest.startswith('compare/'):
            base, head = rest.split('?')[0][len('compare/'):].split('...')
            return Response(200, {}, {'status': 'identical' if base == head else 'ahead', 'files': [], 'total_commits': 0})
        if method == 'POST' and rest == 'git/blobs':
            self._write('blob', body)
            sha = hashlib.sha1(base64.b64decode(body['content'])).hexdigest()
            self.blobs[sha] = body
            return Response(201, {}, {'sha': sha})
        if method == 'POST' and rest == 'git/trees':
            self._write('tree', body)
            self.trees.append(body)
            return Response(201, {}, {'sha': object_sha('tree', body)})
        if method == 'POST' and rest == 'git/commits':
            self._write('commit', body)
            sha = object_sha('commit', body)
            self.commits[sha] = body
            return Response(201, {}, {'sha': sha})
        if method == 'GET' and rest.startswith('git/ref/heads/'):
            branch = unquote(rest[len('git/ref/heads/'):])
            if branch not in self.refs:
                return Response(404, {}, {})
            return Response(200, {}, {'ref': 'refs/heads/' + branch, 'object': {'sha': self.refs[branch]}})
        if method == 'POST' and rest == 'git/refs':
            self._write('ref', body)
            branch = body['ref'][len('refs/heads/'):]
            if branch in self.refs:
                return Response(422, {}, {})
            self.refs[branch] = body['sha']
            return Response(201, {}, {'ref': body['ref'], 'object': {'sha': body['sha']}})
        if method == 'POST' and rest == 'pulls':
            self._write('pull', body)
            number = len(self.pulls) + 1
            pr = {'id': 9000 + number, 'number': number, 'state': 'open', 'draft': body['draft'], 'merged': False,
                  'html_url': 'https://github.com/owner/repo1/pull/%d' % number, 'title': body['title'],
                  'body': body['body'],
                  'head': {'ref': body['head'], 'sha': self.refs[body['head']], 'repo': {'full_name': 'owner/repo1'}},
                  'base': {'ref': body['base'], 'repo': {'full_name': 'owner/repo1'}}}
            self.pulls.append(pr)
            return Response(201, {}, pr)
        if method == 'GET' and rest.startswith('pulls?'):
            return Response(200, {}, [])
        if method == 'GET' and rest.startswith('pulls/'):
            number = int(rest.rsplit('/', 1)[1])
            return Response(200, {}, self.pulls[number - 1])
        return super().__call__(request)


class PublicationIntegrationTests(SessionCase):
    completed = snapshot_tests.PublicationSnapshotTests.completed

    def setUp(self):
        with mock.patch.object(test_sessions_fixture, 'Remote', PublishingRemote):
            super().setUp()
        self.publications = PublicationService(self.platform, self.github, self.sessions)
        self.publications.update_settings('OpenDevEngine Pilot', 'pilot@example.test')
        self.api = PublicationAPI(self.platform, self.publications)

    def run_publication(self):
        Worker(self.jobs, {'publication.run': self.publications.run}, worker_id='publish').run_once()

    def publish(self, sid):
        preview = self.api.dispatch('POST', '/api/sessions/%s/publication-preview' % sid, body={}).body
        self.assertTrue(preview['publishable'], preview['blocking_reasons'])
        self.assertEqual(preview['changed_paths'], ['README.md', 'run.sh'])
        self.assertEqual(preview['title'], 'Fix add')
        self.assertIsInstance(preview['body'], str)
        self.assertIn('## Plan steps', preview['body'])
        self.assertIsInstance(preview['omitted_paths'], list)
        self.assertEqual(preview['intent_version'], 1)
        body = {'title': preview['title'], 'body': preview['body'], 'expected_diff_hash': preview['diff_hash'],
                'intent_version': 1, 'idempotency_key': 'integration-key'}
        return self.api.dispatch('POST', '/api/sessions/%s/pull-requests' % sid, body=body).body

    def test_real_snapshot_publishes_one_draft_pr_with_modes_and_deletions(self):
        sid = self.completed()
        intent = self.publish(sid)
        self.assertEqual(intent['state'], 'queued')
        self.run_publication()
        done = self.api.dispatch('GET', '/api/sessions/%s/publication' % sid).body
        self.assertEqual(done['state'], 'published', done)
        self.assertEqual((done['pull_request_number'], done['pull_request_url']), (1, 'https://github.com/owner/repo1/pull/1'))
        tree = self.remote.trees[0]
        self.assertEqual(tree['base_tree'], BASE_TREE)
        entries = {e['path']: e for e in tree['tree']}
        self.assertEqual(entries['run.sh']['mode'], '100755')
        self.assertIsNone(entries['README.md']['sha'])
        commit = next(iter(self.remote.commits.values()))
        self.assertEqual(commit['parents'], [self.remote.commit])
        self.assertEqual(commit['author']['email'], 'pilot@example.test')
        [ref] = self.remote.refs
        self.assertTrue(ref.startswith('opendevengine/'))
        self.assertNotIn('force', json.dumps(self.remote.writes))
        self.assertEqual([w[0] for w in self.remote.writes if w[0] in ('ref', 'pull')], ['ref', 'pull'])
        self.assertTrue(self.remote.pulls[0]['draft'])
        self.assertIn({'contents': 'write', 'pull_requests': 'write'}, self.remote.token_permissions)
        with self.store.read() as db:
            stored = json.dumps([dict(r) for r in db.execute('SELECT * FROM publications')])
        self.assertNotIn('installation-secret-', stored)

    def interrupt_after_first_blob(self, action):
        client, upload = self.github.client, self.github.client.create_blob
        def upload_then_act(*args):
            # The change lands right after the first blob is confirmed, between external writes.
            sha = upload(*args)
            client.create_blob = upload
            action()
            return sha
        client.create_blob = upload_then_act

    def assert_stopped_after_blob(self, sid, error_code):
        self.assertEqual([w[0] for w in self.remote.writes], ['blob'], 'no tree, commit, ref or PR after the change')
        state = self.api.dispatch('GET', '/api/sessions/%s/publication' % sid).body
        self.assertEqual((state['state'], state['error_code']), ('blocked', error_code), state)

    def test_revoked_implementation_permission_stops_before_tree_ref_and_pr(self):
        sid = self.completed()
        self.publish(sid)
        self.interrupt_after_first_blob(
            lambda: self.github.update_selection(self.repo, index_enabled=True, implementation_enabled=False))
        self.run_publication()
        self.assert_stopped_after_blob(sid, 'source_unavailable')  # the per-blob source check sees revoked access first

    def test_purge_during_upload_stops_before_tree_ref_and_pr(self):
        sid = self.completed()
        self.publish(sid)
        def purge():
            self.github.purge_repository(self.repo)
            self.github_worker.run_once()  # runs registered purge hooks, including publications
        self.interrupt_after_first_blob(purge)
        self.run_publication()
        self.assert_stopped_after_blob(sid, 'repository_purged')

    def test_generated_artifact_is_disclosed_but_never_uploaded(self):
        sid = self.completed(generated=True)
        preview = self.publications.preview(sid)
        self.assertEqual(preview['omitted_paths'], ['__pycache__/x.pyc'])
        self.assertIn('__pycache__/x.pyc', preview['body'])
        self.publish(sid)
        self.run_publication()
        self.assertEqual(self.api.dispatch('GET', '/api/sessions/%s/publication' % sid).body['state'], 'published')
        self.assertEqual(len(self.remote.pulls), 1)
        self.assertTrue(self.remote.pulls[0]['draft'])
        self.assertIn('__pycache__/x.pyc', self.remote.pulls[0]['body'])
        self.assertEqual({e['path'] for e in self.remote.trees[0]['tree']}, {'README.md', 'run.sh'})
        self.assertNotIn(b'generated bytecode', [base64.b64decode(b['content']) for b in self.remote.blobs.values()])
