"""ToolBroker policy, private endpoint and real MCP stdio protocol checks.

The broker runs against an in-memory runner double; the MCP bridge runs as a real
``python -m opendevengine.execution.mcp_server`` subprocess over a real Unix socket.
No model CLI or inference is involved. Real-Docker broker use is covered by
OPENDEVENGINE_DOCKER_TESTS=1 in ``BrokerOverRealDockerTest``.
"""
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import unittest

from opendevengine.execution import TOOL_NAMES, ToolBroker
from opendevengine.execution.runner import RunnerError, validate_relative_path

REPO = Path(__file__).resolve().parents[1]
SECRET = 'TOP-SECRET-SOURCE-LINE'


class MemoryRunner:
    """Test double with the runner tool surface; records every call."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.calls = []

    def _rec(self, name, *args):
        self.calls.append(name)

    def list_files(self, handle):
        self._rec('list_files')
        return sorted(self.files)

    def read_file(self, handle, path):
        self._rec('read_file', path)
        path = validate_relative_path(path)
        if path not in self.files:
            raise RunnerError('not_found')
        return self.files[path]

    def write_file(self, handle, path, content):
        self._rec('write_file', path)
        self.files[validate_relative_path(path)] = content

    def delete_file(self, handle, path):
        self._rec('delete_file', path)
        if self.files.pop(validate_relative_path(path), None) is None:
            raise RunnerError('not_found')

    def run_command(self, handle, argv, *, timeout_seconds=30):
        self._rec('run_command', argv)
        if argv[0] == 'grep':
            query = argv[argv.index('-e') + 1]
            out = []
            for path, data in sorted(self.files.items()):
                for i, line in enumerate(data.decode().splitlines(), 1):
                    if query in line:
                        out.append(f'./{path}\0{i}:{line}')
            return {'exit_code': 0 if out else 1, 'stdout': '\n'.join(out) + '\n', 'stderr': '',
                    'timed_out': False, 'truncated': False}
        return {'exit_code': 0, 'stdout': f'ran {argv[0]}: {SECRET}', 'stderr': '', 'timed_out': False,
                'truncated': False}

    def snapshot(self, handle):
        self._rec('snapshot')
        return dict(self.files)


class Recorder:
    def __init__(self):
        self.events = []
        self.checkpoints = []

    def emit(self, kind, payload):
        json.dumps(payload)  # must be serializable
        self.events.append((kind, payload))

    def checkpoint(self, snap):
        self.checkpoints.append(dict(snap))

    def kinds(self):
        return [k for k, _ in self.events]


class GuardSwitch:
    def __init__(self):
        self.code = None
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.code


def make(files=None, **kw):
    runner = MemoryRunner(files if files is not None else {'src/app.py': f'x = 1\n# {SECRET}\n'.encode()})
    rec, guard = Recorder(), kw.pop('guard', GuardSwitch())
    broker = ToolBroker(runner, object(), guard=guard, emit=rec.emit, checkpoint=rec.checkpoint, **kw)
    return broker, runner, rec, guard


class BrokerPolicyTest(unittest.TestCase):
    def test_exact_tool_inventory_with_closed_schemas(self):
        broker, *_ = make()
        tools = broker.tools()
        self.assertEqual([t['name'] for t in tools], list(TOOL_NAMES))
        for tool in tools:
            self.assertIs(tool['inputSchema']['additionalProperties'], False)
            props = json.dumps(tool['inputSchema'])
            for forbidden in ['container', 'docker', 'host', 'image']:
                self.assertNotIn(forbidden, props.lower())

    def test_denial_never_starts_tool_or_touches_runner(self):
        for code, expected in [('stop_requested', 'stop_requested'), ('lease_lost', 'lease_lost'),
                               ('budget_exhausted', 'budget_exhausted'), ('weird', 'policy_denied')]:
            broker, runner, rec, guard = make()
            guard.code = code
            for name, args in [('run_command', {'argv': ['true']}), ('read_file', {'path': 'src/app.py'}),
                               ('write_file', {'path': 'a', 'content': 'b'})]:
                res = broker.call(name, args)
                self.assertEqual((res['ok'], res['code']), (False, expected))
            self.assertEqual(runner.calls, [])
            self.assertNotIn('tool_started', rec.kinds())
            self.assertEqual(rec.checkpoints, [])

    def test_guard_exception_fails_closed(self):
        def boom():
            raise RuntimeError('lease store down')
        broker, runner, rec, _ = make(guard=boom)
        self.assertEqual(broker.call('list_files', {})['code'], 'policy_denied')
        self.assertEqual(runner.calls, [])

    def test_stop_during_tool_withholds_source_and_blocks_next_tool(self):
        broker, runner, rec, guard = make()
        original = runner.read_file

        def read_then_stop(handle, path):
            data = original(handle, path)
            guard.code = 'stop_requested'  # stop arrives while the tool is running
            return data
        runner.read_file = read_then_stop
        res = broker.call('read_file', {'path': 'src/app.py'})
        self.assertEqual(res['code'], 'stop_requested')
        self.assertNotIn(SECRET, res['text'])
        self.assertEqual(broker.call('list_files', {})['code'], 'stop_requested')
        self.assertEqual(runner.calls, ['read_file'])

    def test_checkpoint_after_mutations_and_commands_only(self):
        broker, runner, rec, _ = make()
        broker.call('read_file', {'path': 'src/app.py'})
        broker.call('list_files', {})
        self.assertEqual(rec.checkpoints, [])
        broker.call('write_file', {'path': 'src/new.py', 'content': 'y = 2\n'})
        self.assertIn('src/new.py', rec.checkpoints[-1])
        broker.call('delete_file', {'path': 'src/new.py'})
        self.assertNotIn('src/new.py', rec.checkpoints[-1])
        broker.call('run_command', {'argv': ['python3', '-m', 'unittest']})
        self.assertEqual(len(rec.checkpoints), 3)
        self.assertTrue(broker.checkpoint_now())
        self.assertEqual(len(rec.checkpoints), 4)

    def test_events_are_bounded_summaries_without_source_or_output(self):
        broker, runner, rec, _ = make()
        broker.call('read_file', {'path': 'src/app.py'})
        broker.call('write_file', {'path': 'w.txt', 'content': SECRET * 3})
        broker.call('search_text', {'query': SECRET})
        broker.call('run_command', {'argv': ['sh', '-c', f'echo {SECRET}']})
        blob = json.dumps(rec.events)
        self.assertNotIn(SECRET, blob)
        started = [p for k, p in rec.events if k == 'tool_started' and p['tool'] == 'run_command']
        self.assertEqual(started, [{'tool': 'run_command', 'program': 'sh', 'argc': 3}])
        for _, payload in rec.events:
            for value in payload.values():
                self.assertTrue(value is None or isinstance(value, (bool, int)) or len(value) <= 201)

    def test_unsafe_and_excluded_paths_denied_before_runner(self):
        broker, runner, rec, _ = make({'src/app.py': b'a', '.git/config': b'[core]', 'secrets/k': b'k'},
                                      excluded_paths=['secrets/*'])
        for name, args, code in [
            ('write_file', {'path': '.git/hooks/pre-commit', 'content': 'x'}, 'policy_denied'),
            ('write_file', {'path': 'secrets/new', 'content': 'x'}, 'policy_denied'),
            ('read_file', {'path': 'secrets/k'}, 'policy_denied'),
            ('delete_file', {'path': '.git/config'}, 'policy_denied'),
            ('write_file', {'path': '../outside', 'content': 'x'}, 'unsafe_path'),
            ('read_file', {'path': '/etc/passwd'}, 'unsafe_path'),
        ]:
            with self.subTest(args=args):
                self.assertEqual(broker.call(name, args)['code'], code)
        self.assertEqual(runner.calls, [])
        self.assertEqual(broker.call('list_files', {})['text'], 'src/app.py')
        self.assertEqual(rec.checkpoints, [])

    def test_search_returns_bounded_matches(self):
        broker, *_ = make({'a.py': b'needle one\nskip\nneedle two\n', 'b.py': b'needle\n'})
        res = broker.call('search_text', {'query': 'needle', 'max_results': 2})
        self.assertTrue(res['ok'])
        self.assertEqual(res['text'].splitlines()[:2], ['a.py:1:needle one', 'a.py:3:needle two'])
        self.assertIn('[results truncated]', res['text'])

    def test_argument_validation_rejects_identifiers_and_bad_types(self):
        broker, runner, *_ = make()
        for name, args in [
            ('read_file', {'path': 'a', 'container_id': 'x'}),
            ('read_file', {}),
            ('run_command', {'argv': 'rm -rf /'}),
            ('run_command', {'argv': []}),
            ('run_command', {'argv': ['true'], 'timeout_seconds': True}),
            ('run_command', {'argv': ['true'], 'timeout_seconds': 10_000}),
            ('write_file', {'path': 'a', 'content': b'bytes'}),
            ('search_text', {'query': ''}),
            ('list_files', ['positional']),
        ]:
            with self.subTest(name=name, args=args):
                self.assertEqual(broker.call(name, args)['code'], 'invalid_arguments')
        self.assertEqual(broker.call('shell', {})['code'], 'unknown_tool')
        self.assertEqual(runner.calls, [])

    def test_ask_user_honest_when_unsupported(self):
        broker, runner, rec, _ = make()
        res = broker.call('ask_user', {'question': 'Which module?'})
        self.assertEqual((res['ok'], res['code']), (False, 'unsupported'))
        self.assertIn('not supported', res['text'])
        broker2, *_ = make(ask_user=lambda q: 'use module B')
        self.assertEqual(broker2.call('ask_user', {'question': 'Which?'})['text'], 'use module B')

    def test_event_persistence_failure_prevents_start(self):
        runner = MemoryRunner({'a': b'1'})

        def bad_emit(kind, payload):
            raise OSError('disk full')
        broker = ToolBroker(runner, object(), guard=lambda: None, emit=bad_emit)
        self.assertEqual(broker.call('write_file', {'path': 'a', 'content': '2'})['code'], 'event_unavailable')
        self.assertEqual(runner.calls, [])

    def test_runner_errors_surface_stable_codes(self):
        broker, *_ = make()
        res = broker.call('read_file', {'path': 'missing.txt'})
        self.assertEqual((res['ok'], res['code']), (False, 'not_found'))
        broker.close()
        self.assertEqual(broker.call('list_files', {})['code'], 'broker_closed')


def _connect(info, token):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(info['socket_path'])
    f = sock.makefile('rb')
    sock.sendall(json.dumps({'token': token}).encode() + b'\n')
    return sock, f


class EndpointTest(unittest.TestCase):
    def setUp(self):
        self.broker, self.runner, self.rec, self.guard = make()
        self.info = self.broker.start()
        self.addCleanup(self.broker.close)

    def _token(self):
        return Path(self.info['token_file']).read_text()

    def test_private_permissions(self):
        directory = os.path.dirname(self.info['socket_path'])
        self.assertEqual(stat.S_IMODE(os.lstat(directory).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.lstat(self.info['token_file']).st_mode), 0o600)
        st = os.lstat(self.info['socket_path'])
        self.assertTrue(stat.S_ISSOCK(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode) & 0o077, 0)
        self.assertEqual(set(self.info), {'socket_path', 'token_file'})
        with self.assertRaises(RunnerError):
            self.broker.start()

    def test_wrong_token_rejected_without_tool_access(self):
        sock, f = _connect(self.info, 'wrong-' + secrets.token_hex(8))
        self.assertEqual(json.loads(f.readline()), {'ok': False, 'error': 'unauthorized'})
        self.assertEqual(f.readline(), b'')
        sock.close()
        self.assertEqual(self.runner.calls, [])

    def test_close_revokes_live_connection_and_restart_invalidates_old_token(self):
        old_token, old_info = self._token(), self.info
        sock, f = _connect(self.info, old_token)
        self.assertTrue(json.loads(f.readline())['ok'])
        sock.sendall(b'{"id":1,"op":"list"}\n')
        self.assertEqual(len(json.loads(f.readline())['tools']), len(TOOL_NAMES))
        self.assertTrue(self.broker.close()['revoked'])
        self.assertFalse(os.path.exists(os.path.dirname(old_info['socket_path'])))
        try:
            sock.sendall(b'{"id":2,"op":"list"}\n')
            self.assertEqual(f.readline(), b'')
        except OSError:
            pass
        sock.close()
        successor, runner2, *_ = make()
        new_info = successor.start()
        self.addCleanup(successor.close)
        self.assertNotEqual(new_info['socket_path'], old_info['socket_path'])
        s2, f2 = _connect(new_info, old_token)
        self.assertFalse(json.loads(f2.readline())['ok'])
        s2.close()
        self.assertEqual(runner2.calls, [])


def _bridge(info, env=None):
    env = dict(os.environ if env is None else env)
    env['PYTHONPATH'] = str(REPO)
    return subprocess.Popen([sys.executable, '-m', 'opendevengine.execution.mcp_server', '--socket', info['socket_path'],
                             '--token-file', info['token_file']], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=tempfile.gettempdir(), env=env)


class McpClient:
    def __init__(self, proc):
        self.proc, self.next_id = proc, 0

    def send(self, obj):
        raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.proc.stdin.write(raw + b'\n')
        self.proc.stdin.flush()

    def recv(self):
        line = self.proc.stdout.readline()
        assert line, self.proc.stderr.read().decode()
        return json.loads(line)

    def request(self, method, params=None):
        self.next_id += 1
        msg = {'jsonrpc': '2.0', 'id': self.next_id, 'method': method}
        if params is not None:
            msg['params'] = params
        self.send(msg)
        reply = self.recv()
        assert reply['id'] == self.next_id, reply
        return reply

    def initialize(self, version='2025-06-18'):
        reply = self.request('initialize', {'protocolVersion': version, 'capabilities': {},
                                            'clientInfo': {'name': 'test-client', 'version': '0'}})
        self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        return reply

    def finish(self):
        self.proc.stdin.close()
        code = self.proc.wait(timeout=10)
        rest = self.proc.stdout.read()
        self.proc.stdout.close()
        self.proc.stderr.close()
        return code, rest


class McpProtocolTest(unittest.TestCase):
    def setUp(self):
        self.broker, self.runner, self.rec, self.guard = make()
        self.info = self.broker.start()
        self.addCleanup(self.broker.close)
        self.client = McpClient(_bridge(self.info))
        self.addCleanup(lambda: self.client.proc.poll() is None and self.client.proc.kill())

    def test_lifecycle_tools_list_and_call(self):
        c = self.client
        self.assertEqual(c.request('tools/list')['error']['code'], -32002)  # before initialize
        self.assertEqual(c.request('ping')['result'], {})
        init = c.initialize('2025-06-18')['result']
        self.assertEqual(init['protocolVersion'], '2025-06-18')
        self.assertEqual(init['capabilities'], {'tools': {'listChanged': False}})
        self.assertEqual(init['serverInfo']['name'], 'opendevengine-session-broker')
        self.assertEqual(c.request('initialize', {'protocolVersion': '2025-06-18'})['error']['code'], -32600)
        tools = c.request('tools/list')['result']['tools']
        self.assertEqual([t['name'] for t in tools], list(TOOL_NAMES))
        wrote = c.request('tools/call', {'name': 'write_file', 'arguments': {'path': 'src/fix.py', 'content': 'ok = True\n'}})
        self.assertFalse(wrote['result']['isError'])
        read = c.request('tools/call', {'name': 'read_file', 'arguments': {'path': 'src/fix.py'}})
        self.assertEqual(read['result']['content'], [{'type': 'text', 'text': 'ok = True\n'}])
        ran = c.request('tools/call', {'name': 'run_command', 'arguments': {'argv': ['python3', '-c', 'pass']}})
        self.assertEqual(json.loads(ran['result']['content'][0]['text'])['exit_code'], 0)
        bad = c.request('tools/call', {'name': 'read_file', 'arguments': {'path': '../../etc/passwd'}})
        self.assertTrue(bad['result']['isError'])
        self.assertIn('unsafe_path', bad['result']['content'][0]['text'])
        asked = c.request('tools/call', {'name': 'ask_user', 'arguments': {'question': 'ok?'}})
        self.assertTrue(asked['result']['isError'])
        self.assertIn('unsupported', asked['result']['content'][0]['text'])
        self.assertEqual(c.request('tools/call', {'name': 'bash', 'arguments': {}})['error']['code'], -32602)
        for method in ['resources/list', 'prompts/list', 'sampling/createMessage', 'roots/list', 'logging/setLevel']:
            self.assertEqual(c.request(method)['error']['code'], -32601, method)
        self.assertEqual(self.runner.files['src/fix.py'], b'ok = True\n')
        self.assertTrue(self.rec.checkpoints)
        code, rest = c.finish()
        self.assertEqual((code, rest), (0, b''))

    def test_malformed_messages_get_jsonrpc_errors(self):
        c = self.client
        c.initialize()
        c.send(b'{not json')
        self.assertEqual(c.recv(), {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}})
        c.send([{'jsonrpc': '2.0', 'id': 9, 'method': 'ping'}])
        self.assertEqual(c.recv()['error']['code'], -32600)
        c.send({'id': 3, 'method': 'ping'})
        self.assertEqual(c.recv()['error']['code'], -32600)
        c.send({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {'requestId': 1}})
        self.assertEqual(c.request('tools/call', {'name': 'list_files', 'arguments': 'x'})['error']['code'], -32602)
        self.assertEqual(c.request('ping')['result'], {})  # notification produced no output line
        self.assertEqual(c.finish()[0], 0)

    def test_unknown_protocol_version_negotiates_supported(self):
        init = self.client.initialize('1999-01-01')['result']
        self.assertEqual(init['protocolVersion'], '2025-11-25')
        self.client.finish()

    def test_stop_request_surfaces_as_tool_error_through_mcp(self):
        c = self.client
        c.initialize()
        self.guard.code = 'stop_requested'
        res = c.request('tools/call', {'name': 'run_command', 'arguments': {'argv': ['true']}})
        self.assertTrue(res['result']['isError'])
        self.assertIn('stop_requested', res['result']['content'][0]['text'])
        self.assertEqual(self.runner.calls, [])
        self.broker.close()
        res = c.request('tools/call', {'name': 'list_files', 'arguments': {}})
        self.assertTrue(res['result']['isError'])
        self.assertIn('broker_unavailable', res['result']['content'][0]['text'])
        c.finish()


class BridgeEndpointValidationTest(unittest.TestCase):
    def setUp(self):
        self.broker, self.runner, *_ = make()
        self.info = self.broker.start()
        self.token = Path(self.info['token_file']).read_bytes()
        self.addCleanup(self.broker.close)

    def _expect_refusal(self, info):
        proc = _bridge(info)
        out, err = proc.communicate(b'', timeout=15)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(out, b'')  # nothing but protocol may reach stdout
        self.assertNotIn(self.token, err)
        return err

    def test_rejects_world_readable_token(self):
        os.chmod(self.info['token_file'], 0o644)
        self.assertIn(b'not private', self._expect_refusal(self.info))

    def test_rejects_group_accessible_directory(self):
        os.chmod(os.path.dirname(self.info['socket_path']), 0o750)
        self._expect_refusal(self.info)

    def test_rejects_symlinked_token_and_relative_or_split_paths(self):
        other = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(other))
        os.chmod(other, 0o700)
        link = os.path.join(os.path.dirname(self.info['socket_path']), 'tok-link')
        os.symlink(self.info['token_file'], link)
        self._expect_refusal({'socket_path': self.info['socket_path'], 'token_file': link})
        self._expect_refusal({'socket_path': 'rel/s', 'token_file': 'rel/token'})
        copy = os.path.join(other, 'token')
        Path(copy).write_bytes(Path(self.info['token_file']).read_bytes())
        os.chmod(copy, 0o600)
        self._expect_refusal({'socket_path': self.info['socket_path'], 'token_file': copy})

    def test_revoked_endpoint_refused(self):
        info = dict(self.info)
        self.broker.close()
        self._expect_refusal(info)
        self.assertEqual(self.runner.calls, [])


DOCKER_TESTS = os.environ.get('OPENDEVENGINE_DOCKER_TESTS') == '1'
TEST_IMAGE = os.environ.get('OPENDEVENGINE_TEST_IMAGE', 'debian:stable-slim')


@unittest.skipUnless(DOCKER_TESTS, 'set OPENDEVENGINE_DOCKER_TESTS=1 to run real Docker broker checks')
class BrokerOverRealDockerTest(unittest.TestCase):
    def test_mcp_bridge_edits_real_container_and_stop_blocks_tools(self):
        from opendevengine.execution import DockerRunner
        runner = DockerRunner('btest' + secrets.token_hex(4))
        handle = runner.provision('mcpsess', {'calc.py': b'def add(a, b):\n    return a - b\n'},
                                  image=TEST_IMAGE, limits={})
        self.addCleanup(runner.discard, handle)
        self.addCleanup(runner.stop, handle)
        rec, guard = Recorder(), GuardSwitch()
        broker = ToolBroker(runner, handle, guard=guard, emit=rec.emit, checkpoint=rec.checkpoint)
        info = broker.start()
        self.addCleanup(broker.close)
        c = McpClient(_bridge(info))
        c.initialize()
        c.request('tools/call', {'name': 'write_file', 'arguments': {
            'path': 'calc.py', 'content': 'def add(a, b):\n    return a + b\n'}})
        found = c.request('tools/call', {'name': 'search_text', 'arguments': {'query': 'a + b'}})
        self.assertIn('calc.py:2:', found['result']['content'][0]['text'])
        ran = c.request('tools/call', {'name': 'run_command', 'arguments': {
            'argv': ['sh', '-c', 'grep -q "a + b" calc.py && echo validated']}})
        self.assertIn('validated', ran['result']['content'][0]['text'])
        self.assertEqual(rec.checkpoints[-1]['calc.py'], b'def add(a, b):\n    return a + b\n')
        guard.code = 'stop_requested'
        self.assertTrue(broker.checkpoint_now())
        self.assertEqual(runner.stop(handle)['confirmed'], True)
        res = c.request('tools/call', {'name': 'list_files', 'arguments': {}})
        self.assertIn('stop_requested', res['result']['content'][0]['text'])
        c.finish()


if __name__ == '__main__':
    unittest.main()
