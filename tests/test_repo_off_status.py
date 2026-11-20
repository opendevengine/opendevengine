"""Pilot 'Off' investigation: actual catalog, indexing, API and persistent restart.

These are lifecycle regression checks, not a reproduction of the pilot defect.
GitHub responses and immutable source are synthetic; no live account is used.
"""
import unittest

from opendevengine.context_api import ContextAPI
from opendevengine.github import GitHubService
from opendevengine.indexing import IndexService, IndexError
from opendevengine.platform import Platform
from opendevengine.platform.secrets import SecretService
from opendevengine.platform.worker import Worker
import test_github_service as github_fixture


class RepositoryStatusLifecycleTests(unittest.TestCase):
    def test_selection_refresh_restart_and_access_recovery(self):
        for registration in ('before_selection', 'after_selection', 'listener_after_selection'):
            with self.subTest(registration=registration):
                self.check_lifecycle(registration)

    def check_lifecycle(self, registration):
        fixture = github_fixture.GitHubTests(methodName='runTest')
        fixture.setUpClass()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.remote.repos['11'] = [github_fixture.repo(1)]
        fixture.refresh()
        fixture.service.update_selection('gh:1', index_enabled=False, implementation_enabled=False)

        def reader(source, revision, caps):
            return dict(revision='a'*40, revision_kind='git',
                        files=[dict(path='api.py', content=b'pilot_identifier = 1\n')])

        def indexing():
            service = IndexService(fixture.platform.store, fixture.platform.jobs,
                                   fixture.service, None, snapshot_reader=reader)
            self.addCleanup(service.vectors.close)
            return service

        service = None if registration == 'listener_after_selection' else indexing()
        if registration == 'before_selection':
            self.assertFalse(service.get_state('gh:1')['eligible'])
        fixture.service.update_selection('gh:1', index_enabled=True, implementation_enabled=False)
        service = service or indexing()
        run = service.request_index('gh:1')
        worker = Worker(fixture.platform.jobs, {'index.run': service.run_index}, worker_id='status-index')
        for _ in range(10):
            if service.get_run(run['id'])['state'] == 'completed':
                break
            self.assertTrue(worker.run_once())
        self.assertEqual(service.get_run(run['id'])['state'], 'completed')
        snapshot = service.get_state('gh:1')['active_snapshot']['id']

        def check_visible(index=service, github=None, platform=None):
            github = github or fixture.service
            platform = platform or fixture.platform
            self.assertTrue(github.get_repository('gh:1')['index_enabled'])
            self.assertTrue(github.is_eligible('gh:1'))
            with platform.store.read() as db:
                self.assertEqual(db.execute('SELECT index_enabled FROM index_sources WHERE id=?', ('gh:1',)).fetchone()[0], 1)
            for _ in range(2):  # repeated HTTP projection, including lazy registration
                response = ContextAPI(platform, index).dispatch('GET', '/api/context/repositories')
                state = response.body['items'][0]
                self.assertTrue(state['eligible'])
                self.assertNotEqual(state['status'], 'excluded')
                self.assertEqual(state['active_snapshot']['id'], snapshot)
            self.assertTrue(index.search('pilot_identifier', ['gh:1'], semantic=False)['hits'])

        check_visible()
        fixture.refresh()
        check_visible()
        fixture.attach('11')  # installation metadata/capability refresh
        fixture.refresh()
        check_visible()
        for allowed in (True, False, True):
            fixture.service.update_selection('gh:1', index_enabled=True, implementation_enabled=allowed)
            check_visible()

        # Authoritative access loss must hide retained source while preserving selection.
        fixture.remote.repos['11'] = []
        fixture.refresh()
        self.assertTrue(fixture.service.get_repository('gh:1')['index_enabled'])
        self.assertFalse(fixture.service.is_eligible('gh:1'))
        self.assertEqual(service.get_state('gh:1')['status'], 'excluded')
        self.assertIsNone(service.get_state('gh:1')['active_snapshot'])
        with self.assertRaises(IndexError):
            service.search('pilot_identifier', ['gh:1'], semantic=False)
        fixture.remote.repos['11'] = [github_fixture.repo(1)]
        fixture.refresh()
        check_visible()

        # Close both databases and reconstruct services from the same durable state.
        service.vectors.close()
        fixture.platform.close()
        reopened = Platform.open(fixture.temp.name)
        self.addCleanup(reopened.close)
        secrets = SecretService(reopened.store, fixture.vault, clock=lambda: fixture.now)
        github = GitHubService(reopened.store, reopened.jobs, secrets,
                              transport=fixture.remote, clock=lambda: fixture.now)
        restarted = IndexService(reopened.store, reopened.jobs, github, None, snapshot_reader=reader)
        self.addCleanup(restarted.vectors.close)
        check_visible(restarted, github, reopened)
        github.update_selection('gh:1', index_enabled=False, implementation_enabled=True)
        self.assertFalse(restarted.get_state('gh:1')['eligible'])
        self.assertIsNone(restarted.get_state('gh:1')['active_snapshot'])
        with self.assertRaises(IndexError):
            restarted.search('pilot_identifier', ['gh:1'], semantic=False)
        github.update_selection('gh:1', index_enabled=True, implementation_enabled=True)
        check_visible(restarted, github, reopened)
