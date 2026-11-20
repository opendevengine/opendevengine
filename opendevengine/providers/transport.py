"""Bounded HTTP transport for model providers.

The production transport ignores proxy environment variables, keeps default TLS
verification, follows no redirects (a 3xx is returned as a response so the service can
reject it) and enforces a response-body byte limit plus an overall deadline. Tests inject
any object with ``send(request, *, cancel_event=None) -> TransportResponse``.
"""
from dataclasses import dataclass, field
import http.client
import socket
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import (HTTPHandler, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request,
                            build_opener)

USER_AGENT = 'OpenDevEngine'

@dataclass(frozen=True)
class TransportRequest:
    method: str
    url: str
    # Headers can carry Authorization; keep them out of repr/logs.
    headers: dict = field(repr=False)
    body: bytes | None = field(default=None, repr=False)
    timeout: float = 60.0
    max_response_bytes: int = 4 * 1024 * 1024


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: dict
    body: bytes = field(repr=False)


class TransportFailure(Exception):
    """Base for transport failures. Messages are fixed strings without request data."""


class TransportTimeout(TransportFailure):
    pass


class TransportUnavailable(TransportFailure):
    pass


class ResponseTooLarge(TransportFailure):
    pass


class TransportCancelled(TransportFailure):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_CHUNK = 64 * 1024


class _RequestControl:
    """Interrupt owned socket I/O without moving the request into a worker thread.

    urllib can close its socket handle while a response's makefile still owns the
    connection. A duplicate keeps shutdown available until all response I/O ends.
    The watcher only calls shutdown; the sending thread closes every handle and
    joins the watcher before returning, including on error.
    """

    def __init__(self, deadline, cancel_event):
        self.deadline = deadline
        self.cancel_event = cancel_event
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._socket = None
        self._failure = None
        self._watcher = threading.Thread(target=self._watch, name='provider-transport-watch',
                                         daemon=False)

    def start(self):
        self.check()
        self._watcher.start()

    def _outcome_locked(self):
        if self._failure is None:
            if self.cancel_event is not None and self.cancel_event.is_set():
                self._failure = TransportCancelled
            elif time.monotonic() >= self.deadline:
                self._failure = TransportTimeout
        return self._failure

    def outcome(self):
        with self._lock:
            return self._outcome_locked()

    def check(self):
        outcome = self.outcome()
        if outcome is not None:
            raise outcome('transport failure')

    def attach(self, sock):
        with self._lock:
            outcome = self._outcome_locked()
            if outcome is not None:
                raise outcome('transport failure')
            self._socket = sock.dup()
            sock.settimeout(max(0.001, self.deadline - time.monotonic()))

    def _watch(self):
        while not self._done.is_set():
            with self._lock:
                if self._outcome_locked() is not None:
                    if self._socket is not None:
                        try:
                            self._socket.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass  # The peer or response may already have closed it.
                    return
            self._done.wait(min(0.05, max(0, self.deadline - time.monotonic())))

    def close(self):
        self._done.set()
        if self._watcher.ident is not None:
            self._watcher.join()
        if self._socket is not None:
            self._socket.close()


class _OwnedConnection:
    def __init__(self, *args, control, **kwargs):
        super().__init__(*args, **kwargs)
        create_connection = self._create_connection

        def connect_socket(*args, **kwargs):
            # getaddrinfo and TCP connect remain synchronous stdlib operations.
            # Once connected, ownership begins before request writes/TLS handshake.
            sock = create_connection(*args, **kwargs)
            try:
                control.attach(sock)
            except BaseException:
                sock.close()
                raise
            return sock

        self._create_connection = connect_socket


class _HTTPConnection(_OwnedConnection, http.client.HTTPConnection):
    pass


class _HTTPSConnection(_OwnedConnection, http.client.HTTPSConnection):
    pass


class _HTTPHandler(HTTPHandler):
    def http_open(self, request):
        return self.do_open(_HTTPConnection, request, control=request._transport_control)


class _HTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_HTTPSConnection, request, control=request._transport_control,
                            context=self._context)


def _read_bounded(stream, limit, deadline, cancel_event):
    parts, total = [], 0
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise TransportCancelled('cancelled')
        if time.monotonic() > deadline:
            raise TransportTimeout('timeout')
        # Tighten the socket deadline after connect and after every partial read.
        raw = getattr(getattr(stream, 'fp', None), 'raw', None)
        sock = getattr(raw, '_sock', None)
        if sock is not None:
            sock.settimeout(max(0.001, deadline - time.monotonic()))
        # read1 avoids waiting to fill a whole chunk while a peer trickles bytes.
        read = getattr(stream, 'read1', stream.read)
        chunk = read(min(_CHUNK, limit + 1 - total))
        if time.monotonic() > deadline:
            raise TransportTimeout('timeout')
        if not chunk:
            return b''.join(parts)
        parts.append(chunk)
        total += len(chunk)
        if total > limit:
            raise ResponseTooLarge('too large')


class UrllibTransport:
    """No proxies/redirects; cancellation and overall deadline interrupt socket I/O.

    System DNS resolution cannot be interrupted. TCP connect uses the configured
    socket timeout (potentially per resolved address); cancellation during either
    phase is observed immediately after connect returns, before sending source.
    Header/body waits and TLS handshake are actively interrupted by the watcher.
    """

    def __init__(self, *, ssl_context=None):
        self._opener = build_opener(ProxyHandler({}), _NoRedirect(),
                                    _HTTPSHandler(context=ssl_context or ssl.create_default_context()),
                                    _HTTPHandler())
        # CDN-fronted providers (for example api.groq.com) reject urllib's default
        # User-Agent with 403, which would surface as provider_auth.
        self._opener.addheaders = [('User-Agent', USER_AGENT)]

    def send(self, request, *, cancel_event=None):
        deadline = time.monotonic() + request.timeout
        control = _RequestControl(deadline, cancel_event)
        outcome = None
        try:
            control.start()
            prepared = Request(request.url, data=request.body, method=request.method,
                               headers=dict(request.headers))
            if prepared.type not in ('http', 'https'):
                raise TransportUnavailable('transport failure')
            prepared._transport_control = control
            try:
                response = self._opener.open(prepared, timeout=request.timeout)
            except HTTPError as error:
                response = error
            try:
                status = response.status if hasattr(response, 'status') and response.status else response.code
                headers = {k.lower(): v for k, v in response.headers.items()} if response.headers else {}
                body = _read_bounded(response, request.max_response_bytes, deadline, cancel_event)
                control.check()
            finally:
                response.close()
            return TransportResponse(int(status), headers, body)
        except TransportFailure as failure:
            outcome = type(failure)
        except (socket.timeout, TimeoutError):
            outcome = TransportTimeout
        except URLError as error:
            outcome = TransportTimeout if isinstance(error.reason, (socket.timeout, TimeoutError)) else TransportUnavailable
        except (OSError, http.client.HTTPException, ValueError):
            outcome = TransportUnavailable
        finally:
            control.close()
        outcome = control.outcome() or outcome
        # Raised outside the except suites so no request/response detail is chained.
        raise outcome('transport failure')
