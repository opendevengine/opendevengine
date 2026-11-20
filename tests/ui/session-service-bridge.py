"""UI bridge over real SQLite, FeatureAPI, profile and session services.

GitHub transport, Docker runner and model adapter are explicit synthetic doubles.
This proves service integration, never real inference, containment or publication.
JSON-lines mode supports node tests; --serve PORT exposes an isolated browser QA app.
"""
import json
from pathlib import Path
import sys
import threading
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_sessions_fixture import SessionCase, FakeRunner, ScriptedAgent
from opendevengine.api import FeatureAPI, error_status
from opendevengine.ask import ConversationService, RoutingService
from opendevengine.execution.profiles import ExecutionProfileService, _VALIDATION
from opendevengine.platform import Worker
from opendevengine.platform.errors import PlatformError
from opendevengine.sessions import SessionService


class SyntheticAgent(ScriptedAgent):
    def preflight(self):
        return {**super().preflight(), 'capabilities': dict(self.capabilities)}


class Bridge:
    def __init__(self):
        self.case = SessionCase()
        self.case.setUp()
        c = self.case
        c.sessions.close()
        self.profiles = ExecutionProfileService(c.platform, c.providers,
            runner_factory=lambda iid: FakeRunner(iid, c.world), adapter_factory=lambda: SyntheticAgent(c.world))
        self.profiles.delete('prof_1')
        self.sessions = SessionService(c.platform, c.index, c.github, c.providers, c.plans, self.profiles,
            runner_factory=lambda iid: FakeRunner(iid, c.world), adapter_factory=lambda: SyntheticAgent(c.world),
            lease_ms=30000, poll_interval=0.05)
        c.plans.execution_preflight = self.sessions.preflight
        routing = RoutingService(c.store, c.index)
        conversations = ConversationService(c.store, c.jobs, c.index, c.providers, routing, c.plans)
        self.api = FeatureAPI(c.platform, c.github, c.providers, indexing=c.index,
            conversations=conversations, plans=c.plans, routing=routing,
            execution_profiles=self.profiles, sessions=self.sessions)
        self.worker = Worker(c.jobs, {'execution.test':self.profiles.run_test,'session.run':self.sessions.run_session},
                             worker_id='ui-synthetic-runtime', lease_ms=30000)
        self.seed = None
        self.lock = threading.RLock()

    def bootstrap(self):
        if self.seed:
            return self.seed
        c = self.case
        profile = self.profiles.create({'name':'Synthetic coding profile','adapter':'claude_code',
            'auth_mode':'cli_login','image':'python:3.12-slim','allow_external_source':True})
        c.world.script = [('write_file', {'path':'calc.sh','content':'#!/bin/sh\nprintf "%s\\n" "$(($1 + $2))"\n'})]
        c.world.commands[_VALIDATION] = lambda files: {'exit_code':0 if b'+ $2' in files.get('calc.sh', b'') else 1,
            'stdout':'synthetic arithmetic validation', 'stderr':'','timed_out':False,'truncated':False}
        self.profiles.request_test(profile['id'])
        self.worker.run_once()
        profile = self.profiles.get(profile['id'])
        if profile['check']['status'] != 'passed':
            raise AssertionError('synthetic check failed: ' + str(profile['check']))
        content = c.content(execution_context={'execution_profile_id':profile['id']})
        plan = c.plans.create(None,content)
        self.seed = {'plan':plan,'profile':profile,'evidence':'real services; synthetic runner, adapter and GitHub transport'}
        return self.seed

    def call(self, method, path, query='', body=None):
        with self.lock:
            try:
                if path == '/test/seed':
                    value = self.bootstrap()
                elif path == '/test/run':
                    c = self.case
                    c.world.script = [('say','Synthetic integration fixture edited calc.py'),
                        ('write_file',{'path':'calc.py','content':'def add(a, b):\n    return a + b\n'})]
                    c.world.commands['python test_calc.py'] = lambda files: {'exit_code':0 if b'a + b' in files.get('calc.py',b'') else 1,
                        'stdout':'synthetic validation passed','stderr':'','timed_out':False,'truncated':False}
                    value = {'ran':self.worker.run_once()}
                elif path == '/test/revoke':
                    self.case.github.update_selection(self.case.repo,index_enabled=True,implementation_enabled=False)
                    value = {'revoked':True}
                elif path == '/test/restore':
                    self.case.github.update_selection(self.case.repo,index_enabled=True,implementation_enabled=True)
                    value = {'restored':True}
                elif path == '/api/repos':
                    value = {'repos':[],'model':None}
                else:
                    response = self.api.dispatch(method,path,query,body)
                    return {'status':response.status,'body':response.body} if response else {'status':404,'body':{'code':'not_found'}}
                return {'status':200,'body':value}
            except PlatformError as e:
                return {'status':getattr(e,'http_status',error_status(e.code)),'body':{'code':e.code}}
            except ValueError:
                return {'status':400,'body':{'code':'validation'}}

    def close(self):
        self.sessions.close()
        self.case.doCleanups()


def main():
    bridge = Bridge()
    try:
        if '--serve' in sys.argv:
            from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
            port = int(sys.argv[sys.argv.index('--serve') + 1])
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.route()
                def do_POST(self):
                    self.route()
                def do_PATCH(self):
                    self.route()
                def do_DELETE(self):
                    self.route()
                def log_message(self, *args):
                    pass
                def route(self):
                    u = urlsplit(self.path)
                    if u.path in ('/','/app.js','/ask.js','/context.js','/sessions.js','/styles.css'):
                        name = 'index.html' if u.path == '/' else u.path[1:]
                        data = (ROOT/'opendevengine'/'web'/name).read_bytes()
                        self.send_response(200)
                        self.send_header('Content-Type','text/html' if name.endswith('.html') else 'text/css' if name.endswith('.css') else 'application/javascript')
                    else:
                        raw = self.rfile.read(int(self.headers.get('Content-Length',0)))
                        result = bridge.call(self.command,u.path,u.query,json.loads(raw) if raw else None)
                        data = json.dumps(result['body']).encode()
                        self.send_response(result['status'])
                        self.send_header('Content-Type','application/json')
                    self.send_header('Cache-Control','no-store')
                    self.end_headers()
                    self.wfile.write(data)
            seed = bridge.bootstrap()
            print(json.dumps({'port':port,'seed':seed}),flush=True)
            ThreadingHTTPServer(('127.0.0.1',port),Handler).serve_forever()
        else:
            for line in sys.stdin:
                request = json.loads(line)
                response = bridge.call(request.get('method','GET'),request['path'],request.get('query',''),request.get('body'))
                print(json.dumps(response),flush=True)
    finally:
        bridge.close()


if __name__ == '__main__':
    main()
