"""MCP stdio bridge to a private ToolBroker endpoint (dependency-free).

Usage: python -m opendevengine.execution.mcp_server --socket PATH --token-file PATH

Speaks newline-delimited JSON-RPC 2.0 on stdin/stdout implementing the MCP
lifecycle (initialize / notifications/initialized / ping) and tools/list,
tools/call only. No resources, prompts, sampling, roots or logging capability is
advertised or served. Nothing but protocol messages is written to stdout;
diagnostics go to stderr without secrets.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import sys

__all__ = ['main', 'serve', 'BrokerClient', 'validate_endpoint', 'SUPPORTED_PROTOCOL_VERSIONS']

SUPPORTED_PROTOCOL_VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
SERVER_INFO = {'name': 'opendevengine-session-broker', 'version': '0.1.0'}
MAX_STDIN_LINE = 4 * 1024 * 1024
MAX_BROKER_LINE = 8 * 1024 * 1024
MAX_TOKEN_BYTES = 512

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
NOT_INITIALIZED = -32002


class EndpointError(Exception):
    pass


def _private(st, kind_check) -> bool:
    return kind_check(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o077


def validate_endpoint(socket_path: str, token_file: str) -> bytes:
    """Validate the broker's private directory, socket and token file; return the token."""
    if not os.path.isabs(socket_path) or not os.path.isabs(token_file):
        raise EndpointError('endpoint paths must be absolute')
    directory = os.path.dirname(socket_path)
    if os.path.dirname(token_file) != directory:
        raise EndpointError('socket and token must share one private directory')
    try:
        if not _private(os.lstat(directory), stat.S_ISDIR):
            raise EndpointError('endpoint directory is not private')
        if not _private(os.lstat(socket_path), stat.S_ISSOCK):
            raise EndpointError('endpoint socket is not private')
        fd = os.open(token_file, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    except OSError:
        raise EndpointError('endpoint is unavailable') from None
    try:
        st = os.fstat(fd)
        if not _private(st, stat.S_ISREG) or st.st_size > MAX_TOKEN_BYTES:
            raise EndpointError('token file is not private')
        token = os.read(fd, MAX_TOKEN_BYTES + 1).strip()
    finally:
        os.close(fd)
    if not token or len(token) > MAX_TOKEN_BYTES:
        raise EndpointError('token file is empty or invalid')
    return token


class BrokerClient:
    def __init__(self, socket_path: str, token: bytes, *, timeout: float = 900.0):
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(10)
        try:
            self._sock.connect(socket_path)
            self._reader = self._sock.makefile('rb')
            self._sock.sendall(json.dumps({'token': token.decode('ascii', 'replace')}).encode() + b'\n')
            hello = self._recv()
        except (OSError, ValueError):
            self._sock.close()
            raise EndpointError('broker unavailable') from None
        if not isinstance(hello, dict) or hello.get('ok') is not True:
            self._sock.close()
            raise EndpointError('broker rejected credentials')
        self._sock.settimeout(timeout)
        self._next = 0
        self.alive = True

    def _recv(self):
        line = self._reader.readline(MAX_BROKER_LINE + 1)
        if not line or len(line) > MAX_BROKER_LINE:
            raise EndpointError('broker connection closed')
        return json.loads(line)

    def request(self, payload: dict) -> dict:
        if not self.alive:
            raise EndpointError('broker connection closed')
        self._next += 1
        payload = dict(payload, id=self._next)
        try:
            self._sock.sendall(json.dumps(payload, ensure_ascii=False).encode('utf-8') + b'\n')
            reply = self._recv()
        except (OSError, ValueError, EndpointError):
            self.alive = False
            raise EndpointError('broker connection closed') from None
        if not isinstance(reply, dict) or reply.get('id') != self._next or 'error' in reply:
            self.alive = False
            raise EndpointError('broker refused request')
        return reply

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


class _Session:
    def __init__(self, client):
        self.client = client
        self.initialized = False
        self.tool_names = None

    def _tools(self):
        reply = self.client.request({'op': 'list'})
        tools = reply.get('tools')
        if not isinstance(tools, list):
            raise EndpointError('invalid tool list')
        self.tool_names = {t.get('name') for t in tools if isinstance(t, dict)}
        return tools

    def handle(self, msg):
        """Return a JSON-RPC response dict, or None for notifications/responses."""
        if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0':
            return _error(msg.get('id') if isinstance(msg, dict) and _valid_id(msg.get('id')) else None,
                          INVALID_REQUEST, 'Invalid Request')
        method = msg.get('method')
        if 'id' not in msg:
            if method == 'notifications/initialized':
                pass  # lifecycle acknowledgement; nothing to send
            return None  # other notifications (e.g. cancelled) are ignored
        rid = msg['id']
        if not _valid_id(rid):
            return _error(None, INVALID_REQUEST, 'Invalid Request')
        if method is None and ('result' in msg or 'error' in msg):
            return None  # we never issue requests to the client
        if not isinstance(method, str):
            return _error(rid, INVALID_REQUEST, 'Invalid Request')
        params = msg.get('params', {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _error(rid, INVALID_PARAMS, 'params must be an object')
        if method == 'ping':
            return _result(rid, {})
        if method == 'initialize':
            if self.initialized:
                return _error(rid, INVALID_REQUEST, 'already initialized')
            requested = params.get('protocolVersion')
            if not isinstance(requested, str):
                return _error(rid, INVALID_PARAMS, 'protocolVersion is required')
            version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
            self.initialized = True
            return _result(rid, {
                'protocolVersion': version,
                'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': SERVER_INFO,
                'instructions': 'Tools act only on the isolated session workspace. Paths are relative.',
            })
        if not self.initialized:
            return _error(rid, NOT_INITIALIZED, 'server not initialized')
        if method == 'tools/list':
            try:
                return _result(rid, {'tools': self._tools()})
            except EndpointError:
                return _error(rid, INTERNAL_ERROR, 'broker unavailable')
        if method == 'tools/call':
            name, arguments = params.get('name'), params.get('arguments', {})
            if not isinstance(name, str):
                return _error(rid, INVALID_PARAMS, 'tool name is required')
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                return _error(rid, INVALID_PARAMS, 'arguments must be an object')
            try:
                reply = self.client.request({'op': 'call', 'name': name, 'arguments': arguments})
            except EndpointError:
                return _result(rid, {'content': [{'type': 'text', 'text': 'broker_unavailable'}], 'isError': True})
            result = reply.get('result')
            if not isinstance(result, dict):
                return _error(rid, INTERNAL_ERROR, 'invalid broker result')
            if result.get('code') == 'unknown_tool':
                return _error(rid, INVALID_PARAMS, f'Unknown tool: {name[:100]}')
            text = result.get('text') if isinstance(result.get('text'), str) else ''
            if not result.get('ok'):
                text = f"error[{result.get('code')}]: {text}"
            return _result(rid, {'content': [{'type': 'text', 'text': text}], 'isError': not result.get('ok')})
        return _error(rid, METHOD_NOT_FOUND, 'Method not found')


def _valid_id(value):
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def _result(rid, result):
    return {'jsonrpc': '2.0', 'id': rid, 'result': result}


def _error(rid, code, message):
    return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': code, 'message': message}}


def serve(stdin, stdout, client) -> int:
    """Serve MCP over binary stdio streams until EOF."""
    session = _Session(client)

    def write(obj):
        stdout.write(json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n')
        stdout.flush()

    while True:
        line = stdin.readline(MAX_STDIN_LINE + 1)
        if not line:
            return 0
        if len(line) > MAX_STDIN_LINE and not line.endswith(b'\n'):
            write(_error(None, INVALID_REQUEST, 'message too large'))
            while line and not line.endswith(b'\n'):  # discard remainder of the oversized line
                line = stdin.readline(MAX_STDIN_LINE + 1)
            continue
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            write(_error(None, PARSE_ERROR, 'Parse error'))
            continue
        if isinstance(msg, list):
            write(_error(None, INVALID_REQUEST, 'batch requests are not supported'))
            continue
        try:
            reply = session.handle(msg)
        except Exception:
            rid = msg.get('id') if isinstance(msg, dict) and _valid_id(msg.get('id')) else None
            reply = _error(rid, INTERNAL_ERROR, 'internal error')
        if reply is not None:
            write(reply)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='python -m opendevengine.execution.mcp_server', add_help=True)
    parser.add_argument('--socket', required=True)
    parser.add_argument('--token-file', required=True)
    args = parser.parse_args(argv)
    try:
        token = validate_endpoint(args.socket, args.token_file)
        client = BrokerClient(args.socket, token)
    except EndpointError as exc:
        print(f'opendevengine mcp bridge: {exc}', file=sys.stderr)
        return 2
    try:
        return serve(sys.stdin.buffer, sys.stdout.buffer, client)
    except (BrokenPipeError, KeyboardInterrupt):
        return 0
    finally:
        client.close()


if __name__ == '__main__':
    sys.exit(main())
