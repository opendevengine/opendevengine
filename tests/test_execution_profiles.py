"""Profile durability/policy tests with injected runtime and a real local broker.

No model or Docker inference runs. The test runner executes only the fixed
synthetic POSIX arithmetic fixture in a temporary directory, not repository code.
It is a fixture, not evidence of production container isolation or CLI login.
"""
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from opendevengine.execution.claude import AdapterError
from opendevengine.execution.adapter_process import run_stream
from opendevengine.execution.profiles import ExecutionProfileError, ExecutionProfileService
from opendevengine.execution.runner import RunnerError, RunnerHandle
from opendevengine.platform import Platform, Worker
from opendevengine.platform.errors import LeaseLost
from opendevengine.platform.worker import JobContext
from opendevengine.providers import ProviderService
from opendevengine.services import ShutdownContext

IMAGE = 'sha256:' + 'a' * 64
CONTAINER = 'b' * 64
PRIVATE = 'private synthetic output /Users/fixture/secret sk-fixture-not-a-real-key'
FIXED = b'#!/bin/sh\nprintf "%s\\n" "$(($1 + $2))"\n'


class FixtureRunner:
    def __init__(self, owner):
        self.owner = owner
        self.temp = tempfile.TemporaryDirectory(prefix='profile-fixture-')
        self.root = Path(self.temp.name)
        self.handles = []
        self.calls = []
        self.contained = True
        self.provision_error = False
        self.available = True
        self.fail_validation = False
        self.validation_timeout = False
        self.extra_snapshot = {}

    def preflight(self, image):
        self.calls.append(('preflight', image))
        if self.owner.unlocked_check:
            self.owner.assert_store_available()
        return {'available': self.available, 'image_id': IMAGE, 'blocking_reasons': []}

    def provision(self, ident, files, *, image, limits):
        self.calls.append(('provision', ident, image, dict(limits)))
        self.handle = RunnerHandle(self.owner.service.instance_id, ident, CONTAINER, IMAGE)
        self.handles.append(self.handle)
        for name, content in files.items():
            (self.root / name).write_bytes(content)
        if self.provision_error:
            raise RunnerError('provision_failed', PRIVATE)
        return self.handle

    def list_owned(self):
        return list(self.handles)

    def list_files(self, handle):
        return sorted(p.name for p in self.root.iterdir())

    def read_file(self, handle, name):
        return (self.root / name).read_bytes()

    def write_file(self, handle, name, content):
        self.calls.append(('write', name))
        (self.root / name).write_bytes(content)

    def delete_file(self, handle, name):
        (self.root / name).unlink()

    def snapshot(self, handle):
        return {**{p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}, **self.extra_snapshot}

    def run_command(self, handle, argv, *, timeout_seconds=30):
        self.calls.append(('command', list(argv), timeout_seconds))
        validation = argv[:2] == ['sh', '-c']
        if validation and self.validation_timeout:
            return {'exit_code': None, 'timed_out': True, 'truncated': False,
                    'stdout': PRIVATE, 'stderr': PRIVATE}
        result = subprocess.run(argv, cwd=self.root, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout_seconds, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        return {'exit_code': 1 if validation and self.fail_validation else result.returncode,
                'timed_out': False, 'truncated': False, 'stdout': PRIVATE, 'stderr': PRIVATE}

    def stop(self, handle):
        self.calls.append(('stop', handle.session_id))
        return {'confirmed': self.contained, 'code': 'stopped' if self.contained else 'containment_unverified'}

    def discard(self, handle):
        self.calls.append(('discard', handle.session_id))
        self.handles.remove(handle)


class FixtureAdapter:
    capabilities = {'headless': True, 'streaming': True, 'cancellation': True,
                    'follow_up': False, 'resume': False, 'checkpoint': False,
                    'ask_user': True, 'ask_user_requires_callback': True}

    def __init__(self, owner):
        self.owner = owner
        self.mode = 'success'
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self.preflight_error = None
        self.cost = None
        self.usage = None
        self.block_for = None
        self.on_run = None
        self.results = []

    def preflight(self):
        self.calls.append('preflight')
        if self.preflight_error:
            raise self.preflight_error
        return {'available': True, 'version': '2.1.311 (Claude Code)',
                'authentication_verified': False, 'capabilities': self.capabilities}

    @staticmethod
    def call(endpoint, name, arguments):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            sock.connect(endpoint['socket_path'])
            with sock.makefile('rwb') as stream:
                stream.write(json.dumps({'token': Path(endpoint['token_file']).read_text()}).encode() + b'\n')
                stream.flush()
                assert json.loads(stream.readline()) == {'ok': True}
                stream.write(json.dumps({'id': 1, 'op': 'call', 'name': name,
                                         'arguments': arguments}).encode() + b'\n')
                stream.flush()
                return json.loads(stream.readline())['result']

    def run(self, endpoint, prompt, **kwargs):
        self.calls.append(('run', prompt, kwargs))
        self.endpoint = dict(endpoint)
        self.kwargs = kwargs
        if self.mode in ('callback_process', 'unconfirmed_process'):
            def started(pid):
                self.process_id = pid
                kwargs['on_started'](pid)
            return run_stream([sys.executable, '-c', 'import time; time.sleep(.05)'],
                              cwd=str(self.owner.runner.root), env={}, on_line=lambda _: None,
                              timeout_seconds=2, max_output_bytes=1024,
                              stop_event=kwargs['stop_event'], on_started=started)
        kwargs['on_started'](os.getpid())  # Simulated harness identity, no model process.
        if self.mode != 'no_inventory':
            kwargs['on_inventory_verified']()
        kwargs['emit']('assistant_summary', {'text': PRIVATE})
        self.started.set()
        if self.on_run:
            self.on_run()
        if self.mode == 'block':
            if self.block_for is None:
                kwargs['stop_event'].wait(5)
            else:
                kwargs['stop_event'].wait(self.block_for)
            self.results.append(self.call(endpoint, 'read_file', {'path': 'calc.sh'}))
            status = 'cancelled' if kwargs['stop_event'].is_set() else 'completed'
        else:
            status = 'completed'
        if self.mode != 'block':
            self.results.append(self.call(endpoint, 'read_file', {'path': 'calc.sh'}))
            if self.mode == 'direct_mutation':
                (self.owner.runner.root / 'calc.sh').write_bytes(FIXED)
            elif self.mode != 'unchanged':
                content = FIXED if self.mode != 'wrong_change' else b'#!/bin/sh\nprintf "999\\n"\n'
                self.results.append(self.call(endpoint, 'write_file',
                                              {'path': 'calc.sh', 'content': content.decode()}))
                self.results.append(self.call(endpoint, 'run_command', {'argv': ['sh', 'calc.sh', '2', '3']}))
        kwargs['stop_event'].set()  # Real adapter closes tools on success as well.
        if self.mode == 'agent_failure':
            status = 'failed'
        return {'status': status, 'code': 'budget_exhausted' if self.mode == 'agent_failure' else
                'cancelled' if status == 'cancelled' else 'success',
                'exit_code': 0, 'stop_confirmed': self.mode != 'uncontained',
                'usage': self.usage, 'reported_cost_usd': self.cost,
                'summary': PRIVATE, 'capabilities': self.capabilities}


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.platform = Platform.open(Path(self.temp.name))
        self.providers = ProviderService(self.platform.store, None)
        self.providers.update_settings(local_only=False)
        self.unlocked_check = False
        self.factory_calls = []
        self.worker_thread = None
        self.service = ExecutionProfileService(self.platform, self.providers,
                                               runner_factory=self.runner_factory,
                                               adapter_factory=self.adapter_factory)
        self.runner = FixtureRunner(self)
        self.adapter = FixtureAdapter(self)

    def tearDown(self):
        self.adapter.release.set()
        if self.worker_thread:
            self.worker_thread.join(3)
            self.assertFalse(self.worker_thread.is_alive(), 'owned profile worker leaked')
        self.assertFalse([t for t in threading.enumerate() if t.name == 'execution-test-lease'])
        self.runner.temp.cleanup()
        self.platform.close()
        self.temp.cleanup()

    def runner_factory(self, instance_id):
        self.factory_calls.append(('runner', instance_id))
        return self.runner

    def adapter_factory(self):
        self.factory_calls.append(('adapter',))
        return self.adapter

    def spec(self, **changes):
        return {'name': 'Coding', 'adapter': 'claude_code', 'auth_mode': 'cli_login',
                'image': 'local-toolchain:cached', 'allow_external_source': True, **changes}

    def create(self, **changes):
        self.profile = self.service.create(self.spec(**changes))
        return self.profile

    def code(self, expected, call):
        with self.assertRaises(ExecutionProfileError) as caught:
            call()
        self.assertEqual(expected, caught.exception.code)
        self.assertEqual('Execution profile operation failed.', str(caught.exception))
        return caught.exception

    def request(self):
        self.requested = self.service.request_test(self.profile['id'])
        return self.requested

    def work(self, *, shutdown=None, lease_ms=30000):
        def handler(job, context):
            if shutdown is not None:
                context = ShutdownContext(context, shutdown)
            return self.service.run_test(job, context)
        return Worker(self.platform.jobs, {'execution.test': handler}, worker_id='fixture', lease_ms=lease_ms).run_once()

    def start_work(self, **kwargs):
        self.worker_thread = threading.Thread(target=lambda: self.work(**kwargs), name='fixture-profile-worker')
        self.worker_thread.start()
        self.assertTrue(self.adapter.started.wait(2), 'adapter fixture never started')

    def result(self):
        return self.service.get(self.profile['id'])['check']

    def private_result(self):
        with self.platform.store.read() as db:
            return json.loads(db.execute('SELECT result_json FROM execution_profile_checks WHERE version_id=?',
                                        (self.requested['version_id'],)).fetchone()[0])

    def assert_store_available(self):
        available = threading.Event()
        def read():
            with self.platform.store.read():
                available.set()
        thread = threading.Thread(target=read)
        thread.start()
        try:
            self.assertTrue(available.wait(1), 'runtime called while database lock held')
        finally:
            thread.join(2)

    def test_api_key_mode_requires_secret_ref(self):
        self.code('validation', lambda: self.create(auth_mode='api_key'))

    def test_cli_login_rejects_secret_ref(self):
        self.code('validation', lambda: self.create(secret_ref='ref'))

    def test_public_version_hides_secret_ref(self):
        with self.platform.store.transaction() as db:
            db.execute("INSERT INTO secret_refs VALUES('ref-x','memory','provider_api_key','coding',1,NULL)")
        profile = self.create(auth_mode='api_key', secret_ref='ref-x')
        self.assertIs(profile['version']['has_secret'], True)
        self.assertNotIn('ref-x', json.dumps(profile))
        self.assertNotIn('secret_ref', json.dumps(self.service.get(profile['id'])))
        self.assertEqual(self.service.get_version(profile['active_version_id'])['auth_mode'], 'api_key')

    def test_immutable_versions_soft_delete_history_and_restart(self):
        first = self.create()
        second = self.service.update(first['id'], {'name': 'Renamed', 'image': 'other:cached'})
        self.assertEqual(2, second['version']['version'])
        self.assertEqual('local-toolchain:cached', self.service.get_version(first['active_version_id'])['config']['image'])
        instance_id = self.service.instance_id
        self.platform.close()
        self.platform = Platform.open(Path(self.temp.name))
        self.providers = ProviderService(self.platform.store, None)
        self.service = ExecutionProfileService(self.platform, self.providers,
                                               runner_factory=self.runner_factory, adapter_factory=self.adapter_factory)
        self.assertEqual(instance_id, self.service.instance_id)
        self.assertEqual(second, self.service.get(first['id']))
        self.service.delete(first['id'])
        self.service.delete(first['id'])
        self.assertTrue(self.service.get_version(first['active_version_id'])['profile_deleted'])
        self.assertEqual([], self.service.list_profiles()['items'])
        for call in (lambda: self.service.update(first['id'], {'name': 'No'}),
                     lambda: self.service.request_test(first['id']), lambda: self.service.require_usable(first['id'])):
            self.code('execution_profile_deleted', call)
        self.assertEqual([], self.factory_calls)

    def test_pagination_name_conflict_and_bad_cursors(self):
        records = [self.service.create(self.spec(name='Profile %d' % n)) for n in range(5)]
        page = self.service.list_profiles(limit=2)
        seen = page['items']
        while page['next_cursor']:
            page = self.service.list_profiles(limit=2, cursor=page['next_cursor'])
            seen += page['items']
        self.assertEqual({r['id'] for r in records}, {r['id'] for r in seen})
        self.code('conflict', lambda: self.service.create(self.spec(name='PROFILE 0')))
        for limit in (0, 101, True, 1.0):
            self.code('validation', lambda: self.service.list_profiles(limit=limit))
        for cursor in (True, '!', 'e30=', 'WzEsMl0='):
            self.code('validation', lambda: self.service.list_profiles(cursor=cursor))

    def test_cursor_timestamp_rejects_overflow_and_accepts_int64_boundaries(self):
        self.create()
        def cursor(stamp):
            return base64.urlsafe_b64encode(json.dumps([stamp, 'cursor']).encode()).decode()
        for stamp in (2 ** 63, -(2 ** 63) - 1, 10 ** 100, -(10 ** 100), True, 1.5):
            self.code('validation', lambda: self.service.list_profiles(cursor=cursor(stamp)))
        self.assertEqual(1, len(self.service.list_profiles(cursor=cursor(-(2 ** 63)))['items']))
        self.assertEqual([], self.service.list_profiles(cursor=cursor(2 ** 63 - 1))['items'])

    def test_image_references_reject_userinfo_urls_and_malformed_digests(self):
        for image in ('user:password@registry.example/repo', 'https://registry.example/repo',
                      'docker://repo', 'registry.example//repo', 'repo@not-a-digest',
                      'repo@sha256:bad', 'sha256:bad', 'repo@sha256:' + 'a' * 64 + '@suffix',
                      'user:secret/repo', 'registry:65536/repo'):
            with self.subTest(image=image):
                self.code('validation', lambda: self.service.create(self.spec(image=image)))
        for n, image in enumerate(('local-toolchain:cached', 'registry.example:5000/team/image:tag',
                                   'localhost:5000/team/image', IMAGE, 'team/image@' + IMAGE)):
            self.assertEqual(image, self.service.create(self.spec(name='Image %d' % n, image=image))['version']['config']['image'])

    def test_agent_model_is_optional_validated_and_reaches_the_profile_test(self):
        default = self.create()
        self.assertIsNone(self.service.get_version(default['active_version_id'])['config']['agent_model'])
        chosen = self.create(name='Opus profile', agent_model=' claude-opus-5-5 ')
        self.assertEqual(self.service.get_version(chosen['active_version_id'])['config']['agent_model'], 'claude-opus-5-5')
        for bad in ('--model', 'a/b', 'two words', 7):
            with self.assertRaises(Exception):
                self.create(name='Bad model', agent_model=bad)

    def test_validation_rejects_credentials_paths_flags_and_unsupported_auth(self):
        for key in ('executable', 'environment', 'mounts', 'docker_args', 'secret_value', 'token', 'capabilities'):
            self.code('validation', lambda: self.service.create(self.spec(**{key: PRIVATE})))
        for field, value in (('auth_mode', 'oauth_token'), ('adapter', 'other')):
            self.code('unsupported', lambda: self.service.create(self.spec(**{field: value})))
        for change in ({'allow_external_source': 1}, {'image': '/host/image'}, {'name': '\x00'},
                       {'image': 'https://user:secret@host?key=private'}):
            self.code('validation', lambda: self.service.create(self.spec(**change)))
        self.assertEqual([], self.service.list_profiles()['items'])
        self.assertEqual([], self.factory_calls)

    def test_agent_and_runner_bounds_are_strict_finite_and_preserved(self):
        for key, values in {'wall_seconds': (True, 0, 3601), 'max_turns': (False, 0, 41),
                            'max_output_bytes': (1023, 1048577, 4096.0),
                            'max_budget_usd': (False, 0, 10.1, float('nan'), float('inf'))}.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.code('validation', lambda: self.service.create(self.spec(agent_limits={key: value})))
        for limits in ({'memory_mib': True}, {'max_output_bytes': 1023}, {'mount': PRIVATE},
                       {'workspace_mib': 1024}, {'wall_seconds': 9}):
            self.code('validation', lambda: self.service.create(self.spec(runner_limits=limits)))
        self.create(agent_limits={'wall_seconds': 5, 'max_turns': 3, 'max_output_bytes': 1024, 'max_budget_usd': .25})
        self.request(); self.work()
        options = self.adapter.kwargs
        self.assertLessEqual(options['timeout_seconds'], 5)
        self.assertEqual((3, 1024, .25), (options['max_turns'], options['max_output_bytes'], options['max_budget_usd']))
        provision = next(c for c in self.runner.calls if c[0] == 'provision')
        self.assertEqual(IMAGE, provision[2])
        self.assertEqual(65536, provision[3]['max_output_bytes'])

    def test_settings_persist_and_never_launch_or_kill_runtime(self):
        self.assertEqual({'max_active_sessions': 1}, self.service.settings())
        self.assertEqual({'max_active_sessions': 4}, self.service.update_settings(4))
        self.assertEqual({'max_active_sessions': 1}, self.service.update_settings(max_active_sessions=1))
        for value in (0, 5, True, 1.0, '2'):
            self.code('validation', lambda: self.service.update_settings(value))
        self.assertEqual([], self.factory_calls)

    def test_local_only_blocks_test_before_detection_and_source_requires_separate_consent(self):
        profile = self.create(allow_external_source=False)
        self.providers.update_settings(local_only=True)
        self.code('execution_local_only', self.request)
        self.assertEqual([], self.factory_calls)
        self.providers.update_settings(local_only=False)
        self.request(); self.work()
        self.assertEqual('passed', self.result()['status'])
        self.code('execution_external_source_denied', lambda: self.service.require_usable(profile['id']))
        self.assertTrue(self.adapter.kwargs['synthetic'])
        self.assertFalse(self.adapter.kwargs['external_source_consent'])

    def test_reads_and_require_usable_never_probe_or_claim_detection_is_proof(self):
        self.create()
        self.code('execution_test_required', lambda: self.service.require_usable(self.profile['id']))
        self.service.get(self.profile['id']); self.service.get_version(self.profile['active_version_id'])
        self.service.list_profiles(); self.service.settings()
        self.assertEqual([], self.factory_calls)
        self.request()
        self.code('execution_test_pending', lambda: self.service.require_usable(self.profile['id']))
        self.assertEqual([], self.factory_calls)

    def test_request_test_is_atomic_idempotent_across_threads_and_new_version(self):
        self.create()
        requests = []
        threads = [threading.Thread(target=lambda: requests.append(self.service.request_test(self.profile['id']))) for _ in range(6)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(1, len({r['job_id'] for r in requests}))
        self.assertEqual([], self.factory_calls)
        self.service.update(self.profile['id'], {'image': 'other:cached'})
        self.assertEqual(requests[0]['job_id'], self.service.request_test(self.profile['id'])['job_id'])
        job = self.platform.jobs.get(requests[0]['job_id'])
        self.assertEqual(('external', 1, 'execution.test'), (job.effect, job.max_attempts, job.kind))
        self.assertEqual({'version_id': self.profile['active_version_id']}, job.payload)

    def test_success_uses_real_broker_changed_fixture_and_independent_validation(self):
        self.create(); self.request(); self.work()
        self.assertEqual('passed', self.result()['status'])
        version = self.service.require_usable(self.profile['id'])
        self.assertEqual(self.profile['active_version_id'], version['id'])
        self.assertEqual(self.profile['id'], version['profile_id'])
        self.assertEqual(IMAGE, version['check']['image_id'])
        self.assertEqual('2.1.311 (Claude Code)', version['check']['cli_version'])
        self.assertTrue(self.private_result()['checks']['independent_validation'])
        self.assertTrue(self.private_result()['checks']['fixture_changed'])
        self.assertIsNone(self.private_result()['usage'])
        self.assertIsNone(self.private_result()['reported_cost_usd'])
        self.assertEqual([], self.runner.handles)
        self.assertFalse(Path(self.adapter.endpoint['socket_path']).exists())
        self.assertFalse(Path(self.adapter.endpoint['token_file']).exists())
        serialized = json.dumps(self.service.get(self.profile['id']))
        for private in (PRIVATE, CONTAINER, 'private_runtime', 'harness_pid', 'socket_path'):
            self.assertNotIn(private, serialized)
        self.assertNotIn(PRIVATE, json.dumps(self.private_result()))

    def test_profile_test_passes_vault_key_only_in_api_key_mode(self):
        from types import SimpleNamespace
        from opendevengine.platform.secrets import SecretValue
        with self.platform.store.transaction() as db:
            db.execute("INSERT INTO secret_refs VALUES('ref-k','memory','provider_api_key','coding',1,NULL)")
        self.providers.secrets = SimpleNamespace(resolve=lambda ref: SecretValue('sk-vault-' + ref))
        self.create(auth_mode='api_key', secret_ref='ref-k'); self.request(); self.work()
        self.assertEqual(self.adapter.kwargs['api_key'], 'sk-vault-ref-k')
        self.assertEqual(self.adapter.kwargs['auth_mode'], 'api_key')
        self.assertNotIn('sk-vault', json.dumps(self.result()) + json.dumps(self.private_result()))
        self.create(name='Login'); self.request(); self.work()
        self.assertIsNone(self.adapter.kwargs['api_key'])

    def test_profile_test_passes_the_chosen_model_and_legacy_versions_pass_none(self):
        self.create(agent_model='sonnet'); self.request(); self.work()
        self.assertEqual('passed', self.result()['status'])
        self.assertEqual('sonnet', self.adapter.kwargs['model'])
        # A version stored before agent_model existed has no key; the test must use the CLI default.
        self.create(name='Legacy profile')
        with self.platform.store.transaction() as db:
            row = db.execute('SELECT config_json FROM execution_profile_versions WHERE id=?',
                             (self.profile['active_version_id'],)).fetchone()
            config = json.loads(row[0]); config.pop('agent_model', None)
            db.execute('UPDATE execution_profile_versions SET config_json=? WHERE id=?',
                       (json.dumps(config), self.profile['active_version_id']))
        self.request(); self.work()
        self.assertIsNone(self.adapter.kwargs['model'])

    def test_claimed_success_is_not_proof_without_inventory_tools_change_and_validation(self):
        for mode in ('no_inventory', 'unchanged', 'wrong_change', 'direct_mutation'):
            with self.subTest(mode=mode):
                self.create(name=mode)
                self.adapter.mode = mode
                self.request(); self.work()
                self.assertEqual('failed', self.result()['status'])
                self.assertEqual({}, self.result()['capabilities'])
                self.code('execution_test_failed', lambda: self.service.require_usable(self.profile['id']))
        self.assertTrue(any(not result['ok'] for result in self.adapter.results))

    def test_latest_failed_and_pending_probe_cannot_reuse_previous_pass(self):
        self.create(); self.request(); self.work()
        self.assertEqual('passed', self.result()['status'])
        self.adapter.mode = 'wrong_change'
        self.request()
        self.assertEqual('queued', self.result()['status'])
        self.assertIsNone(self.result()['image_id'])
        self.code('execution_test_pending', lambda: self.service.require_usable(self.profile['id']))
        self.work()
        self.assertEqual('failed', self.result()['status'])
        self.code('execution_test_failed', lambda: self.service.require_usable(self.profile['id']))

    def test_name_only_rename_preserves_completed_check_without_runtime_work(self):
        self.create(); self.request(); self.work()
        previous = self.result()
        calls = len(self.factory_calls)
        renamed = self.service.update(self.profile['id'], {'name': 'Presentation rename'})
        self.assertEqual(previous, renamed['check'])
        self.assertEqual(renamed['active_version_id'], self.service.require_usable(self.profile['id'])['id'])
        self.assertEqual(calls, len(self.factory_calls))
        self.assertIsNone(self.platform.jobs.claim('rival', kinds={'execution.test'}))

    def test_name_only_rename_preserves_inflight_check_and_single_recovery_record(self):
        self.create(); self.request()
        self.adapter.on_run = lambda: self.adapter.release.wait(2)
        self.start_work()
        renamed = self.service.update(self.profile['id'], {'name': 'Renamed during check'})
        self.assertEqual('running', renamed['check']['status'])
        self.assertEqual(1, len(self.service.pending_runtime_tests()))
        self.adapter.release.set(); self.worker_thread.join(2)
        self.assertFalse(self.worker_thread.is_alive())
        self.assertEqual('passed', self.result()['status'])
        self.assertEqual(renamed['active_version_id'], self.service.require_usable(self.profile['id'])['id'])
        self.assertEqual([], self.service.pending_runtime_tests())

    def test_rename_cannot_inherit_over_config_changes_or_resurrect_old_pass(self):
        self.create(); self.request(); self.work()
        self.service.update(self.profile['id'], {'image': 'changed:cached'})
        self.service.update(self.profile['id'], {'image': 'local-toolchain:cached'})
        self.service.update(self.profile['id'], {'name': 'Back to original config'})
        self.assertIsNone(self.result())
        self.code('execution_test_required', lambda: self.service.require_usable(self.profile['id']))
        self.request(); self.adapter.mode = 'wrong_change'; self.work()
        self.service.update(self.profile['id'], {'name': 'Rename after failure'})
        self.assertEqual('failed', self.result()['status'])
        self.code('execution_test_failed', lambda: self.service.require_usable(self.profile['id']))

    def test_cached_success_survives_restart_but_policy_is_live(self):
        self.create(); self.request(); self.work()
        profile_id = self.profile['id']
        self.platform.close()
        self.platform = Platform.open(Path(self.temp.name))
        self.providers = ProviderService(self.platform.store, None)
        self.service = ExecutionProfileService(self.platform, self.providers,
                                               runner_factory=self.runner_factory, adapter_factory=self.adapter_factory)
        before = len(self.factory_calls)
        self.assertEqual('passed', self.service.require_usable(profile_id)['check']['status'])
        self.providers.update_settings(local_only=True)
        self.code('execution_local_only', lambda: self.service.require_usable(profile_id))
        self.assertEqual(before, len(self.factory_calls))

    def test_pinned_version_keeps_original_budget_and_image_after_harmless_profile_update(self):
        self.create(agent_limits={'max_budget_usd': .25, 'max_turns': 3})
        self.request(); self.work()
        version_id = self.requested['version_id']
        before = self.service.require_version_usable(version_id)
        self.service.update(self.profile['id'], {'name': 'New defaults', 'image': 'new:cached',
                                               'agent_limits': {'max_budget_usd': 5, 'max_turns': 25}})
        after = self.service.require_version_usable(version_id)
        self.assertEqual(before, after)
        self.assertEqual(.25, after['config']['agent_limits']['max_budget_usd'])
        self.assertEqual(3, after['config']['agent_limits']['max_turns'])
        self.assertEqual('local-toolchain:cached', after['config']['image'])
        self.assertEqual(IMAGE, after['check']['image_id'])
        self.code('execution_test_required', lambda: self.service.require_usable(self.profile['id']))

    def test_pinned_version_observes_live_consent_privacy_and_deletion(self):
        self.create(); self.request(); self.work()
        version_id = self.requested['version_id']
        self.service.update(self.profile['id'], {'allow_external_source': False})
        self.code('execution_external_source_denied', lambda: self.service.require_version_usable(version_id))
        self.service.update(self.profile['id'], {'allow_external_source': True})
        self.assertEqual(version_id, self.service.require_version_usable(version_id)['id'])
        self.providers.update_settings(local_only=True)
        self.code('execution_local_only', lambda: self.service.require_version_usable(version_id))
        self.providers.update_settings(local_only=False)
        self.service.delete(self.profile['id'])
        self.code('execution_profile_deleted', lambda: self.service.require_version_usable(version_id))

    def test_pinned_version_never_reuses_replaced_check_or_uncertain_job(self):
        self.create(); self.request(); self.work()
        version_id = self.requested['version_id']
        self.assertEqual('passed', self.service.require_version_usable(version_id)['check']['status'])
        # A later queue outcome invalidates its cached pass even if check metadata
        # survived a crash before the worker acknowledged the external operation.
        with self.platform.store.transaction() as db:
            db.execute("UPDATE jobs SET state='needs_reconciliation' WHERE id=?", (self.requested['job_id'],))
        self.code('execution_test_interrupted', lambda: self.service.require_version_usable(version_id))
        self.service.reconcile_test(version_id, self.requested['job_id'], containment_confirmed=True)
        self.request()
        self.code('execution_test_pending', lambda: self.service.require_version_usable(version_id))

    def test_agent_failure_code_timeout_and_usage_are_not_fabricated(self):
        self.create(); self.adapter.mode = 'agent_failure'
        self.request(); self.work()
        self.assertEqual('budget_exhausted', self.result()['error_code'])
        self.assertEqual('failed', self.result()['status'])
        self.assertIsNone(self.private_result()['reported_cost_usd'])
        self.adapter.mode = 'success'; self.runner.validation_timeout = True
        self.request(); self.work()
        self.assertEqual('execution_coding_unverified', self.result()['error_code'])
        self.assertFalse(self.private_result()['checks']['independent_validation'])

    def test_wall_budget_stops_waiting_adapter_without_trusting_its_timeout_argument(self):
        self.create(agent_limits={'wall_seconds': 1}); self.adapter.mode = 'block'
        self.request()
        started = time.monotonic()
        self.work()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual('failed', self.result()['status'])
        self.assertEqual('execution_test_timeout', self.result()['error_code'])
        self.assertEqual([], self.runner.handles)

    def test_reported_spend_over_budget_fails_and_unknown_usage_stays_unknown(self):
        self.create(agent_limits={'max_budget_usd': .25}); self.adapter.cost = .5
        self.adapter.usage = {'input_tokens': 3, 'output_tokens': None, 'private': PRIVATE}
        self.request(); self.work()
        self.assertEqual('budget_exhausted', self.result()['error_code'])
        self.assertEqual(.5, self.private_result()['reported_cost_usd'])
        self.assertEqual({'input_tokens': 3, 'output_tokens': None}, self.private_result()['usage'])

    def test_local_only_enabled_after_queueing_prevents_any_runtime_detection(self):
        self.create(); self.request(); self.providers.update_settings(local_only=True)
        self.work()
        self.assertEqual([], self.factory_calls)
        self.assertEqual('failed', self.result()['status'])
        self.assertEqual('execution_local_only', self.result()['error_code'])
        self.assertEqual('succeeded', self.platform.jobs.get(self.requested['job_id']).state)
        self.assertEqual([], self.service.pending_runtime_tests())
        self.providers.update_settings(local_only=False)
        self.request(); self.work()
        self.assertEqual('passed', self.result()['status'])

    def test_shutdown_before_runtime_is_safe_failure_and_permits_fresh_test(self):
        self.create(); self.request()
        shutdown = threading.Event(); shutdown.set()
        self.work(shutdown=shutdown)
        self.assertEqual([], self.factory_calls)
        self.assertEqual('failed', self.result()['status'])
        self.assertEqual('succeeded', self.platform.jobs.get(self.requested['job_id']).state)
        self.assertEqual([], self.service.pending_runtime_tests())
        self.request(); self.work()
        self.assertEqual('passed', self.result()['status'])

    def test_callback_error_after_real_process_cleanup_is_not_unknown_containment(self):
        self.create(); self.request(); self.adapter.mode = 'callback_process'
        with patch.object(self.service, '_process_start', side_effect=RuntimeError(PRIVATE)):
            self.work()
        self.assertEqual('failed', self.result()['status'])
        self.assertEqual('execution_test_failed', self.result()['error_code'])
        self.assertTrue(self.private_result()['checks']['containment_confirmed'])
        self.assertEqual('succeeded', self.platform.jobs.get(self.requested['job_id']).state)
        self.assertEqual([], self.service.pending_runtime_tests())
        with self.assertRaises(ProcessLookupError):
            os.kill(self.adapter.process_id, 0)

    def test_process_containment_error_remains_unknown_even_when_process_is_gone(self):
        from opendevengine.execution import adapter_process
        self.create(); self.request(); self.adapter.mode = 'unconfirmed_process'
        original = adapter_process._terminate_group
        def unverified(process, watcher):
            original(process, watcher)
            return False
        with patch.object(adapter_process, '_terminate_group', side_effect=unverified):
            self.work()
        self.assertEqual('interrupted', self.result()['status'])
        self.assertEqual('containment_unverified', self.result()['error_code'])
        self.assertEqual('needs_reconciliation', self.platform.jobs.get(self.requested['job_id']).state)
        self.code('execution_test_reconciliation_required', self.request)
        with self.assertRaises(ProcessLookupError):
            os.kill(self.adapter.process_id, 0)

    def test_detected_auth_failure_stores_only_safe_code(self):
        self.create(); self.adapter.preflight_error = AdapterError('authentication_required')
        self.request(); self.work()
        self.assertEqual('failed', self.result()['status'])
        self.assertEqual('authentication_required', self.result()['error_code'])
        self.assertFalse(any(c[0] == 'provision' for c in self.runner.calls))
        self.assertEqual([], self.service.pending_runtime_tests())

    def test_external_runtime_calls_are_outside_database_lock(self):
        self.unlocked_check = True
        self.create(); self.request(); self.work()
        self.assertEqual('passed', self.result()['status'])

    def test_private_runtime_is_durable_but_never_public_while_running(self):
        self.create(); self.adapter.mode = 'block'; self.request(); self.start_work()
        pending = self.service.pending_runtime_tests()
        self.assertEqual(1, len(pending))
        runtime = pending[0]['private_runtime']
        self.assertEqual(self.service.instance_id, runtime['instance_id'])
        self.assertEqual(CONTAINER, runtime['container_id'])
        self.assertEqual(os.getpid(), runtime['harness_pid'])
        self.assertTrue(runtime['harness_start'].startswith(('linux:', 'ps:')))
        for public in (self.service.get(self.profile['id']), self.service.get_version(self.requested['version_id']),
                       self.service.list_profiles()):
            serialized = json.dumps(public)
            for private in ('private_runtime', 'container_id', 'harness_pid', 'harness_start', CONTAINER, PRIVATE):
                self.assertNotIn(private, serialized)
        self.platform.jobs.cancel(self.requested['job_id'])
        self.worker_thread.join(2)
        self.assertEqual('interrupted', self.result()['status'])
        self.assertEqual('cancelled', self.platform.jobs.get(self.requested['job_id']).state)
        self.assertEqual([], self.service.pending_runtime_tests())

    def test_policy_change_or_delete_during_probe_stops_tools(self):
        for reason in ('policy', 'delete'):
            with self.subTest(reason=reason):
                self.create(name=reason); self.adapter.mode = 'block'
                self.adapter.started.clear(); self.request(); self.start_work()
                if reason == 'policy':
                    self.providers.update_settings(local_only=True)
                else:
                    self.service.delete(self.profile['id'])
                self.worker_thread.join(2)
                self.assertFalse(self.worker_thread.is_alive())
                self.assertFalse(self.adapter.results[-1]['ok'])
                self.assertEqual('failed', self.result()['status'])
                self.assertEqual([], self.runner.handles)
                self.providers.update_settings(local_only=False)

    def test_shutdown_interrupts_blocked_adapter_and_external_job_needs_reconciliation(self):
        shutdown = threading.Event()
        self.create(); self.adapter.mode = 'block'; self.request(); self.start_work(shutdown=shutdown)
        shutdown.set(); self.worker_thread.join(2)
        self.assertFalse(self.worker_thread.is_alive())
        self.assertEqual('interrupted', self.result()['status'])
        self.assertEqual('needs_reconciliation', self.platform.jobs.get(self.requested['job_id']).state)
        self.assertEqual([], self.runner.handles)
        self.service.reconcile_test(self.requested['version_id'], self.requested['job_id'], containment_confirmed=True)
        self.assertEqual('failed', self.platform.jobs.get(self.requested['job_id']).state)

    def test_live_lease_renewal_prevents_reclaim_during_long_probe(self):
        # The probe outlives the lease; renewal must keep ownership. Scaled for slow CI runners,
        # where a 1 s lease left too little slack for the renewal thread.
        self.create(); self.adapter.mode = 'block'; self.adapter.block_for = 2.8
        self.request(); self.start_work(lease_ms=2000)
        time.sleep(2.2)
        self.assertIsNone(self.platform.jobs.claim('rival', kinds={'execution.test'}, lease_ms=2000))
        self.worker_thread.join(5)
        self.assertFalse(self.worker_thread.is_alive())
        # No code was changed, but the lengthy operation kept its live ownership.
        self.assertEqual('succeeded', self.platform.jobs.get(self.requested['job_id']).state)
        self.assertEqual('failed', self.result()['status'])

    def test_lost_or_expired_lease_never_writes_late_success_or_renews_stale_lease(self):
        self.create(); self.request()
        lease = self.platform.jobs.claim('owner', kinds={'execution.test'}, lease_ms=1000)
        with self.platform.store.transaction() as db:
            db.execute('UPDATE jobs SET lease_expires_at=? WHERE id=?', (self.platform.jobs.clock() - 1, lease.job.id))
        with self.assertRaises(LeaseLost):
            self.service.run_test(lease.job, JobContext(self.platform.jobs, lease, 1000))
        self.assertEqual([], self.factory_calls)
        self.platform.jobs.reconcile_expired()
        self.assertEqual('interrupted', self.result()['status'])
        self.code('execution_test_reconciliation_required', self.request)

    def test_expiry_during_probe_rejects_late_result_and_retains_recovery_identity(self):
        self.create(); self.request()
        def expire():
            with self.platform.store.transaction() as db:
                db.execute('UPDATE jobs SET lease_expires_at=? WHERE id=?',
                           (self.platform.jobs.clock() - 1, self.requested['job_id']))
            self.platform.jobs.reconcile_expired()
        self.adapter.on_run = expire
        self.work()
        self.assertEqual('interrupted', self.result()['status'])
        self.assertEqual({}, self.result()['capabilities'])
        self.assertEqual('needs_reconciliation', self.platform.jobs.get(self.requested['job_id']).state)
        self.assertTrue(self.service.pending_runtime_tests()[0]['private_runtime'])
        self.assertTrue(all(not result['ok'] for result in self.adapter.results))
        self.service.reconcile_test(self.requested['version_id'], self.requested['job_id'], containment_confirmed=True)
        self.assertEqual([], self.service.pending_runtime_tests())

    def test_unknown_containment_blocks_retest_until_verified_reconciliation(self):
        self.create(); self.runner.contained = False; self.request(); self.work()
        self.assertEqual('interrupted', self.result()['status'])
        self.assertEqual('containment_unverified', self.result()['error_code'])
        self.assertEqual('needs_reconciliation', self.platform.jobs.get(self.requested['job_id']).state)
        self.assertTrue(self.service.pending_runtime_tests()[0]['private_runtime'])
        self.code('execution_test_reconciliation_required', self.request)
        self.service.reconcile_test(self.requested['version_id'], self.requested['job_id'], containment_confirmed=False)
        self.code('execution_test_reconciliation_required', self.request)
        self.runner.contained = True
        self.runner.stop(self.runner.handle); self.runner.discard(self.runner.handle)
        self.service.reconcile_test(self.requested['version_id'], self.requested['job_id'], containment_confirmed=True)
        self.assertEqual([], self.service.pending_runtime_tests())
        self.assertEqual('failed', self.platform.jobs.get(self.requested['job_id']).state)
        self.request(); self.work()
        self.assertEqual('passed', self.result()['status'])

    def test_recovery_does_not_steal_live_job_from_metadata(self):
        self.create(); self.adapter.mode = 'block'; self.request(); self.start_work()
        self.code('execution_test_active', lambda: self.service.reconcile_test(
            self.requested['version_id'], self.requested['job_id'], containment_confirmed=True))
        self.assertEqual('leased', self.platform.jobs.get(self.requested['job_id']).state)
        self.platform.jobs.cancel(self.requested['job_id']); self.worker_thread.join(2)

    def test_provision_failure_contains_returned_orphan_without_assuming_absence(self):
        self.create(); self.runner.provision_error = True; self.request(); self.work()
        self.assertEqual('failed', self.result()['status'])
        self.assertEqual('provision_failed', self.result()['error_code'])
        self.assertEqual([], self.runner.handles)
        self.assertEqual([], self.service.pending_runtime_tests())

    def test_unexpected_runtime_failure_and_outputs_are_redacted(self):
        self.create(); self.adapter.preflight_error = RuntimeError(PRIVATE)
        self.request(); self.work()
        self.assertEqual('execution_test_failed', self.result()['error_code'])
        self.assertNotIn(PRIVATE, json.dumps(self.private_result()))
        self.assertNotIn(PRIVATE, json.dumps(self.platform.jobs.get(self.requested['job_id']).result))


if __name__ == '__main__':
    unittest.main()
