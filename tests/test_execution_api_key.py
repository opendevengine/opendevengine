"""Claude Code runs with an Anthropic API key only in api_key mode, passed to the CLI process only."""
import json
import os
import threading
import unittest
from unittest.mock import patch

from opendevengine.execution.adapter_process import ProcessOutcome
from opendevengine.execution.claude import AdapterError, ClaudeAdapter
import test_execution_adapter
from test_sessions_fixture import SessionCase

KEY_STATUS = '{"loggedIn":true,"authMethod":"api_key","apiProvider":"firstParty","apiKeySource":"ANTHROPIC_API_KEY"}'


class ApiKeyAdapterTests(unittest.TestCase):
    setUp, script, init, mark_ready = test_execution_adapter.AdapterTests.setUp, test_execution_adapter.AdapterTests.script, test_execution_adapter.AdapterTests.init, test_execution_adapter.AdapterTests.mark_ready

    def run_key(self, auth_mode, api_key, events=None):
        seen = self.root / 'seen'
        # Answer auth status by what the CLI actually reports; record the key every process saw.
        before = ('key=os.environ.get("ANTHROPIC_API_KEY","")\n'
                  'open(' + repr(str(seen)) + ',"a").write(key+"\\n")\n'
                  'if sys.argv[-2:] == ["auth", "status"] and key:\n'
                  ' print(' + repr(KEY_STATUS) + ');sys.exit(0)\n')
        binary = self.script('import json\n' + '\n'.join('print(' + repr(json.dumps(e)) + ', flush=True)'
                                                       for e in (events or [])) + '\n', before=before)
        result = ClaudeAdapter(claude_binary=binary).run(
            self.endpoint, 'Synthetic fixture task', auth_mode=auth_mode, api_key=api_key, synthetic=True,
            emit=lambda k, v: self.events.append((k, v)), on_inventory_verified=self.mark_ready,
            stop_event=threading.Event())
        return result, seen.read_text().splitlines() if seen.exists() else []

    def test_api_key_mode_sets_env_only_for_cli(self):
        result, seen = self.run_key('api_key', 'sk-ant-fixture',
                                    [self.init(), {'type': 'result', 'subtype': 'success'}])
        self.assertEqual(result['status'], 'completed')
        # auth status and the agent get the key; the plugin-list query needs no credential.
        self.assertEqual(seen, ['sk-ant-fixture', '', 'sk-ant-fixture'])
        self.assertNotIn('ANTHROPIC_API_KEY', os.environ)
        self.assertNotIn('sk-ant-fixture', json.dumps(self.events) + json.dumps(result))

    def test_api_key_mode_requires_key(self):
        with self.assertRaisesRegex(AdapterError, 'unsupported_auth_mode'):
            self.run_key('api_key', None)

    def test_cli_login_rejects_key(self):
        with self.assertRaisesRegex(AdapterError, 'unsupported_auth_mode'):
            self.run_key('cli_login', 'sk-ant-fixture')

    def test_cli_login_ignores_ambient_key(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'sk-ambient'}):
            result, seen = self.run_key('cli_login', None, [self.init(), {'type': 'result', 'subtype': 'success'}])
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(seen and all(line == '' for line in seen), seen)

    def status(self, profile):
        def fake(*args, **kwargs):
            kwargs['on_line'](json.dumps(profile).encode())
            return ProcessOutcome(0, 'exited', True, 120)
        return patch('opendevengine.execution.claude.run_stream', side_effect=fake)

    def test_api_key_mode_rejects_third_party_provider(self):
        adapter = ClaudeAdapter(claude_binary=self.script(''))
        with self.status({**json.loads(KEY_STATUS), 'apiProvider': 'bedrock'}):
            with self.assertRaisesRegex(AdapterError, 'unsupported_auth_profile'):
                adapter._check_auth_profile(auth_mode='api_key', api_key='k')

    def test_api_key_mode_accepts_first_party_key(self):
        adapter = ClaudeAdapter(claude_binary=self.script(''))
        with self.status(json.loads(KEY_STATUS)):
            self.assertEqual(adapter._check_auth_profile(auth_mode='api_key', api_key='k')['method'], 'api_key')

    def test_cli_login_rejects_key_profile(self):
        adapter = ClaudeAdapter(claude_binary=self.script(''))
        with self.status(json.loads(KEY_STATUS)):
            with self.assertRaisesRegex(AdapterError, 'unsupported_auth_profile'):
                adapter._check_auth_profile()


class SessionApiKeyTests(SessionCase):
    def test_session_launch_resolves_execution_key(self):
        self.profiles.versions['epv_1']['auth_mode'] = 'api_key'
        self.world.script = [('write_file', {'path': 'calc.py', 'content': 'def add(a, b):\n    return a + b\n'})]
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-api-key')['session_id']
        final = self.wait_state(sid)
        run = self.world.runs[-1]
        self.assertEqual((run['auth_mode'], run['api_key']), ('api_key', 'sk-fixture-coding-key'), final)
        with self.store.read() as db:
            dump = json.dumps([list(r) for r in db.execute('SELECT * FROM session_events')] +
                              [list(r) for r in db.execute('SELECT instructions_json, outcome_json FROM sessions')])
        self.assertNotIn('sk-fixture-coding-key', dump)
