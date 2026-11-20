"""Exercise actual loopback HTTP dispatch and durable metadata across restarts."""
import http.client
from pathlib import Path
import json
from tempfile import TemporaryDirectory
from threading import Thread
from http.server import ThreadingHTTPServer
import unittest
import os
import select
import signal
import socket
import subprocess
import sys

from opendevengine.__main__ import RequestHandler, ROOT
from opendevengine.index import CodeIndex, load_demos
from opendevengine.platform import Platform


class FoundationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.start(None)
        self.addCleanup(self.stop)

    def start(self, data_dir):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), RequestHandler)
        self.server.index = CodeIndex(load_demos(ROOT / 'examples'))
        self.server.model = None
        self.server.platform = Platform.open(data_dir)
        self.thread = Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01})
        self.thread.start()

    def stop(self):
        if getattr(self, 'server', None) is None:
            return
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()
        self.server.platform.close()
        self.server.index.db.close()
        self.server = None

    def request(self, path, *, method='GET', body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            data = json.loads(response.read())
            return response.status, data, dict(response.getheaders())
        finally:
            conn.close()

    def test_summary_health_and_legacy_routes_are_real(self):
        status, summary, headers = self.request('/api/workspace')
        self.assertEqual(status, 200)
        self.assertEqual(summary['mode'], 'demo')
        self.assertEqual(summary['catalog']['repository_count'], 4)
        self.assertFalse(summary['storage']['persistent'])
        self.assertEqual(summary['secrets']['available'], False)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("form-action 'self' https://github.com", headers['Content-Security-Policy'])
        status, health, _ = self.request('/api/health')
        self.assertEqual(status, 200)
        self.assertEqual(health['components']['jobs']['workers_running'], 0)
        self.assertFalse(health['components']['model']['probed'])
        status, repositories, _ = self.request('/api/repos')
        self.assertEqual(status, 200)
        self.assertEqual(set(repositories), {'repos', 'model'})
        status, answer, _ = self.request('/api/ask', method='POST', body=json.dumps({
            'question': 'Trace checkout from the storefront through the orders API, database, and confirmation email.',
            'repos': [r['name'] for r in repositories['repos']],
        }), headers={'Content-Type': 'application/json'})
        self.assertEqual(status, 200)
        self.assertEqual({s['repo'] for s in answer['sources']}, {r['name'] for r in repositories['repos']})

    def test_status_routes_preserve_host_and_query_boundaries(self):
        for path in ['/api/workspace', '/api/health']:
            self.assertEqual(self.request(path, headers={'Host': 'attacker.invalid'})[0], 403)
            status, data, _ = self.request(path + '?unexpected=1')
            self.assertEqual(status, 400)
            self.assertEqual(data['code'], 'bad_request')
        status, data, _ = self.request('/api/no-such-route')
        self.assertEqual((status, set(data)), (404, {'error'}))
        status, _, _ = self.request('/api/ask', method='POST', body='{}', headers={
            'Origin': 'https://attacker.invalid', 'Content-Type': 'application/json'})
        self.assertEqual(status, 403)

    def test_durable_job_survives_server_restart(self):
        self.stop()
        directory = Path(self.directory.name)
        self.start(directory)
        job = self.server.platform.jobs.enqueue('test.restart', {'cursor': 7})
        self.stop()
        self.start(directory)
        self.assertEqual(self.server.platform.jobs.get(job.id).payload, {'cursor': 7})
        status, health, _ = self.request('/api/health')
        self.assertEqual(status, 200)
        self.assertEqual(health['components']['jobs']['counts']['queued'], 1)
        self.assertTrue(health['components']['storage']['persistent'])
        self.assertNotIn(self.directory.name, json.dumps(health))
        self.assertNotIn('cursor', json.dumps(health))


class LoopbackBindTests(unittest.TestCase):
    def test_server_binds_without_reverse_dns(self):
        from unittest.mock import patch
        from opendevengine.__main__ import LoopbackHTTPServer, RequestHandler
        with patch('socket.getfqdn', side_effect=AssertionError('reverse DNS lookup during bind')):
            server = LoopbackHTTPServer(('127.0.0.1', 0), RequestHandler)
        try:
            self.assertEqual(server.server_name, '127.0.0.1')
            self.assertEqual(server.server_port, server.server_address[1])
        finally:
            server.server_close()


class ApplicationLifecycleTests(unittest.TestCase):
    def start_process(self, directory, *args):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([sys.executable, '-m', 'opendevengine', '--port', str(port), *args],
                                   cwd=directory, env={**os.environ, 'PYTHONPATH': str(ROOT.parent),
                                                       'PYTHONDONTWRITEBYTECODE': '1'},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
        self.addCleanup(cleanup)
        return process, port

    def test_actual_cli_serves_then_sigterm_exits_cleanly_without_demo_files(self):
        with TemporaryDirectory() as directory:
            process, port = self.start_process(directory)
            readable, _, _ = select.select([process.stdout], [], [], 10)
            self.assertTrue(readable, 'CLI did not report readiness')
            self.assertIn('Open http://', process.stdout.readline())
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
            try:
                conn.request('GET', '/api/health')
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())['status'], 'ok')
            finally:
                conn.close()
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertNotIn('Traceback', stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_actual_cli_storage_failure_has_no_traceback_path_or_fallback(self):
        with TemporaryDirectory() as directory:
            invalid = Path(directory) / 'private-path.sqlite'
            invalid.write_text('not a directory')
            for value in [str(invalid), '~opendevengine_nonexistent_account_38192/data']:
                process, _ = self.start_process(directory, '--data-dir', value)
                stdout, stderr = process.communicate(timeout=10)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn('Could not open workspace storage.', stderr)
                self.assertNotIn('Traceback', stderr)
                self.assertNotIn(value, stderr)
                self.assertNotIn('Indexed', stdout)


if __name__ == '__main__':
    unittest.main()
