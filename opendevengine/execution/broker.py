"""Guarded tool broker for one pinned session container (M2 spike).

The broker is the only component allowed to touch the runner handle. Model-facing
tool arguments carry workspace-relative paths, text and argv only. ``guard`` is
consulted before every new tool and again before any result is returned; a denial
never starts a tool or invokes Docker. Events carry bounded summaries, never file
content, command output or reasoning.

``start()`` exposes a private per-attempt Unix socket plus a 0600 token file inside
a freshly created 0700 directory. ``close()`` revokes the token, drops connections
and refuses further tools. A new broker always gets a new directory and token.
"""
from __future__ import annotations

import fnmatch
import hmac
import json
import os
import secrets
import shutil
import socket
import stat
import struct
import sys
import tempfile
import threading

from .runner import RunnerError, validate_relative_path

__all__ = ['ToolBroker', 'DENIAL_CODES', 'TOOL_NAMES', 'MAX_LINE_BYTES']

DENIAL_CODES = frozenset({'stop_requested', 'lease_lost', 'budget_exhausted', 'policy_denied'})
TOOL_NAMES = ('list_files', 'read_file', 'search_text', 'write_file', 'delete_file', 'run_command', 'ask_user')
MUTATING = frozenset({'write_file', 'delete_file', 'run_command'})
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_TEXT_CHARS = 512 * 1024
MAX_CONTENT_BYTES = 262144
MAX_QUERY_CHARS = 512
MAX_QUESTION_CHARS = 2000
MAX_ANSWER_CHARS = 8000
MAX_ARGV = 64
MAX_COMMAND_SECONDS = 300
AUTH_TIMEOUT = 5.0
_SUMMARY_CHARS = 200

_PATH = {'type': 'string', 'minLength': 1, 'maxLength': 1024,
         'description': 'Path relative to the session workspace root.'}
TOOL_SPECS = {
    'list_files': ('List regular files in the session workspace.',
                   {'type': 'object', 'properties': {}, 'additionalProperties': False}, True),
    'read_file': ('Read a UTF-8 text file from the session workspace.',
                  {'type': 'object', 'properties': {'path': _PATH}, 'required': ['path'],
                   'additionalProperties': False}, True),
    'search_text': ('Search workspace files for a fixed string; returns path:line:text matches.',
                    {'type': 'object', 'properties': {
                        'query': {'type': 'string', 'minLength': 1, 'maxLength': MAX_QUERY_CHARS},
                        'path': _PATH,
                        'max_results': {'type': 'integer', 'minimum': 1, 'maximum': 200}},
                     'required': ['query'], 'additionalProperties': False}, True),
    'write_file': ('Create or replace a UTF-8 text file in the session workspace.',
                   {'type': 'object', 'properties': {
                       'path': _PATH, 'content': {'type': 'string', 'maxLength': MAX_CONTENT_BYTES}},
                    'required': ['path', 'content'], 'additionalProperties': False}, False),
    'delete_file': ('Delete one file from the session workspace.',
                    {'type': 'object', 'properties': {'path': _PATH}, 'required': ['path'],
                     'additionalProperties': False}, False),
    'run_command': ('Run an argv command inside the isolated session container (cwd is the '
                    'workspace, no network). Shell argv such as ["sh","-c","..."] runs only there.',
                    {'type': 'object', 'properties': {
                        'argv': {'type': 'array', 'minItems': 1, 'maxItems': MAX_ARGV,
                                 'items': {'type': 'string', 'maxLength': 8192}},
                        'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': MAX_COMMAND_SECONDS}},
                     'required': ['argv'], 'additionalProperties': False}, False),
    'ask_user': ('Ask the user a clarifying question. May be unsupported in this session.',
                 {'type': 'object', 'properties': {
                     'question': {'type': 'string', 'minLength': 1, 'maxLength': MAX_QUESTION_CHARS}},
                  'required': ['question'], 'additionalProperties': False}, True),
}


class _ToolFailure(Exception):
    def __init__(self, code, message=''):
        super().__init__(code)
        self.code, self.message = code, message or code


def _short(value, limit=_SUMMARY_CHARS):
    text = str(value)
    return text if len(text) <= limit else text[:limit] + '…'


def _peer_uid(conn):
    """Best-effort peer UID; None when the platform cannot report it."""
    try:
        if hasattr(socket, 'SO_PEERCRED'):
            raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
            return struct.unpack('3i', raw)[1]
        if sys.platform == 'darwin':
            # struct xucred {u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t cr_groups[16];}
            raw = conn.getsockopt(0, 0x001, 76)  # SOL_LOCAL, LOCAL_PEERCRED
            version, uid = struct.unpack_from('II', raw)
            return uid if version == 0 else None
    except OSError:
        return None
    return None


class ToolBroker:
    def __init__(self, runner, handle, *, guard, emit, checkpoint=None, ask_user=None,
                 excluded_paths=()):
        if not callable(guard) or not callable(emit):
            raise TypeError('guard and emit must be callable')
        self._runner, self._handle = runner, handle
        self._guard, self._emit = guard, emit
        self._checkpoint, self._ask_user = checkpoint, ask_user
        self._excluded = tuple(str(p) for p in excluded_paths)
        self._call_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._closed = False
        self._server = None
        self._dir = None
        self._token = None
        self._conns: set = set()
        self._accept_thread = None

    # ------------------------------------------------------------- policy
    def _denial(self):
        try:
            code = self._guard()
        except Exception:
            return 'policy_denied'  # fail closed
        if code is None:
            return None
        return code if code in DENIAL_CODES else 'policy_denied'

    def _safe_emit(self, kind, payload) -> bool:
        clean = {}
        for key, value in payload.items():
            if isinstance(value, bool) or value is None or isinstance(value, int):
                clean[key] = value
            else:
                clean[key] = _short(value)
        try:
            self._emit(kind, clean)
            return True
        except Exception:
            return False

    def _path(self, value, *, write=False):
        path = validate_relative_path(value)
        parts = path.split('/')
        if '.git' in parts or any(fnmatch.fnmatchcase(path, pat) for pat in self._excluded):
            raise _ToolFailure('policy_denied', 'path is excluded by session policy')
        return path

    def _visible(self, path):
        parts = path.split('/')
        return '.git' not in parts and not any(fnmatch.fnmatchcase(path, pat) for pat in self._excluded)

    # -------------------------------------------------------------- tools
    def tools(self) -> list[dict]:
        out = []
        for name in TOOL_NAMES:
            description, schema, read_only = TOOL_SPECS[name]
            annotations = {'readOnlyHint': read_only, 'openWorldHint': False}
            if name == 'delete_file':
                annotations['destructiveHint'] = True
            out.append({'name': name, 'description': description, 'inputSchema': schema,
                        'annotations': annotations})
        return out

    @staticmethod
    def _check_args(name, args):
        _, schema, _ = TOOL_SPECS[name]
        if not isinstance(args, dict):
            raise _ToolFailure('invalid_arguments', 'arguments must be an object')
        props = schema['properties']
        if set(args) - set(props):
            raise _ToolFailure('invalid_arguments', 'unexpected argument')
        for key in schema.get('required', ()):
            if key not in args:
                raise _ToolFailure('invalid_arguments', f'missing argument {key}')
        for key, value in args.items():
            spec = props[key]
            if spec['type'] == 'string':
                if not isinstance(value, str) or len(value) < spec.get('minLength', 0) or len(value) > spec['maxLength']:
                    raise _ToolFailure('invalid_arguments', f'invalid {key}')
            elif spec['type'] == 'integer':
                if isinstance(value, bool) or not isinstance(value, int) or not spec['minimum'] <= value <= spec['maximum']:
                    raise _ToolFailure('invalid_arguments', f'invalid {key}')
            elif spec['type'] == 'array':
                if (not isinstance(value, list) or not spec['minItems'] <= len(value) <= spec['maxItems']
                        or not all(isinstance(v, str) and len(v) <= spec['items']['maxLength'] for v in value)):
                    raise _ToolFailure('invalid_arguments', f'invalid {key}')

    def call(self, name, arguments=None) -> dict:
        """Run one tool. Returns {'ok', 'text', 'code'}; never raises for tool errors."""
        if not isinstance(name, str) or name not in TOOL_SPECS:
            return {'ok': False, 'code': 'unknown_tool', 'text': 'unknown tool'}
        arguments = {} if arguments is None else arguments
        with self._call_lock:
            if self._closed:
                return {'ok': False, 'code': 'broker_closed', 'text': 'broker closed'}
            denial = self._denial()
            if denial:
                self._safe_emit('tool_denied', {'tool': name, 'code': denial, 'phase': 'before'})
                return {'ok': False, 'code': denial, 'text': f'denied: {denial}'}
            try:
                self._check_args(name, arguments)
            except _ToolFailure as exc:
                return {'ok': False, 'code': exc.code, 'text': exc.message}
            if not self._safe_emit('tool_started', {'tool': name, **self._arg_summary(name, arguments)}):
                return {'ok': False, 'code': 'event_unavailable', 'text': 'event persistence failed; tool not started'}
            try:
                text, summary = getattr(self, '_tool_' + name)(arguments)
                ok, code = True, None
            except _ToolFailure as exc:
                ok, code, text, summary = False, exc.code, exc.message, {}
            except RunnerError as exc:
                ok, code, text, summary = False, exc.code, exc.message, {}
            except Exception:
                ok, code, text, summary = False, 'internal', 'tool failed', {}
            if name in MUTATING and code not in ('invalid_arguments', 'policy_denied', 'unsafe_path'):
                self._do_checkpoint(name)
            denial = self._denial()  # nothing is returned once the session lost authority
            if denial:
                self._safe_emit('tool_denied', {'tool': name, 'code': denial, 'phase': 'after'})
                return {'ok': False, 'code': denial, 'text': f'denied: {denial}'}
            self._safe_emit('tool_finished', {'tool': name, 'ok': ok, 'code': code, **summary})
            if len(text) > MAX_TEXT_CHARS:
                text = text[:MAX_TEXT_CHARS] + '\n[truncated by broker]'
            return {'ok': ok, 'code': code, 'text': text}

    @staticmethod
    def _arg_summary(name, args):
        if 'path' in args:
            return {'path': args['path']}
        if name == 'run_command':
            return {'program': os.path.basename(args['argv'][0])[:64], 'argc': len(args['argv'])}
        if name == 'search_text':
            return {'query_chars': len(args['query'])}
        if name == 'ask_user':
            return {'question_chars': len(args['question'])}
        return {}

    def _do_checkpoint(self, reason) -> bool:
        if self._checkpoint is None:
            return False
        try:
            snap = self._runner.snapshot(self._handle)
            self._checkpoint(snap)
        except RunnerError as exc:
            self._safe_emit('checkpoint_failed', {'reason': reason, 'code': exc.code})
            return False
        except Exception:
            self._safe_emit('checkpoint_failed', {'reason': reason, 'code': 'internal'})
            return False
        self._safe_emit('checkpoint_saved', {'reason': reason, 'files': len(snap),
                                             'bytes': sum(len(v) for v in snap.values())})
        return True

    def checkpoint_now(self) -> bool:
        """Trusted caller hook: checkpoint before termination where possible."""
        with self._call_lock:
            return self._do_checkpoint('termination')

    def _tool_list_files(self, args):
        names = [n for n in self._runner.list_files(self._handle) if self._visible(n)]
        return '\n'.join(names), {'count': len(names)}

    def _tool_read_file(self, args):
        path = self._path(args['path'])
        data = self._runner.read_file(self._handle, path)
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            raise _ToolFailure('binary_file', 'file is not UTF-8 text') from None
        return text, {'bytes': len(data)}

    def _tool_search_text(self, args):
        target = self._path(args['path']) if 'path' in args else '.'
        limit = args.get('max_results', 50)
        res = self._runner.run_command(
            self._handle, ['grep', '-r', '-n', '-I', '-F', '-Z', '--max-count=20', '-e', args['query'], '--', target],
            timeout_seconds=30)
        if res['timed_out']:
            raise _ToolFailure('timeout', 'search timed out')
        if res['exit_code'] not in (0, 1):
            raise _ToolFailure('search_failed', 'search failed')
        lines, truncated = [], res['truncated']
        for raw in res['stdout'].split('\n'):
            if '\0' not in raw:
                continue
            fname, rest = raw.split('\0', 1)
            if fname.startswith('./'):
                fname = fname[2:]
            try:
                fname = validate_relative_path(fname)
            except RunnerError:
                continue
            if not self._visible(fname):
                continue
            if len(lines) >= limit:
                truncated = True
                break
            lines.append(f'{fname}:{rest[:400]}')
        text = '\n'.join(lines) + ('\n[results truncated]' if truncated else '')
        return text, {'matches': len(lines), 'truncated': truncated}

    def _tool_write_file(self, args):
        path = self._path(args['path'], write=True)
        content = args['content'].encode('utf-8')
        if len(content) > MAX_CONTENT_BYTES:
            raise _ToolFailure('limit_exceeded', 'content too large')
        self._runner.write_file(self._handle, path, content)
        return f'wrote {len(content)} bytes to {path}', {'bytes': len(content)}

    def _tool_delete_file(self, args):
        path = self._path(args['path'], write=True)
        self._runner.delete_file(self._handle, path)
        return f'deleted {path}', {}

    def _tool_run_command(self, args):
        res = self._runner.run_command(self._handle, list(args['argv']),
                                       timeout_seconds=args.get('timeout_seconds', 30))
        payload = {k: res[k] for k in ('exit_code', 'timed_out', 'truncated', 'stdout', 'stderr')}
        summary = {'exit_code': res['exit_code'], 'timed_out': res['timed_out'], 'truncated': res['truncated'],
                   'stdout_chars': len(res['stdout']), 'stderr_chars': len(res['stderr'])}
        return json.dumps(payload, ensure_ascii=False), summary

    def _tool_ask_user(self, args):
        if self._ask_user is None:
            raise _ToolFailure('unsupported', 'ask_user is not supported in this session; no question was delivered')
        answer = self._ask_user(args['question'])
        if not isinstance(answer, str):
            raise _ToolFailure('no_answer', 'no answer was received')
        return answer[:MAX_ANSWER_CHARS], {'answer_chars': len(answer)}

    # ----------------------------------------------------------- endpoint
    def start(self, runtime_root=None) -> dict:
        """Create the private endpoint. Returns paths for the trusted caller only."""
        with self._state_lock:
            if self._closed:
                raise RunnerError('broker_closed', 'broker closed; create a new broker')
            if self._server is not None:
                raise RunnerError('already_started', 'broker endpoint already started')
            root = runtime_root or tempfile.gettempdir()
            directory = tempfile.mkdtemp(prefix='odv-', dir=root)
            try:
                os.chmod(directory, 0o700)
                st = os.lstat(directory)
                if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                    raise RunnerError('endpoint_untrusted', 'runtime directory is not private')
                token = secrets.token_urlsafe(32)
                token_path = os.path.join(directory, 'token')
                fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
                with os.fdopen(fd, 'w') as fh:
                    fh.write(token)
                sock_path = os.path.join(directory, 's')
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                server.bind(sock_path)
                os.chmod(sock_path, 0o600)
                server.listen(8)
                server.settimeout(0.25)
            except BaseException:
                shutil.rmtree(directory, ignore_errors=True)
                raise
            self._server, self._dir, self._token = server, directory, token.encode('ascii')
            self._accept_thread = threading.Thread(target=self._accept_loop, args=(server,), daemon=True)
            self._accept_thread.start()
        self._safe_emit('broker_started', {})
        return {'socket_path': sock_path, 'token_file': token_path}

    def close(self) -> dict:
        with self._state_lock:
            already = self._closed
            self._closed = True
            server, directory = self._server, self._dir
            self._server, self._token = None, None
            conns = list(self._conns)
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        for conn in conns:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        if directory:
            shutil.rmtree(directory, ignore_errors=True)
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2)
        if not already:
            self._safe_emit('broker_closed', {})
        return {'closed': True, 'revoked': directory is not None}

    def _accept_loop(self, server):
        while True:
            with self._state_lock:
                if self._server is not server:
                    return
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._state_lock:
                if self._server is not server:
                    conn.close()
                    return
                self._conns.add(conn)
            threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()

    def _serve_conn(self, conn):
        reader = None
        try:
            peer = _peer_uid(conn)
            if peer is not None and peer != os.getuid():
                return
            conn.settimeout(AUTH_TIMEOUT)
            reader = conn.makefile('rb')
            line = reader.readline(4096)
            try:
                hello = json.loads(line)
            except ValueError:
                return
            with self._state_lock:
                expected = self._token
            supplied = hello.get('token') if isinstance(hello, dict) else None
            if (expected is None or not isinstance(supplied, str)
                    or not hmac.compare_digest(supplied.encode('utf-8'), expected)):
                self._send(conn, {'ok': False, 'error': 'unauthorized'})
                return
            self._send(conn, {'ok': True})
            conn.settimeout(None)
            while True:
                line = reader.readline(MAX_LINE_BYTES + 1)
                if not line:
                    return
                if len(line) > MAX_LINE_BYTES or not line.endswith(b'\n'):
                    self._send(conn, {'id': None, 'error': 'line_too_long'})
                    return
                try:
                    req = json.loads(line)
                except ValueError:
                    self._send(conn, {'id': None, 'error': 'bad_request'})
                    continue
                if not isinstance(req, dict):
                    self._send(conn, {'id': None, 'error': 'bad_request'})
                    continue
                rid, op = req.get('id'), req.get('op')
                with self._state_lock:
                    revoked = self._token is None
                if revoked:
                    self._send(conn, {'id': rid, 'error': 'revoked'})
                    return
                if op == 'list':
                    self._send(conn, {'id': rid, 'tools': self.tools()})
                elif op == 'call':
                    self._send(conn, {'id': rid, 'result': self.call(req.get('name'), req.get('arguments'))})
                else:
                    self._send(conn, {'id': rid, 'error': 'bad_request'})
        except OSError:
            return
        finally:
            if reader is not None:
                try:
                    reader.close()
                except OSError:
                    pass
            with self._state_lock:
                self._conns.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _send(conn, obj):
        conn.sendall(json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n')
