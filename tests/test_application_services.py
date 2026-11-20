"""Verify real service composition, durable background execution and stop ownership."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
from unittest import TestCase
from opendevengine.platform import Platform
from opendevengine.services import ApplicationServices


class ApplicationServiceTests(TestCase):
    def test_execution_composition_wires_api_and_lifecycle_once(self):
        class Profiles:
            def list_profiles(self,limit=50,cursor=None):return {'items': [], 'next_cursor': None}
        class Sessions:
            def __init__(self):self.started=self.closed=0
            def runtime_status(self):return {'status':'running' if self.started and not self.closed else 'stopped'}
            def start(self):self.started+=1;return self.runtime_status()
            def close(self):self.closed+=1;return self.runtime_status()
            def list_sessions(self,limit=50,cursor=None):return {'items': [], 'next_cursor': None}
        class Context:
            vectors=type('Vectors',(),{'close':lambda self:None})()
            def run_index(self,job,context):return {}
        class Conversations:
            def run_answer(self,job,context):return {}
            def purge_repository(self,ident):return None
        platform=Platform.open(None);self.addCleanup(platform.close)
        services=ApplicationServices(platform);services.enable_context(Context())
        services.enable_ask(object(),object(),Conversations())
        profiles,sessions=Profiles(),Sessions()
        status=services.enable_execution(profiles,sessions)
        self.assertEqual(status['status'],'available')
        self.assertEqual(services.api.dispatch('GET','/api/sessions').body['items'],[])
        with self.assertRaises(RuntimeError):services.enable_execution(profiles,sessions)
        services.start()
        try:
            deadline=time.monotonic()+5
            while services.recovering and time.monotonic()<deadline:time.sleep(0.01)
            self.assertEqual((sessions.started,sessions.closed),(1,0))
        finally:services.close()
        self.assertEqual((sessions.started,sessions.closed),(1,1))

    def test_slow_session_recovery_does_not_delay_serving(self):
        release=Event()
        class Sessions:
            def __init__(self):self.started=self.closed=0
            def runtime_status(self):return {'status':'running' if self.started and not self.closed else 'stopped'}
            def start(self):release.wait(5);self.started+=1;return self.runtime_status()
            def close(self):self.closed+=1;return self.runtime_status()
            def list_sessions(self,limit=50,cursor=None):return {'items': [], 'next_cursor': None}
        class Context:
            vectors=type('Vectors',(),{'close':lambda self:None})()
            def run_index(self,job,context):return {}
        class Conversations:
            def run_answer(self,job,context):return {}
            def purge_repository(self,ident):return None
        platform=Platform.open(None);self.addCleanup(platform.close)
        services=ApplicationServices(platform);services.enable_context(Context())
        services.enable_ask(object(),object(),Conversations())
        sessions=Sessions();services.enable_execution(type('Profiles',(),{})(),sessions)
        started=time.monotonic();services.start()
        try:
            self.assertLess(time.monotonic()-started,1,'start() must not wait for session recovery')
            self.assertEqual(services.execution_status()['runtime'],{'status':'starting'})
            release.set()
            deadline=time.monotonic()+5
            while services.recovering and time.monotonic()<deadline:time.sleep(0.01)
            self.assertEqual(services.execution_status()['runtime'],{'status':'running'})
        finally:
            release.set();services.close()
        self.assertEqual((sessions.started,sessions.closed),(1,1))

    def test_execution_requires_context_ask_and_paired_injection(self):
        platform=Platform.open(None);self.addCleanup(platform.close)
        services=ApplicationServices(platform)
        with self.assertRaises(RuntimeError):services.enable_execution(object(),object())

    def test_profile_http_adapter_persists_versions_and_roles_across_restart(self):
        with TemporaryDirectory() as directory:
            platform = Platform.open(Path(directory))
            services = ApplicationServices(platform)
            profile = services.api.dispatch('POST', '/api/provider-profiles', body={
                'name': 'Local chat', 'adapter': 'ollama', 'endpoint': 'http://127.0.0.1:11434',
                'model': 'synthetic', 'config': {'capabilities': ['chat']}}).body
            roles = services.api.dispatch('PATCH', '/api/settings/model-roles', body={
                'assignments': {'ask': profile['id']}}).body
            self.assertEqual(roles['roles']['ask']['status'], 'untested')
            platform.close()
            platform = Platform.open(Path(directory))
            try:
                services = ApplicationServices(platform)
                saved = services.api.dispatch('GET', '/api/provider-profiles').body['profiles']
                self.assertEqual(saved[0]['active_version_id'], profile['active_version_id'])
                self.assertEqual(services.providers.roles()['ask']['profile_id'], profile['id'])
                self.assertEqual(services.github.connections(), [])
            finally:
                platform.close()

    def test_background_worker_executes_durable_handler_and_joins_before_store_close(self):
        platform = Platform.open(None)
        self.addCleanup(platform.close)
        services = ApplicationServices(platform)
        seen = Event()
        services.github.register_purge_hook('test-context', lambda repository_id: seen.set())
        with platform.store.transaction() as conn:
            conn.execute("INSERT INTO repositories(id,github_id,owner,name,full_name,default_branch,visibility,clone_url,created_at,updated_at) VALUES('gh:123',123,'test','repo','test/repo','main','private','https://github.com/test/repo.git',1,1)")
        job = platform.jobs.enqueue('github.purge', {'repository_id': 'gh:123'})
        services.start()
        try:
            self.assertTrue(seen.wait(3), 'background handler did not execute')
            deadline = time.monotonic() + 3
            while platform.jobs.get(job.id).state != 'succeeded' and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(platform.jobs.get(job.id).state, 'succeeded')
        finally:
            services.close()
        self.assertFalse(services.running)
        self.assertFalse(services.thread.is_alive())
        self.assertEqual(services.error_code, None)

    def test_github_validation_maps_to_safe_http_status(self):
        from opendevengine.api import error_status
        self.assertEqual(error_status('validation'), 400)
        self.assertEqual(error_status('auth'), 409)
        self.assertEqual(error_status('timeout'), 504)
        self.assertEqual(error_status('malformed_output'), 502)

    def test_shutdown_checkpoints_and_releases_safe_work_without_attempt_penalty(self):
        platform = Platform.open(None)
        self.addCleanup(platform.close)
        services = ApplicationServices(platform)
        entered, proceed = Event(), Event()
        def handler(job, context):
            entered.set()
            self.assertTrue(proceed.wait(3))
            context.checkpoint({'cursor': 42})
            raise AssertionError('must yield before continuing work')
        services.handlers = {'test.resume': handler}
        job = platform.jobs.enqueue('test.resume', {})
        services.start()
        self.assertTrue(entered.wait(3))
        services.stop_event.set()
        proceed.set()
        services.close()
        saved = platform.jobs.get(job.id)
        self.assertEqual(saved.state, 'queued')
        self.assertEqual(saved.attempts, 0)
        self.assertEqual(saved.checkpoint, {'cursor': 42})
        self.assertIsNone(saved.lease_owner)
        resumed = platform.jobs.claim('next-process')
        self.assertEqual(resumed.job.checkpoint, {'cursor': 42})

    def test_shutdown_never_requeues_an_uncertain_external_effect(self):
        from opendevengine.platform.worker import Worker, JobInterrupted
        platform = Platform.open(None)
        self.addCleanup(platform.close)
        job = platform.jobs.enqueue('test.external', {}, effect='external')
        def interrupted(job, context):
            raise JobInterrupted()
        Worker(platform.jobs, {'test.external': interrupted}, worker_id='owner').run_once()
        self.assertEqual(platform.jobs.get(job.id).state, 'needs_reconciliation')

    def test_local_purge_hides_source_and_redacts_answers_before_physical_cleanup(self):
        from types import SimpleNamespace
        events=[]
        class Context:
            vectors=SimpleNamespace(close=lambda:events.append('close-vectors'))
            def get_state(self,ident):return {'kind':'local','repository_id':ident}
            def set_local_selection(self,ident,enabled):events.append(('retrieval',enabled))
            def purge_repository(self,ident):events.append('physical-purge');return {'state':'completed'}
            def run_index(self,job,context):return {}
        class Conversations:
            def purge_repository(self,ident):events.append('redact-answers')
            def run_answer(self,job,context):return {}
        platform=Platform.open(None);self.addCleanup(platform.close)
        services=ApplicationServices(platform)
        services.enable_context(Context())
        services.enable_ask(object(),object(),Conversations())
        response=services.api.dispatch('POST','/api/repositories/local:fixture/purge',body={})
        self.assertEqual(response.body,{'state':'completed'})
        self.assertEqual(events,[('retrieval',False),'redact-answers','physical-purge'])
        services.start()
        try:
            with self.assertRaises(RuntimeError):services.enable_context(Context())
        finally:services.close()
        self.assertEqual(events[-1],'close-vectors')

    def test_local_purge_failure_keeps_retrieval_disabled(self):
        from types import SimpleNamespace
        class Context:
            enabled=True;vectors=SimpleNamespace(close=lambda:None)
            def get_state(self,ident):return {'kind':'local','repository_id':ident}
            def set_local_selection(self,ident,enabled):self.enabled=enabled
            def purge_repository(self,ident):raise RuntimeError('synthetic cleanup failure')
            def run_index(self,job,context):return {}
        platform=Platform.open(None);self.addCleanup(platform.close)
        services=ApplicationServices(platform);index=Context();services.enable_context(index)
        with self.assertRaises(RuntimeError):services.purge_context('local:fixture')
        self.assertFalse(index.enabled)
        services.close()
