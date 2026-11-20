"""Shared session fixtures: real SQLite/jobs/GitHub/Index/Plan services, synthetic transport.

Injected doubles are limited to the runner (in-memory container stand-in), the coding
agent (a scripted client of the REAL broker socket protocol) and the not-yet-integrated
ExecutionProfileService producer. None of these is inference or Docker proof.
"""
import base64
import copy
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import secrets
import signal
import subprocess
import tempfile
import threading
import time
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from opendevengine.ask import PlanService
from opendevengine.execution.mcp_server import BrokerClient, validate_endpoint
from opendevengine.execution.runner import RunnerError, RunnerHandle, validate_relative_path
from opendevengine.github import GitHubService, Response
from opendevengine.indexing import IndexService
from opendevengine.platform import Platform
from opendevengine.platform.secrets import BackendStatus, SecretService
from opendevengine.platform.worker import Worker
from opendevengine.providers import ProviderService
from opendevengine.sessions import SessionService, TERMINAL

IMAGE_ID = 'sha256:' + 'f' * 64
CLI_VERSION = '2.1.311 (Claude Code)'
_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = _KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                         serialization.NoEncryption()).decode()

BASE_FILES = {
    'calc.py': b'def add(a, b):\n    return a - b\n',
    'test_calc.py': b'from calc import add\nassert add(2, 3) == 5\n',
    'README.md': b'# Calc\nSynthetic fixture.\n',
    'lib/util.py': b'VALUE = 1\n',
    '.env': b'SECRET=do-not-ship\n',
    'config/token.txt': b'not a real token\n',
    'leak.py': b'KEY = "AKIA' + b'ABCDEFGHIJKLMNOP' + b'"\n',
    'node_modules/pkg/index.js': b'module.exports = 1\n',
    'run.sh': b'#!/bin/sh\necho run\n',
}
CONTEXT_FILES = {
    'shared/money.py': b'def round_half_even(x):\n    return round(x)\n',
    'secrets.txt': b'context secret\n',
}


class Vault:
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


def git_sha(data):
    return hashlib.sha1(data).hexdigest()


class Remote:
    """Synthetic GitHub REST transport (App, installation, catalog, trees, blobs).

    repo1 (gh:1) is the writable fixture; repo2 (gh:2) is a related read-only context.
    """

    def __init__(self, files=None):
        self.files = dict(BASE_FILES if files is None else files)
        self.symlinks = {'link.py': 'calc.py'}
        self.executable = {'run.sh'}
        self.commit = 'c' * 40
        self.head = 'c' * 40
        self.truncate = False
        self.calls, self.token_permissions = [], []
        self.lock = threading.Lock()
        self.repo2 = {'files': dict(CONTEXT_FILES), 'commit': 'd' * 40}

    def _repo(self, name):
        if name == 'repo1':
            return {'files': self.files, 'commit': self.commit, 'symlinks': self.symlinks, 'head': self.head}
        if name == 'repo2':
            return {'files': self.repo2['files'], 'commit': self.repo2['commit'], 'symlinks': {}, 'head': self.repo2['commit']}
        return None

    def tree(self, prefix, repo=None):
        repo = repo or self._repo('repo1')
        entries, dirs = [], set()
        for path, data in sorted(repo['files'].items()):
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            if '/' in rest:
                dirs.add(rest.split('/', 1)[0])
            else:
                mode = '100755' if path in self.executable else '100644'
                entries.append({'path': rest, 'type': 'blob', 'mode': mode, 'sha': git_sha(data), 'size': len(data)})
        for name in sorted(dirs):
            entries.append({'path': name, 'type': 'tree', 'mode': '040000', 'sha': git_sha(('tree:' + prefix + name).encode())})
        if prefix == '':
            for name, target in repo['symlinks'].items():
                entries.append({'path': name, 'type': 'blob', 'mode': '120000', 'sha': git_sha(target.encode()), 'size': len(target)})
        return entries

    def __call__(self, request):
        path = request.path
        with self.lock:
            self.calls.append((request.method, path))
        if path == '/app':
            return Response(200, {}, {'id': 1, 'slug': 'synthetic-app'})
        if path.startswith('/app/installations/'):
            if path.endswith('/access_tokens'):
                with self.lock:
                    self.token_permissions.append((request.json or {}).get('permissions'))
                expires = datetime.fromtimestamp(time.time() + 3000, timezone.utc).isoformat()
                return Response(201, {}, {'token': 'installation-secret-' + secrets.token_hex(4), 'expires_at': expires})
            return Response(200, {}, {'id': 11, 'app_id': 1, 'account': {'login': 'owner', 'type': 'Organization'},
                                     'permissions': {'contents': 'write', 'pull_requests': 'write', 'metadata': 'read'},
                                     'suspended_at': None})
        if path.startswith('/installation/repositories?'):
            page = int(path.rsplit('page=', 1)[1])
            repos = [{'id': 1, 'owner': {'login': 'owner'}, 'name': 'repo1', 'default_branch': 'main',
                      'description': 'synthetic calc', 'private': True, 'topics': []},
                     {'id': 2, 'owner': {'login': 'owner'}, 'name': 'repo2', 'default_branch': 'main',
                      'description': 'synthetic shared library', 'private': True, 'topics': []}] if page == 1 else []
            return Response(200, {}, {'repositories': repos})
        parts = path.split('/')
        if len(parts) > 4 and parts[1] == 'repos' and parts[2] == 'owner':
            repo = self._repo(parts[3])
            if repo is None:
                return Response(404, {}, {})
            if parts[4] == 'commits':
                return Response(200, {}, {'sha': repo['head']})
            sha = parts[-1]
            if parts[4:6] == ['git', 'trees']:
                if sha == repo['commit']:
                    return Response(200, {}, {'sha': sha, 'tree': self.tree('', repo),
                                             'truncated': self.truncate and parts[3] == 'repo1'})
                prefixes = {git_sha(('tree:' + p).encode()): p for p in self._dir_prefixes(repo['files'])}
                if sha in prefixes:
                    return Response(200, {}, {'sha': sha, 'tree': self.tree(prefixes[sha] + '/', repo), 'truncated': False})
                return Response(404, {}, {})
            if parts[4:6] == ['git', 'blobs']:
                for data in repo['files'].values():
                    if git_sha(data) == sha:
                        return Response(200, {}, {'encoding': 'base64', 'size': len(data),
                                                 'content': base64.b64encode(data).decode()})
                return Response(404, {}, {})
        raise AssertionError('unexpected request ' + path)

    @staticmethod
    def _dir_prefixes(files):
        out = set()
        for path in files:
            parts = path.split('/')
            for i in range(1, len(parts)):
                out.add('/'.join(parts[:i]))
        return out


class World:
    """Shared fake container state: survives runner re-creation like a Docker daemon."""

    def __init__(self):
        self.containers = {}
        self.lock = threading.Lock()
        self.provisioned = []
        self.stop_calls = []
        self.stop_fails = False
        self.commands = {}
        self.script = []
        self.runs = []
        self.results = []
        self.children = []
        self.adapter_status = None
        self.cli_version = CLI_VERSION
        self.container_commands = {}
        self.provision_error = None
        self.snapshot_fails = False


class FakeRunner:
    """In-memory stand-in for DockerRunner's accepted interface. NOT isolation proof."""

    def __init__(self, instance_id, world):
        self.instance_id, self.world = instance_id, world

    def preflight(self, image):
        return {'available': True, 'image_id': IMAGE_ID if image in (IMAGE_ID, 'python:3.12-slim') else None,
                'blocking_reasons': [] if image in (IMAGE_ID, 'python:3.12-slim') else ['image_not_present']}

    def _c(self, handle):
        with self.world.lock:
            container = self.world.containers.get(handle.container_id)
        if container is None or container['instance'] != self.instance_id:
            raise RunnerError('not_found')
        if not container['running']:
            raise RunnerError('not_running')
        return container

    def provision(self, session_id, files, *, image, limits, file_modes=None, context=None):
        if any(h.session_id == session_id for h in self.list_owned()):
            raise RunnerError('session_exists')
        for path in list(files) + list(context or {}):
            validate_relative_path(path)
        if self.world.provision_error:
            raise RunnerError(self.world.provision_error)
        cid = secrets.token_hex(32)
        modes = {p: m for p, m in (file_modes or {}).items()}
        with self.world.lock:
            self.world.containers[cid] = {'instance': self.instance_id, 'session': session_id, 'files': dict(files),
                                          'modes': modes, 'context': dict(context or {}), 'context_given': context is not None,
                                          'running': True, 'image': image, 'limits': dict(limits)}
            self.world.provisioned.append(session_id)
        return RunnerHandle(self.instance_id, session_id, cid, image)

    def list_files(self, handle):
        return sorted(self._c(handle)['files'])

    def read_file(self, handle, path):
        files = self._c(handle)['files']
        path = validate_relative_path(path)
        if path not in files:
            raise RunnerError('not_found')
        return files[path]

    def write_file(self, handle, path, content):
        container = self._c(handle)
        path = validate_relative_path(path)
        container['files'][path] = bytes(content)
        container['modes'].setdefault(path, '100644')

    def delete_file(self, handle, path):
        files = self._c(handle)['files']
        if files.pop(validate_relative_path(path), None) is None:
            raise RunnerError('not_found')

    def run_command(self, handle, argv, *, timeout_seconds=30):
        container = self._c(handle)
        key = argv[2] if argv[:2] == ['sh', '-c'] else ' '.join(argv)
        handler = self.world.commands.get(key)
        if argv[:1] == ['grep']:
            return {'exit_code': 1, 'stdout': '', 'stderr': '', 'timed_out': False, 'truncated': False}
        if argv[:1] == ['cat'] and len(argv) == 2 and argv[1].startswith('/context/'):
            data = container['context'].get(argv[1][len('/context/'):])
            if data is None:
                return {'exit_code': 1, 'stdout': '', 'stderr': 'No such file', 'timed_out': False, 'truncated': False}
            return {'exit_code': 0, 'stdout': data.decode(), 'stderr': '', 'timed_out': False, 'truncated': False}
        rich = self.world.container_commands.get(key)
        if rich is not None:
            return rich(container)
        if handler is None:
            return {'exit_code': 127, 'stdout': '', 'stderr': 'sh: command not found', 'timed_out': False, 'truncated': False}
        return handler(container['files'])

    def snapshot(self, handle, *, include_modes=False):
        if self.world.snapshot_fails:
            raise RunnerError('snapshot_failed')
        container = self._c(handle)
        files = dict(container['files'])
        if include_modes:
            return files, {p: container['modes'].get(p, '100644') for p in files}
        return files

    def stop(self, handle):
        self.world.stop_calls.append(handle.session_id)
        if self.world.stop_fails:
            return {'confirmed': False, 'code': 'containment_unverified'}
        with self.world.lock:
            container = self.world.containers.get(handle.container_id)
            if container is None:
                return {'confirmed': True, 'code': 'absent'}
            container['running'] = False
        return {'confirmed': True, 'code': 'stopped'}

    def discard(self, handle):
        with self.world.lock:
            container = self.world.containers.get(handle.container_id)
            if container and container['running']:
                raise RunnerError('not_stopped')
            self.world.containers.pop(handle.container_id, None)

    def list_owned(self):
        with self.world.lock:
            return [RunnerHandle(self.instance_id, c['session'], cid, c['image'])
                    for cid, c in self.world.containers.items() if c['instance'] == self.instance_id]


class ScriptedAgent:
    """Scripted MCP-tool client of the real broker endpoint plus a real harness process.

    It exercises the adapter calling convention and broker protocol, never inference.
    """
    capabilities = {'headless': True, 'streaming': True, 'cancellation': True, 'resume': False,
                    'checkpoint': False, 'follow_up': False, 'ask_user': True, 'ask_user_requires_callback': True}

    def __init__(self, world):
        self.world = world

    def preflight(self):
        return {'available': True, 'version': self.world.cli_version}

    def run(self, endpoint, prompt, *, auth_mode, on_inventory_verified, emit, synthetic=False, local_only=True,
            external_source_consent=False, stop_event=None, on_started=None, timeout_seconds=180, max_turns=12,
            max_budget_usd=1.0, max_output_bytes=524288, model=None):
        world = self.world
        world.runs.append({'prompt': prompt, 'auth_mode': auth_mode, 'synthetic': synthetic, 'local_only': local_only,
                           'consent': external_source_consent, 'timeout_seconds': timeout_seconds,
                           'max_turns': max_turns, 'max_budget_usd': max_budget_usd, 'stop_event': stop_event,
                           'model': model})
        if stop_event is None or stop_event.is_set():
            raise AssertionError('a fresh unset stop Event is required')
        child = subprocess.Popen(['/bin/sleep', '300'], start_new_session=True)
        world.children.append(child)
        status, code = 'completed', 'success'
        client = None
        try:
            on_started(child.pid)
            # Before inventory verification the broker guard must deny every tool.
            token = validate_endpoint(endpoint['socket_path'], endpoint['token_file'])
            client = BrokerClient(endpoint['socket_path'], token, timeout=60)
            world.results.append(('pre_gate', client.request({'op': 'call', 'name': 'list_files', 'arguments': {}})['result']))
            emit('adapter_inventory', {'tools': ['mcp__opendevengine__list_files'], 'servers': [{'name': 'opendevengine', 'status': 'connected'}],
                                       'plugins_present': False, 'commands_present': False})
            on_inventory_verified()
            emit('adapter_ready', {'tools': [], 'model': 'scripted-fixture'})
            for step in list(world.script):
                if stop_event.is_set():
                    status, code = 'cancelled', 'cancelled'
                    break
                if step == 'hang':
                    if stop_event.wait(timeout_seconds):
                        status, code = 'cancelled', 'cancelled'
                    else:
                        status, code = 'failed', 'timeout'
                    break
                if isinstance(step, tuple) and step[0] == 'fail':
                    status, code = 'failed', step[1]
                    break
                if isinstance(step, tuple) and step[0] == 'say':
                    emit('assistant_summary', {'text': step[1]})
                    continue
                if step == 'ack':
                    import re as _re
                    seen = sorted({m for _, r in world.results if isinstance(r, dict)
                                   for m in _re.findall(r'\[message:smsg_[0-9a-f]+:ack\]', r.get('text', ''))})
                    emit('assistant_summary', {'text': 'Noted the update. ' + ' '.join(seen)})
                    continue
                if callable(step):
                    step()
                    continue
                name, args = step
                world.results.append((name, client.request({'op': 'call', 'name': name, 'arguments': args})['result']))
            if status == 'completed' and stop_event.is_set():
                status, code = 'cancelled', 'cancelled'
            emit('adapter_result', {'status': status, 'code': code, 'summary': 'scripted run finished',
                                    'usage': {'input_tokens': 10}, 'reported_cost_usd': 0.01})
        finally:
            stop_event.set()
            if client is not None:
                client.close()
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        return {'status': status, 'code': code, 'exit_code': 0 if status == 'completed' else 143,
                'stop_confirmed': True, 'usage': {'input_tokens': 10, 'output_tokens': 5}, 'reported_cost_usd': 0.01,
                'capabilities': dict(self.capabilities)}


class ProfileError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class FakeProfiles:
    """Matches root-clarified ExecutionProfileService producer shapes (flat version dict)."""

    def __init__(self, store=None):
        self.error = None
        self.tests = []
        self.store = store
        self.active = 'epv_1'
        self.versions = {}
        self.versions['epv_1'] = {
            'id': 'epv_1', 'profile_id': 'prof_1', 'version': 1, 'adapter': 'claude_code', 'auth_mode': 'cli_login',
            'config': {'image': 'python:3.12-slim', 'allow_external_source': True,
                       'runner_limits': {'wall_seconds': 120},
                       'agent_limits': {'wall_seconds': 60, 'max_turns': 5, 'max_budget_usd': 1.0, 'max_output_bytes': 65536}},
            'created_at': 1, 'profile_deleted': False,
            'check': {'status': 'passed', 'image_id': IMAGE_ID, 'cli_version': CLI_VERSION,
                      'capabilities': dict(ScriptedAgent.capabilities), 'checked_at': 1, 'error_code': None}}

    @property
    def version(self):
        return self.versions[self.active]

    def add_version(self, **config):
        """Producer-style update: a new immutable active version (rows persisted)."""
        number = len(self.versions) + 1
        ident = 'epv_%d' % number
        new = copy.deepcopy(self.version)
        new.update(id=ident, version=number)
        for key, value in config.items():
            new['config'][key] = value
        self.versions[ident] = new
        self.active = ident
        self.persist()
        return new

    def persist(self):
        """Write the frozen schema-9 rows the real producer owns (FK targets for sessions)."""
        import json as _json
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO execution_profiles VALUES ('prof_1','synthetic',?,NULL,1,1)", (self.active,))
            db.execute('UPDATE execution_profiles SET active_version_id=? WHERE id=?', (self.active, 'prof_1'))
            for ident, version in self.versions.items():
                db.execute("INSERT OR IGNORE INTO execution_profile_versions VALUES (?,'prof_1',?,'claude_code','cli_login',?,1)",
                           (ident, version['version'], _json.dumps(version['config'])))
                db.execute("INSERT OR IGNORE INTO execution_profile_checks VALUES (?,NULL,'passed',?,NULL,1)",
                           (ident, _json.dumps(version['check'])))

    def require_usable(self, profile_id):
        if profile_id != 'prof_1':
            raise ProfileError('not_found')
        return self.require_version_usable(self.active)

    def require_version_usable(self, version_id):
        """Pinned config/budget/image; CURRENT consent/deletion/test status gate use."""
        if self.error:
            raise ProfileError(self.error)
        if version_id not in self.versions:
            raise ProfileError('not_found')
        if self.versions[self.active]['config'].get('allow_external_source') is not True:
            raise ProfileError('execution_external_source_denied')
        return copy.deepcopy(self.versions[version_id])

    def get_version(self, version_id):
        if version_id not in self.versions:
            raise ProfileError('not_found')
        return copy.deepcopy(self.versions[version_id])

    def run_test(self, job, context):
        self.tests.append(job.id)
        return {'state': 'passed'}


class SessionCase(unittest.TestCase):
    lease_ms = 3000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / 'data'
        self.platform = Platform.open(self.data)
        self.addCleanup(self.platform.close)
        self.store, self.jobs = self.platform.store, self.platform.jobs
        self.remote = Remote()
        self.github = GitHubService(self.store, self.jobs, SecretService(self.store, Vault()), transport=self.remote)
        connection = self.github.configure(1, 'synthetic-app', PEM, index_new=True)
        from urllib.parse import parse_qs, urlsplit
        state = parse_qs(urlsplit(self.github.begin_connect(connection['id'])['install_url']).query)['state'][0]
        self.github.finish_connect(state, '11')
        self.github.refresh(connection['id'])
        self.github_worker = Worker(self.jobs, {'github.refresh': self.github.run_refresh,
                                                'github.purge': self.github.run_purge}, worker_id='github')
        self.github_worker.run_once()
        self.repo = 'gh:1'
        self.github.update_selection(self.repo, index_enabled=True, implementation_enabled=True)
        self.providers = ProviderService(self.store, None)
        self.providers.update_settings(local_only=False)
        self.index = IndexService(self.store, self.jobs, self.github, None, data_dir=self.data)
        self.addCleanup(self.index.vectors.close)
        run = self.index.request_index(self.repo)
        Worker(self.jobs, {'index.run': self.index.run_index}, worker_id='index').run_once()
        self.assertEqual(self.index.get_run(run['id'])['state'], 'completed')
        self.world = World()
        self.profiles = FakeProfiles(self.store)
        self.profiles.persist()
        holder = {}
        self.plans = PlanService(self.store, self.index, None, execution_preflight=lambda c: holder['s'].preflight(c))
        self.sessions = self.make_service()
        holder['s'] = self.sessions
        self.addCleanup(self.cleanup_children)

    def make_service(self, **kwargs):
        service = SessionService(self.platform, self.index, self.github, self.providers, self.plans, self.profiles,
                                 runner_factory=lambda iid: FakeRunner(iid, self.world),
                                 adapter_factory=lambda: ScriptedAgent(self.world),
                                 lease_ms=kwargs.pop('lease_ms', self.lease_ms), poll_interval=0.05, **kwargs)
        self.addCleanup(service.close)
        return service

    def cleanup_children(self):
        for child in self.world.children:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()

    def content(self, **changes):
        snap = self.index.get_state(self.repo)['active_snapshot']
        content = {'goal': 'Fix add', 'user_request': 'add() subtracts; make it add',
                   'repositories': [{'repository_id': self.repo, 'role': 'write', 'snapshot_id': snap['id'],
                                     'revision': snap['revision'], 'branch': 'main'}],
                   'citations': [{'id': 'e1', 'repository_id': self.repo, 'snapshot_id': snap['id'],
                                  'revision': snap['revision'], 'path': 'calc.py', 'start_line': 1, 'end_line': 2}],
                   'findings': [{'text': 'add subtracts', 'citation_ids': ['e1']}],
                   'requirements': ['add returns a + b'],
                   'steps': [{'title': 'Fix', 'description': 'Use +', 'paths': ['calc.py'], 'citation_ids': ['e1']}],
                   'validation': [{'command': 'python test_calc.py', 'expected': 'passes', 'source': 'proposed', 'citation_ids': []}],
                   'execution_context': {'execution_profile_id': 'prof_1'}}
        content.update(changes)
        return content

    def ready_plan(self, **changes):
        plan = self.plans.create(None, self.content(**changes))
        version = plan['version']
        self.assertEqual(version['readiness']['state'], 'ready', version['readiness'])
        return plan, version

    def wait(self, predicate, timeout=15.0, message='condition'):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        self.fail('timed out waiting for ' + message)

    def wait_state(self, sid, states=TERMINAL, timeout=15.0):
        return self.wait(lambda: self.sessions.get(sid) if self.sessions.get(sid)['state'] in states else None,
                         timeout, 'session state in %s (now %s)' % (states, self.sessions.get(sid)['state']))

    def all_events(self, sid, service=None):
        service = service or self.sessions
        items, after = [], 0
        while True:
            page = service.events(sid, after=after, limit=7)
            items += page['items']
            after = page['next_seq']
            if not page['has_more']:
                return items
