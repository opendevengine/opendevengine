"""Live launch freshness decisions and durable retries."""
import threading
import time
from unittest.mock import patch

from opendevengine.session_api import ExecutionAPI
from opendevengine.sessions import SessionError
from test_sessions_fixture import SessionCase


class LaunchFreshnessTests(SessionCase):
    def launch(self, version, key='freshness', **kwargs):
        return self.sessions.launch(version['id'], version['content_hash'], key, **kwargs)

    def test_equal_head_proceeds_and_pins(self):
        _, version = self.ready_plan()
        with patch.object(self.github.client, 'default_branch_sha', return_value='c' * 40):
            result = self.launch(version)
        session = self.sessions.get(result['session_id'])
        self.assertEqual(session['repositories'][0]['base_sha'], 'c' * 40)
        self.assertEqual(session['instructions']['base_observation']['status'], 'current')
        self.assertEqual(session['instructions']['acknowledged_base'], {})

    def test_advanced_rejection_survives_new_head_and_service_restart(self):
        _, version = self.ready_plan()
        with patch.object(self.github.client, 'default_branch_sha', return_value='d' * 40):
            with self.assertRaises(SessionError) as caught:
                self.launch(version)
        error = caught.exception
        self.assertEqual(error.code, 'base_advanced')
        self.assertEqual(error.details[0]['current_head'], 'd' * 40)
        with self.store.read() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='session.run'").fetchone()[0], 0)
        other = self.make_service()
        with patch.object(self.github.client, 'default_branch_sha', side_effect=AssertionError('must not observe twice')):
            with self.assertRaises(SessionError) as repeated:
                other.launch(version['id'], version['content_hash'], 'freshness')
        self.assertEqual(repeated.exception.details, error.details)
        ack = {self.repo: 'c' * 40}
        result = self.launch(version, acknowledged_base=ack)
        self.assertEqual(self.sessions.get(result['session_id'])['instructions']['acknowledged_base'], ack)
        self.assertEqual(self.launch(version, acknowledged_base=ack), result)
        with self.assertRaises(SessionError) as original:
            self.launch(version)
        self.assertEqual(original.exception.details, error.details)

    def test_unobservable_head_proceeds_and_records(self):
        _, version = self.ready_plan()
        with patch.object(self.github.client, 'default_branch_sha', side_effect=TimeoutError):
            result = self.launch(version)
        instructions = self.sessions.get(result['session_id'])['instructions']
        self.assertEqual(instructions['base_observation']['status'], 'unavailable')
        self.assertIsNone(instructions['base_observation']['current_head'])
        with patch.object(self.github.client, 'default_branch_sha', return_value='e' * 40) as lookup:
            self.assertEqual(self.launch(version), result)
            lookup.assert_not_called()

    def test_slow_observation_is_bounded(self):
        _, version = self.ready_plan()
        release = threading.Event()
        def slow(*args):
            release.wait(5)
            return 'd' * 40
        try:
            with patch.object(self.github.client, 'default_branch_sha', side_effect=slow):
                started = time.monotonic()
                result = self.launch(version)
                self.assertLess(time.monotonic() - started, 3)
                self.assertEqual(self.sessions.get(result['session_id'])['instructions']['base_observation']['status'], 'unavailable')
        finally:
            release.set()

    def test_api_409_shape_and_acknowledged_creation(self):
        _, version = self.ready_plan()
        api = ExecutionAPI(sessions=self.sessions)
        body = dict(plan_version_id=version['id'], content_hash=version['content_hash'], idempotency_key='api-fresh')
        with patch.object(self.github.client, 'default_branch_sha', return_value='d' * 40):
            response = api.dispatch('POST', '/api/sessions', body=body)
        self.assertEqual(response.status, 409)
        self.assertEqual(response.body['code'], 'base_advanced')
        self.assertEqual(response.body['details'][0], dict(repository_id=self.repo, pinned_revision='c' * 40, current_head='d' * 40, status='advanced'))
        response = api.dispatch('POST', '/api/sessions', body=dict(body, acknowledged_base={self.repo: 'c' * 40}))
        self.assertEqual(response.status, 202)

    def test_wrong_ack_does_not_authorize_another_base(self):
        _, version = self.ready_plan()
        with self.assertRaises(SessionError) as caught:
            self.launch(version, acknowledged_base={self.repo: 'd' * 40})
        self.assertEqual(caught.exception.code, 'validation')

    def failed_current_session(self):
        _, version = self.ready_plan()
        with patch.object(self.github.client, 'default_branch_sha', return_value='c' * 40):
            sid = self.launch(version)['session_id']
        with self.store.transaction() as db:
            db.execute("UPDATE sessions SET state='failed' WHERE id=?", (sid,))
        return sid

    def test_retry_after_advance_requires_ack_and_replays_durably(self):
        sid = self.failed_current_session()
        with patch.object(self.github.client, 'default_branch_sha', return_value='d' * 40):
            with self.assertRaises(SessionError) as refused:
                self.sessions.retry(sid, 'retry-fresh')
        self.assertEqual(refused.exception.code, 'base_advanced')
        with self.store.read() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE kind='session.run'").fetchone()[0], 1)
        other = self.make_service()
        ack = {self.repo: 'c' * 40}
        with patch.object(self.github.client, 'default_branch_sha', side_effect=AssertionError('reobserved')):
            with self.assertRaises(SessionError) as repeated:
                other.retry(sid, 'retry-fresh')
            self.assertEqual(repeated.exception.details, refused.exception.details)
            for invalid in ({self.repo: 'd' * 40}, [], {self.repo: 1}):
                with self.assertRaises(SessionError) as error:
                    other.retry(sid, 'retry-fresh', acknowledged_base=invalid)
                self.assertEqual(error.exception.code, 'validation')
            result = other.retry(sid, 'retry-fresh', acknowledged_base=ack)
            self.assertEqual(other.retry(sid, 'retry-fresh', acknowledged_base=ack), result)
            with self.assertRaises(SessionError) as original:
                other.retry(sid, 'retry-fresh')
            self.assertEqual(original.exception.code, 'base_advanced')
        successor = other.get(result['session_id'])
        self.assertEqual(successor['instructions']['acknowledged_base'], ack)
        self.assertEqual(successor['repositories'][0]['base_sha'], 'c' * 40)
        self.assertEqual(successor['predecessor_id'], sid)

    def test_retry_api_409_shape_and_acknowledged_creation(self):
        sid = self.failed_current_session()
        api = ExecutionAPI(sessions=self.sessions)
        path = f'/api/sessions/{sid}/retry'
        body = {'idempotency_key': 'retry-api'}
        with patch.object(self.github.client, 'default_branch_sha', return_value='d' * 40):
            response = api.dispatch('POST', path, body=body)
        self.assertEqual(response.status, 409)
        details = [dict(repository_id=self.repo, pinned_revision='c' * 40, current_head='d' * 40, status='advanced')]
        self.assertEqual(response.body, {'code': 'base_advanced', 'details': details,
                                       'error': {'code': 'base_advanced', 'details': details}})
        response = api.dispatch('POST', path, body=dict(body, acknowledged_base={self.repo: 'c' * 40}))
        self.assertEqual(response.status, 202)
        self.assertEqual(api.dispatch('POST', path, body=dict(body, acknowledged_base={self.repo: 'c' * 40})).body, response.body)
