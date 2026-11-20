"""Real socket regressions for cancellation of nonstreaming provider responses."""
import contextlib
import os
import socket
import ssl
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from opendevengine.providers.transport import (
    ResponseTooLarge, TransportCancelled, TransportRequest, TransportTimeout,
    TransportUnavailable, UrllibTransport,
)


class Peer:
    """One local provider: read a request, run a response scenario, observe EOF."""

    def __init__(self, respond, *, tls_context=None, hold_handshake=False):
        self.respond = respond
        self.tls_context = tls_context
        self.hold_handshake = hold_handshake
        self.received = threading.Event()
        self.waiting = threading.Event()
        self.closed = threading.Event()
        self.stopped = threading.Event()
        self.release = threading.Event()
        self.errors = []
        self.requests = []
        self.connections = 0
        self.active = None
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen()
        self.listener.settimeout(0.05)
        self.thread = threading.Thread(target=self._serve, name='test-provider-peer')

    @property
    def url(self):
        scheme = 'https' if self.tls_context else 'http'
        return f'{scheme}://127.0.0.1:{self.port}/api/chat'

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stopped.set()
        self.release.set()
        if self.active is not None:
            with contextlib.suppress(OSError):
                self.active.shutdown(socket.SHUT_RDWR)
        self.listener.close()
        self.thread.join(2)
        if self.thread.is_alive():
            raise AssertionError('test provider thread did not terminate')
        if self.errors and args[0] is None:
            raise self.errors[0]

    def _serve(self):
        try:
            while not self.stopped.is_set():
                try:
                    conn, _ = self.listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self.stopped.is_set():
                        return
                    raise
                self.connections += 1
                self.active = conn
                try:
                    conn.settimeout(2)
                    if self.tls_context:
                        if self.hold_handshake:
                            self.received.set()
                            self.wait_for_close(conn)
                            continue
                        try:
                            conn = self.tls_context.wrap_socket(conn, server_side=True)
                            self.active = conn
                        except ssl.SSLError:
                            self.closed.set()
                            continue
                    data = b''
                    while b'\r\n\r\n' not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    if b'\r\n\r\n' not in data:
                        self.closed.set()
                        continue
                    headers, body = data.split(b'\r\n\r\n', 1)
                    lengths = [line.split(b':', 1)[1].strip() for line in headers.split(b'\r\n')
                               if line.lower().startswith(b'content-length:')]
                    size = int(lengths[0]) if lengths else 0
                    while len(body) < size:
                        body += conn.recv(size - len(body))
                    self.requests.append(headers + b'\r\n\r\n' + body)
                    self.received.set()
                    self.respond(self, conn)
                finally:
                    conn.close()
                    self.active = None
        except BaseException as error:
            if not self.stopped.is_set():
                self.errors.append(error)

    def wait_for_close(self, conn):
        self.waiting.set()
        conn.settimeout(0.05)
        while not self.stopped.is_set():
            try:
                if not conn.recv(4096):
                    self.closed.set()
                    return
            except socket.timeout:
                continue
            except (ConnectionResetError, ssl.SSLError):
                self.closed.set()
                return


def held_headers(peer, conn):
    peer.wait_for_close(conn)


def held_body(peer, conn):
    conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nx')
    peer.wait_for_close(conn)


def reply(status=200, body=b'{}', extra=b''):
    def respond(peer, conn):
        conn.sendall(f'HTTP/1.1 {status} Test\r\nContent-Length: {len(body)}\r\n'.encode()
                     + extra + b'\r\n' + body)
        peer.wait_for_close(conn)
    return respond


def open_socket_count():
    count = 0
    for entry in os.listdir('/dev/fd'):
        try:
            count += stat.S_ISSOCK(os.fstat(int(entry)).st_mode)
        except OSError:
            pass  # The directory descriptor may already be closed.
    return count


class _TransportChecks(unittest.TestCase):
    def setUp(self):
        # macOS lazily opens process-wide mDNSResponder descriptors on its first
        # hostname lookup. Warm that stdlib resolver before measuring our sockets.
        socket.getaddrinfo('localhost', 80, type=socket.SOCK_STREAM)
        self.socket_count = open_socket_count()

    def tearDown(self):
        self.assert_no_transport_threads()
        self.assertEqual(self.socket_count, open_socket_count(), 'socket descriptor leaked')

    def request(self, peer, **kwargs):
        return TransportRequest('POST', kwargs.pop('url', peer.url),
                                {'Authorization': 'Bearer test-secret', 'Content-Type': 'application/json'},
                                b'{"source":"private fixture"}', **kwargs)

    def assert_no_transport_threads(self):
        self.assertFalse([t for t in threading.enumerate() if t.name == 'provider-transport-watch'])

    def assert_cancel(self, respond, *, tls_context=None, hold_handshake=False):
        trusted = ssl.create_default_context(cafile=str(self.cert)) if tls_context else None
        transport = UrllibTransport(ssl_context=trusted)
        event = threading.Event()
        with Peer(respond, tls_context=tls_context, hold_handshake=hold_handshake) as peer:
            results = []
            def send():
                try:
                    url = peer.url.replace('127.0.0.1', 'localhost') if tls_context else peer.url
                    transport.send(self.request(peer, url=url, timeout=30), cancel_event=event)
                except BaseException as error:
                    results.append(error)
            thread = threading.Thread(target=send, name='test-provider-call')
            thread.start()
            try:
                self.assertTrue(peer.received.wait(2), 'provider did not receive request')
                self.assertTrue(peer.waiting.wait(2), 'provider did not reach held response phase')
                # Let the caller consume the available headers/body prefix before
                # cancelling a response whose remaining bytes will never arrive.
                peer.release.wait(0.05)
                started = time.monotonic()
                event.set()
                thread.join(1.5)
                elapsed = time.monotonic() - started
                self.assertFalse(thread.is_alive(), 'cancel blocked on provider response')
                self.assertLess(elapsed, 2)
                self.assertEqual(1, len(results))
                self.assertIsInstance(results[0], TransportCancelled)
                self.assertEqual('transport failure', str(results[0]))
                self.assertIsNone(results[0].__context__)
                self.assertTrue(peer.closed.wait(1), 'provider did not observe connection close')
                self.assertEqual(1, peer.connections)
                self.assertEqual(0 if hold_handshake else 1, len(peer.requests))
                self.assert_no_transport_threads()
            finally:
                # Cleanup even when testing the unfixed implementation, whose send
                # stays blocked until the peer finally closes its connection.
                if thread.is_alive() and peer.active is not None:
                    with contextlib.suppress(OSError):
                        peer.active.shutdown(socket.SHUT_RDWR)
                thread.join(2)

class TransportNetworkTests(_TransportChecks):
    def test_cancel_while_nonstreaming_response_headers_withheld(self):
        self.assert_cancel(held_headers)

    def test_cancel_while_response_body_blocked(self):
        self.assert_cancel(held_body)

    def test_already_cancelled_sends_nothing(self):
        event = threading.Event()
        event.set()
        with Peer(reply()) as peer:
            with self.assertRaises(TransportCancelled):
                UrllibTransport().send(self.request(peer), cancel_event=event)
            self.assertEqual(0, peer.connections)
            self.assertEqual([], peer.requests)
        self.assert_no_transport_threads()

    def assert_deadline(self, respond):
        with Peer(respond) as peer:
            started = time.monotonic()
            with self.assertRaises(TransportTimeout) as result:
                UrllibTransport().send(self.request(peer, timeout=0.35))
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertIsNone(result.exception.__context__)
            self.assertTrue(peer.closed.wait(1))
            self.assertEqual(1, peer.connections)
        self.assert_no_transport_threads()

    def test_headers_deadline(self):
        self.assert_deadline(held_headers)

    def test_body_deadline(self):
        self.assert_deadline(held_body)

    def test_trickling_headers_cannot_extend_overall_deadline(self):
        def trickle(peer, conn):
            for byte in b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}':
                try:
                    conn.sendall(bytes([byte]))
                except (BrokenPipeError, ConnectionResetError):
                    break
                if peer.stopped.wait(0.05):
                    return
            peer.wait_for_close(conn)
        self.assert_deadline(trickle)

    def test_trickling_body_cannot_extend_overall_deadline(self):
        def trickle(peer, conn):
            conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n')
            for _ in range(100):
                try:
                    conn.sendall(b'x')
                except (BrokenPipeError, ConnectionResetError):
                    break
                if peer.stopped.wait(0.05):
                    return
            peer.wait_for_close(conn)
        self.assert_deadline(trickle)

    def test_redirect_is_returned_without_forwarding_credentials(self):
        with Peer(reply()) as target:
            with Peer(reply(307, b'redirect', f'Location: {target.url}\r\n'.encode())) as peer:
                response = UrllibTransport().send(self.request(peer))
                self.assertEqual(307, response.status)
                self.assertEqual(b'redirect', response.body)
                self.assertTrue(peer.closed.wait(1))
                self.assertEqual(1, peer.connections)
                self.assertEqual(0, target.connections)
                self.assertEqual([], target.requests)
        self.assert_no_transport_threads()

    def test_sends_product_user_agent_not_urllib_default(self):
        with Peer(reply()) as peer:
            UrllibTransport().send(self.request(peer))
            self.assertTrue(peer.closed.wait(1))
            head = peer.requests[0].split(b'\r\n\r\n', 1)[0].lower()
            self.assertIn(b'\r\nuser-agent: opendevengine\r\n', head + b'\r\n')
            self.assertNotIn(b'python-urllib', head)
        self.assert_no_transport_threads()

    def test_environment_proxies_are_ignored(self):
        with Peer(reply(body=b'origin')) as origin, Peer(reply(body=b'proxy')) as proxy:
            env = {name: proxy.url for name in ('http_proxy', 'HTTP_PROXY', 'https_proxy',
                                               'HTTPS_PROXY', 'ALL_PROXY', 'all_proxy')}
            env.update({'NO_PROXY': '', 'no_proxy': ''})
            with patch.dict('os.environ', env):
                response = UrllibTransport().send(self.request(origin))
            self.assertEqual(b'origin', response.body)
            self.assertEqual(1, origin.connections)
            self.assertEqual(0, proxy.connections)
            self.assertEqual([], proxy.requests)
        self.assert_no_transport_threads()

    def test_response_byte_cap_and_exact_limit(self):
        for size, limit, expected in ((5, 4, ResponseTooLarge), (4, 4, None)):
            with self.subTest(size=size), Peer(reply(body=b'x' * size)) as peer:
                if expected:
                    with self.assertRaises(expected):
                        UrllibTransport().send(self.request(peer, max_response_bytes=limit))
                else:
                    result = UrllibTransport().send(self.request(peer, max_response_bytes=limit))
                    self.assertEqual(b'x' * size, result.body)
                self.assertTrue(peer.closed.wait(1))
                self.assertEqual(1, peer.connections)
        self.assert_no_transport_threads()

    def test_disconnect_is_unavailable_redacted_and_never_retried(self):
        def disconnect(peer, conn):
            conn.shutdown(socket.SHUT_RDWR)
        with Peer(disconnect) as peer:
            with self.assertRaises(TransportUnavailable) as result:
                UrllibTransport().send(self.request(peer))
            self.assertEqual('transport failure', str(result.exception))
            self.assertIsNone(result.exception.__context__)
            self.assertIsNone(result.exception.__cause__)
            self.assertEqual(1, peer.connections)
            self.assertEqual(1, len(peer.requests))
        self.assert_no_transport_threads()

    def test_shared_transport_keeps_concurrent_cancellation_isolated(self):
        transport = UrllibTransport()
        event = threading.Event()
        results = []
        with Peer(held_headers) as blocked, Peer(reply(body=b'independent')) as independent:
            def send():
                try:
                    transport.send(self.request(blocked), cancel_event=event)
                except BaseException as error:
                    results.append(error)
            thread = threading.Thread(target=send, name='test-provider-call')
            thread.start()
            try:
                self.assertTrue(blocked.received.wait(2))
                result = transport.send(self.request(independent))
                self.assertEqual(b'independent', result.body)
                event.set()
                thread.join(1.5)
                self.assertFalse(thread.is_alive())
                self.assertIsInstance(results[0], TransportCancelled)
                self.assertTrue(blocked.closed.wait(1))
                self.assert_no_transport_threads()
            finally:
                if thread.is_alive() and blocked.active is not None:
                    with contextlib.suppress(OSError):
                        blocked.active.shutdown(socket.SHUT_RDWR)
                thread.join(2)


class TransportTLSTests(_TransportChecks):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.cert = Path(cls.temp.name) / 'cert.pem'
        key = Path(cls.temp.name) / 'key.pem'
        try:
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                            '-keyout', str(key), '-out', str(cls.cert), '-days', '1',
                            '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
            cls.tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            cls.tls_context.load_cert_chain(str(cls.cert), str(key))
        except (FileNotFoundError, subprocess.SubprocessError):
            cls.temp.cleanup()
            raise unittest.SkipTest('openssl with -addext is required for real TLS checks')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_default_tls_rejects_untrusted_certificate(self):
        with Peer(reply(), tls_context=self.tls_context) as peer:
            with self.assertRaises(TransportUnavailable) as result:
                UrllibTransport().send(self.request(peer))
            self.assertEqual('transport failure', str(result.exception))
            self.assertIsNone(result.exception.__context__)
            self.assertTrue(peer.closed.wait(1))
            self.assertEqual([], peer.requests)
            self.assertEqual(1, peer.connections)
        self.assert_no_transport_threads()

    def test_injected_trust_context_still_verifies_hostname(self):
        trusted = ssl.create_default_context(cafile=str(self.cert))
        # The trusted certificate covers localhost, but not 127.0.0.1.
        with Peer(reply(), tls_context=self.tls_context) as peer:
            with self.assertRaises(TransportUnavailable):
                UrllibTransport(ssl_context=trusted).send(self.request(peer))
            self.assertEqual([], peer.requests)
        with Peer(reply(body=b'tls'), tls_context=self.tls_context) as peer:
            request = TransportRequest('POST', peer.url.replace('127.0.0.1', 'localhost'), {}, b'{}')
            result = UrllibTransport(ssl_context=trusted).send(request)
            self.assertEqual(b'tls', result.body)
            self.assertTrue(peer.closed.wait(1))
        self.assert_no_transport_threads()

    def test_cancel_tls_header_wait(self):
        self.assert_cancel(held_headers, tls_context=self.tls_context)

    def test_cancel_tls_body_wait(self):
        self.assert_cancel(held_body, tls_context=self.tls_context)

    def test_cancel_tls_handshake_wait(self):
        self.assert_cancel(held_headers, tls_context=self.tls_context, hold_handshake=True)


if __name__ == '__main__':
    unittest.main()
