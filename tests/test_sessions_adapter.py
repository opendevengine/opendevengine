"""Real ClaudeAdapter/adapter_process/MCP bridge/broker path with a FAKE CLI executable.

The fake CLI speaks MCP stdio to the real bridge and prints stream-json; it proves the
session<->adapter protocol, inventory gate and process-group cancellation only. It is
not model inference and is not a substitute for the root-authorized real Claude probe.
"""
import json
import os
from pathlib import Path
import sys
import time
import unittest

from opendevengine.execution.claude import AdapterError, ClaudeAdapter
from test_sessions_fixture import CLI_VERSION, FakeRunner, SessionCase

FAKE_CLI = r'''
import json, os, subprocess, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
args = sys.argv[1:]
while args[:1] == ["--setting-sources"]:  # CLI global metadata flags precede subcommands
    args = args[2:]
if args == ["--version"]:
    print(VERSION); sys.exit(0)
if args == ["auth", "status"]:
    print('{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty","subscriptionType":"pro"}'); sys.exit(0)
if args == ["plugin", "list", "--json"]:
    print("[]"); sys.exit(0)
mode = (here / "mode").read_text().strip()
prompt = sys.stdin.read()  # the adapter passes the prompt on private stdin, never argv
(here / "prompt_seen").write_text(str(len(prompt)) + ":" + str(any("Plan version" in a for a in sys.argv)))
config = json.loads(args[args.index("--mcp-config") + 1])
server = config["mcpServers"]["opendevengine"]
env = dict(os.environ); env.update(server.get("env", {}))
bridge = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
counter = [0]
def rpc(method, params):
    counter[0] += 1
    bridge.stdin.write((json.dumps({"jsonrpc": "2.0", "id": counter[0], "method": method, "params": params}) + "\n").encode())
    bridge.stdin.flush()
    return json.loads(bridge.stdout.readline())
results = []
rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fake", "version": "0"}})
bridge.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'); bridge.stdin.flush()
tools = rpc("tools/list", {})["result"]["tools"]
results.append(["pre_gate", rpc("tools/call", {"name": "read_file", "arguments": {"path": "calc.py"}})["result"]])
print(json.dumps({"type": "system", "subtype": "init", "tools": ["mcp__opendevengine__" + t["name"] for t in tools],
                  "mcp_servers": [{"name": "opendevengine", "status": "connected"}], "model": "fake-cli-no-inference"}), flush=True)
time.sleep(0.5)
if mode == "hang":
    (here / "results.json").write_text(json.dumps(results))
    time.sleep(300)
results.append(["read", rpc("tools/call", {"name": "read_file", "arguments": {"path": "calc.py"}})["result"]])
if mode == "followup":
    (here / "results.json").write_text(json.dumps(results))
    while not (here / "go").exists():
        time.sleep(0.05)
    listed = rpc("tools/call", {"name": "list_files", "arguments": {}})["result"]
    results.append(["list", listed])
    import re
    markers = re.findall(r"\[message:smsg_[0-9a-f]+:ack\]", listed["content"][0]["text"])
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Read the update. " + " ".join(markers)}]}}), flush=True)
results.append(["write", rpc("tools/call", {"name": "write_file", "arguments": {"path": "calc.py", "content": "def add(a, b):\n    return a + b\n"}})["result"]])
(here / "results.json").write_text(json.dumps(results))
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "PRIVATE-REASONING"},
      {"type": "text", "text": "Fixed add"}, {"type": "tool_use", "name": "mcp__opendevengine__write_file", "input": {"content": "PRIVATE-INPUT"}}]}}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "result": "done", "usage": {"input_tokens": 3, "output_tokens": 2},
                  "total_cost_usd": 0.0}), flush=True)
bridge.stdin.close(); bridge.wait(5)
'''


class RealAdapterProtocolTests(SessionCase):
    def setUp(self):
        super().setUp()
        self.bin = Path(self.tmp.name) / 'fakecli'
        self.bin.mkdir(mode=0o700)
        cli = self.bin / 'claude'
        cli.write_text('#!' + sys.executable + '\nVERSION = ' + repr(CLI_VERSION) + '\n' + FAKE_CLI)
        cli.chmod(0o700)
        self.cli = str(cli)
        try:
            ClaudeAdapter(claude_binary=self.cli).preflight()
        except AdapterError as exc:
            self.skipTest('host CLI policy prevents the adapter boundary fixture: ' + exc.code)
        self.sessions = self.make_service_real()

    def make_service_real(self):
        from opendevengine.sessions import SessionService
        service = SessionService(self.platform, self.index, self.github, self.providers, self.plans, self.profiles,
                                 runner_factory=lambda iid: FakeRunner(iid, self.world),
                                 adapter_factory=lambda: ClaudeAdapter(claude_binary=self.cli),
                                 lease_ms=self.lease_ms, poll_interval=0.05)
        self.addCleanup(service.close)
        self.plans.execution_preflight = service.preflight
        return service

    def results(self):
        path = self.bin / 'results.json'
        return json.loads(path.read_text()) if path.exists() else None

    def test_real_adapter_inventory_gate_tools_and_completion(self):
        (self.bin / 'mode').write_text('code')
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-real-adapter')['session_id']
        final = self.wait_state(sid, timeout=40)
        self.assertEqual(final['state'], 'completed', final)
        results = dict((k, v) for k, v in self.results())
        self.assertTrue(results['pre_gate']['isError'])
        self.assertIn('policy_denied', results['pre_gate']['content'][0]['text'])
        self.assertIn('return a - b', results['read']['content'][0]['text'])
        self.assertFalse(results['write']['isError'])
        self.assertIn('+    return a + b', self.sessions.diff(sid)['files'][0]['diff'])
        events = self.all_events(sid)
        blob = json.dumps(events)
        self.assertNotIn('PRIVATE-REASONING', blob)
        self.assertNotIn('PRIVATE-INPUT', blob)
        self.assertIn('fake-cli-no-inference', blob)
        self.assertEqual(final['usage']['tokens']['input_tokens'], 3)
        with self.store.read() as db:
            runner = db.execute('SELECT harness_pid,harness_start FROM session_runners').fetchone()
        self.assertTrue(runner['harness_pid'] and runner['harness_start'])
        with self.assertRaises(ProcessLookupError):
            os.kill(runner['harness_pid'], 0)

    def test_real_adapter_mcp_follow_up_dispatch_and_observed_ack(self):
        (self.bin / 'mode').write_text('followup')
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-real-follow')['session_id']
        self.wait(self.results, timeout=40, message='fake CLI read')
        message = self.sessions.send_message(sid, 'Keep the public signature unchanged', 'c-real-follow')
        (self.bin / 'go').write_text('1')
        final = self.wait_state(sid, timeout=40)
        self.assertEqual(final['state'], 'completed', final)
        listed = dict((k, v) for k, v in self.results())['list']
        self.assertIn('Keep the public signature unchanged', listed['content'][0]['text'])
        with self.store.read() as db:
            row = db.execute('SELECT delivery,reason FROM session_messages WHERE id=?', (message['id'],)).fetchone()
        self.assertEqual((row['delivery'], row['reason']), ('acknowledged', 'model_marker_observed'))
        texts = [e['payload'].get('text', '') for e in self.all_events(sid) if e['type'] == 'agent.summary']
        self.assertIn('Read the update.', texts)
        self.assertFalse([t for t in texts if '[message:' in t])
        seen = (self.bin / 'prompt_seen').read_text()
        self.assertTrue(int(seen.split(':')[0]) > 0 and seen.endswith('False'))  # prompt on stdin, not argv

    def test_real_adapter_process_group_cancellation_timing(self):
        (self.bin / 'mode').write_text('hang')
        _, version = self.ready_plan()
        self.sessions.start()
        sid = self.sessions.launch(version['id'], version['content_hash'], 'k-real-cancel')['session_id']
        self.wait(self.results, timeout=40, message='fake CLI ready')
        self.wait(lambda: self.sessions.get(sid)['state'] == 'running', message='running')
        started = time.monotonic()
        self.assertEqual(self.sessions.stop(sid)['state'], 'stopping')
        self.assertLess(time.monotonic() - started, 2.0)
        final = self.wait_state(sid, timeout=15)
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertEqual((final['state'], final['runner']['containment']), ('cancelled', 'confirmed'))
        self.assertEqual(final['outcome']['adapter']['status'], 'cancelled')
        with self.store.read() as db:
            pid = db.execute('SELECT harness_pid FROM session_runners').fetchone()[0]
        with self.assertRaises(ProcessLookupError):
            os.killpg(pid, 0)


if __name__ == '__main__':
    unittest.main()
