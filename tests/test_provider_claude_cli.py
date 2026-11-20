"""claude_cli provider adapter: a fake Claude Code binary stands in for the real CLI."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from opendevengine.platform import migrations
from opendevengine.platform.db import Store
from opendevengine.providers import ProviderService
from opendevengine.providers.claude_cli import ClaudeCliTransport
from opendevengine.providers.errors import (DestinationNotAllowed, ProviderAuth, ProviderCancelled,
                                      ProviderRateLimited, ProviderValidationError, SourceSharingNotAllowed)

FAKE = r'''#!{python}
import json, os, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
mode = json.loads((here / 'mode.json').read_text())
args = sys.argv[1:]
if args[:2] == ['--setting-sources', '']:
    args = args[2:]
if args == ['auth', 'status']:
    print(json.dumps({{'loggedIn': mode['logged_in'], 'authMethod': 'claude.ai', 'apiProvider': 'firstParty',
                      'subscriptionType': 'max'}}))
    sys.exit(0)
if args == ['plugin', 'list', '--json']:
    print('[]'); sys.exit(0)
if args == ['--version']:
    print('9.9.9 (Claude Code)'); sys.exit(0)
stdin = sys.stdin.read()
system = Path(args[args.index('--system-prompt-file') + 1]).read_text()
with open(here / 'calls.jsonl', 'a') as log:
    log.write(json.dumps({{'argv': args, 'stdin': stdin, 'system': system, 'pid': os.getpid(),
                          'env': sorted(os.environ)}}) + '\n')
if mode['run'] == 'sleep':
    time.sleep(60)
if mode['run'] == 'rate':
    print(json.dumps({{'type': 'result', 'subtype': 'success', 'is_error': True,
                      'result': "You've hit your limit"}})); sys.exit(1)
structured = {{'ok': True}} if '--json-schema' in args else None
print(json.dumps({{'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'ok',
                  'structured_output': structured, 'usage': {{'input_tokens': 3, 'cache_read_input_tokens': 7,
                  'output_tokens': 5}}, 'total_cost_usd': 0.01}}))
'''

SPEC = {'name': 'Claude subscription', 'adapter': 'claude_cli', 'endpoint': 'https://api.anthropic.com',
        'model': 'sonnet', 'config': {'capabilities': ['chat', 'structured_output'], 'context_limit': 32768,
                                      'max_output_tokens': 8192, 'timeout_seconds': 30,
                                      'allow_external_source': True}}
SCHEMA = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok'],
          'additionalProperties': False}
MESSAGES = [{'role': 'system', 'content': 'SYSTEM-MARKER instructions'},
            {'role': 'user', 'content': 'SOURCE-MARKER def charge(): pass'}]


class ClaudeCliProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.bin = root / 'bin'
        self.bin.mkdir()
        self.fake = self.bin / 'claude'
        self.fake.write_text(FAKE.format(python=sys.executable))
        self.fake.chmod(0o755)
        self.mode({'logged_in': True, 'run': 'ok'})
        policy = patch('opendevengine.execution.claude.ClaudeAdapter._check_policy', staticmethod(lambda: None))
        policy.start()
        self.addCleanup(policy.stop)
        self.store = Store.open(root / 'data')
        self.service = ProviderService(self.store, None, cli_transport=ClaudeCliTransport(claude_binary=str(self.fake)))
        self.service.update_settings(local_only=False)
        self.profile = self.service.create_profile(SPEC)
        self.service.assign_role('planning', self.profile['id'])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def mode(self, value):
        (self.bin / 'mode.json').write_text(json.dumps(value))

    def calls(self):
        path = self.bin / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_structured_plan_call_runs_without_tools_and_keeps_prompt_off_argv(self):
        result = self.service.generate('planning', MESSAGES, schema=SCHEMA)
        self.assertEqual(result['json'], {'ok': True})
        self.assertEqual(result['destination'], {'kind': 'remote', 'host': 'api.anthropic.com'})
        self.assertEqual((result['usage']['input_tokens'], result['usage']['output_tokens']), (10, 5))
        call = self.calls()[-1]
        argv = ' '.join(call['argv'])
        self.assertNotIn('SOURCE-MARKER', argv)
        self.assertNotIn('SYSTEM-MARKER', argv)
        self.assertEqual(call['stdin'], MESSAGES[1]['content'])
        self.assertEqual(call['system'], MESSAGES[0]['content'])
        for flag in ('--restricted', '--strict-mcp-config', '--no-session-persistence', '--disable-slash-commands'):
            self.assertIn(flag, call['argv'])
        self.assertEqual(call['argv'][call['argv'].index('--tools') + 1], '')
        self.assertEqual(json.loads(call['argv'][call['argv'].index('--mcp-config') + 1]), {'mcpServers': {}})
        self.assertEqual(call['argv'][call['argv'].index('--model') + 1], 'sonnet')
        self.assertEqual(json.loads(call['argv'][call['argv'].index('--json-schema') + 1]), SCHEMA)
        self.assertFalse(any(name.startswith(('ANTHROPIC_API', 'OPENAI')) for name in call['env']))

    def test_a_completed_call_does_not_mark_the_callers_event_cancelled(self):
        event = threading.Event()
        self.service.generate('planning', MESSAGES, schema=SCHEMA, cancel_event=event)
        self.assertFalse(event.is_set())
        self.service.generate('planning', MESSAGES, schema=SCHEMA, cancel_event=event)  # a repair attempt reuses it
        self.assertEqual(len(self.calls()), 2)

    def test_cancel_stops_the_cli_process_group(self):
        self.mode({'logged_in': True, 'run': 'sleep'})
        event = threading.Event()
        def cancel_once_running():
            # Cancel only after the fake CLI recorded its start; a fixed delay races slow process startup.
            deadline = time.monotonic() + 10
            while not self.calls() and time.monotonic() < deadline:
                time.sleep(0.05)
            event.set()
        threading.Thread(target=cancel_once_running, daemon=True).start()
        started = time.monotonic()
        with self.assertRaises(ProviderCancelled):
            self.service.generate('planning', MESSAGES, cancel_event=event)
        self.assertLess(time.monotonic() - started, 10)
        pid = self.calls()[-1]['pid']
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_connection_test_verifies_chat_and_structured_output(self):
        result = self.service.test_profile(self.profile['active_version_id'])
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['verified_capabilities'], ['chat', 'structured_output'])

    def test_logged_out_cli_is_an_auth_failure(self):
        self.mode({'logged_in': False, 'run': 'ok'})
        with self.assertRaises(ProviderAuth):
            self.service.generate('planning', MESSAGES)
        self.assertEqual(self.calls(), [])

    def test_subscription_limit_is_rate_limited(self):
        self.mode({'logged_in': True, 'run': 'rate'})
        with self.assertRaises(ProviderRateLimited):
            self.service.generate('planning', MESSAGES)

    def test_profile_rules(self):
        for change in ({'endpoint': 'https://api.example.com'}, {'model': '--dangerously-skip-permissions'},
                       {'config': {**SPEC['config'], 'capabilities': ['embeddings'], 'embedding_dimensions': 8}}):
            with self.subTest(change=change), self.assertRaises(ProviderValidationError):
                self.service.update_profile(self.profile['id'], change)
        with self.assertRaises(ProviderValidationError):
            self.service.create_profile({**SPEC, 'name': 'with key'}, secret_value='sk-not-used')
        self.service.update_profile(self.profile['id'], {'model': 'claude-opus-5-5'})

    def test_local_only_and_source_consent_apply(self):
        self.service.update_profile(self.profile['id'], {'config': {**SPEC['config'], 'allow_external_source': False}})
        with self.assertRaises(SourceSharingNotAllowed):
            self.service.generate('planning', MESSAGES)
        self.service.update_settings(local_only=True)
        with self.assertRaises(DestinationNotAllowed):
            self.service.assign_role('ask', self.profile['id'])
        self.assertEqual(self.calls(), [])


class ClaudeCliMigrationTests(unittest.TestCase):
    def test_v11_profiles_survive_the_adapter_table_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            with patch.object(migrations, 'MIGRATIONS', migrations.MIGRATIONS[:11]), \
                    patch.object(migrations, 'LATEST_VERSION', 11):
                store = Store.open(data)
                service = ProviderService(store, None)
                old = service.create_profile({'name': 'local', 'adapter': 'ollama', 'endpoint': 'http://127.0.0.1:11434',
                                              'model': 'llama3.2', 'config': {'capabilities': ['chat']}})
                service.assign_role('ask', old['id'])
                self.assertEqual(store.schema_version(), 11)
                store.close()
            store = Store.open(data)
            try:
                self.assertEqual(store.schema_version(), migrations.LATEST_VERSION)
                service = ProviderService(store, None)
                self.assertEqual(service.roles()['ask']['profile_id'], old['id'])
                with store.read() as conn:
                    self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
                    self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
                service.update_settings(local_only=False)
                service.create_profile(SPEC)
                self.assertTrue((data / 'backups' / 'opendevengine.pre-v12.sqlite3').exists())
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
