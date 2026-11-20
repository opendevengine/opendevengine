"""Claude Code CLI as a provider transport, on the user's own claude.ai login.

The provider service hands ``claude_cli`` chat requests to this transport instead of the
network transport. Each call runs the CLI headless with no tools, MCP servers, hooks,
settings files, plugins or session persistence, so it can only return text. The system
instruction is passed as a private file and the conversation on stdin; only the model
alias and the JSON schema appear in argv. Login and managed-policy checks are shared
with the session adapter and cached briefly.
"""
import json
import os
from pathlib import Path
import tempfile
import threading
import time

from .transport import (ResponseTooLarge, TransportCancelled, TransportResponse, TransportTimeout,
                        TransportUnavailable)

ENDPOINT = 'https://api.anthropic.com'
CHECK_TTL_SECONDS = 300
MAX_OUTPUT_BYTES = 4_194_304
_FAILURE_STATUS = {'rate_limit': 429, 'authentication': 401, 'context_overflow': 400}
_AUTH_CODES = {'authentication_required', 'authentication_unverified', 'unsupported_auth_profile'}


class _Stop:
    """``run_stream`` sets its stop event on every exit; never leak that into the caller's event."""

    def __init__(self, outer):
        self.outer, self.flag = outer, False

    def is_set(self):
        return self.flag or (self.outer is not None and self.outer.is_set())

    def set(self):
        self.flag = True


def _error(status, code, message=''):
    return TransportResponse(status, {}, json.dumps({'error': {'code': code, 'message': message}}).encode())


class ClaudeCliTransport:
    def __init__(self, *, claude_binary=None, clock=time.monotonic):
        self.claude_binary, self.clock = claude_binary, clock
        self._lock = threading.Lock()
        self._checked = None  # (expires_at, binary, disabled_plugins)

    def _preflight(self):
        from opendevengine.execution.claude import ClaudeAdapter
        with self._lock:
            if self._checked is not None and self._checked[0] > self.clock():
                return self._checked[1], self._checked[2]
        adapter = ClaudeAdapter(claude_binary=self.claude_binary)
        adapter._check_policy()
        adapter._check_auth_profile()
        plugins = adapter._disabled_plugins()
        with self._lock:
            self._checked = (self.clock() + CHECK_TTL_SECONDS, adapter.binary, plugins)
        return adapter.binary, plugins

    def send(self, request, *, cancel_event=None):
        from opendevengine.execution.adapter_process import ProcessContainmentError, run_stream
        from opendevengine.execution.claude import AdapterError, _environment, _failure_code
        body = json.loads(request.body)
        try:
            binary, plugins = self._preflight()
        except AdapterError as error:
            if error.code in _AUTH_CODES:
                return _error(401, error.code)
            if error.code in ('cli_unavailable', 'containment_failure'):
                raise TransportUnavailable('transport failure') from None
            return _error(400, error.code)
        except (ProcessContainmentError, OSError):
            raise TransportUnavailable('transport failure') from None

        messages = body['messages']
        system = '\n\n'.join(m['content'] for m in messages if m['role'] == 'system') or 'Respond to the user.'
        turns = [m for m in messages if m['role'] != 'system']
        if len(turns) == 1 and turns[0]['role'] == 'user':
            conversation = turns[0]['content']
        else:
            conversation = '\n\n'.join(f"[{m['role']}]\n{m['content']}" for m in turns)

        with tempfile.TemporaryDirectory(prefix='opendevengine-claude-provider-') as root:
            os.chmod(root, 0o700)
            cwd = Path(root) / 'cwd'
            cwd.mkdir(mode=0o700)
            plugin_root = Path(root) / 'plugins'
            plugin_root.mkdir(mode=0o700)
            system_file = Path(root) / 'system.txt'
            fd = os.open(system_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(system)
            argv = [binary, '--print', '--output-format', 'json', '--restricted', '--tools', '',
                    '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--disable-slash-commands',
                    '--setting-sources', '', '--settings', json.dumps({
                        'disableAllHooks': True, 'autoMemoryEnabled': False, 'claudeMdExcludes': ['**'],
                        'syncClaudeAiSkills': False, 'syncClaudeAiPlugins': False, 'enabledPlugins': plugins,
                    }), '--no-session-persistence', '--no-chrome',
                    '--permission-mode', 'dontAsk', '--permission-prompts', 'none',
                    '--model', body['model'], '--system-prompt-file', str(system_file)]
            if body.get('schema') is not None:
                argv += ['--json-schema', json.dumps(body['schema'])]
            env = _environment()
            env['CLAUDE_CODE_PLUGIN_CACHE_DIR'] = str(plugin_root)
            lines = []
            try:
                with tempfile.TemporaryFile(mode='w+b', dir=root) as stdin_file:
                    stdin_file.write(conversation.encode('utf-8'))
                    stdin_file.flush()
                    stdin_file.seek(0)
                    outcome = run_stream(
                        argv, cwd=str(cwd), env=env, on_line=lines.append,
                        timeout_seconds=min(max(1.0, float(request.timeout)), 3600.0),
                        max_output_bytes=max(1024, min(int(request.max_response_bytes), MAX_OUTPUT_BYTES)),
                        stop_event=_Stop(cancel_event), stdin_file=stdin_file)
            except (ProcessContainmentError, OSError, ValueError):
                raise TransportUnavailable('transport failure') from None
        if outcome.reason == 'cancelled':
            raise TransportCancelled('transport failure')
        if outcome.reason == 'timeout':
            raise TransportTimeout('transport failure')
        if outcome.reason == 'output_limit':
            raise ResponseTooLarge('transport failure')

        result = None
        for line in reversed(lines):
            try:
                candidate = json.loads(line)
            except ValueError:
                continue
            if isinstance(candidate, dict) and candidate.get('type') == 'result':
                result = candidate
                break
        if result is None:
            if outcome.exit_code:
                raise TransportUnavailable('transport failure')
            return _error(502, 'cli_output')
        if result.get('is_error') or result.get('subtype') != 'success':
            code = _failure_code(result)
            status = _FAILURE_STATUS.get(code, 502)
            return _error(status, 'context_length_exceeded' if code == 'context_overflow' else code)

        structured = result.get('structured_output')
        content = json.dumps(structured) if structured is not None else result.get('result')
        if not isinstance(content, str):
            return _error(502, 'cli_output')
        usage = result.get('usage') if isinstance(result.get('usage'), dict) else {}
        counts = [usage.get(k) for k in ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')]
        prompt_tokens = sum(c for c in counts if type(c) is int) if any(type(c) is int for c in counts) else None
        payload = {'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}],
                   'usage': {'prompt_tokens': prompt_tokens, 'completion_tokens': usage.get('output_tokens')}}
        return TransportResponse(200, {}, json.dumps(payload).encode())
