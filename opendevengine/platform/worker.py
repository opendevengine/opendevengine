"""Explicit single-step worker; no production handlers or background threads."""
from typing import Callable, Mapping, Any
from .jobs import DEFAULT_LEASE_MS, Job, json_object
from .errors import LeaseLost

class RetryableError(Exception):
    code = 'transient'

class PermanentError(Exception):
    code = 'invalid_input'

class CancelledError(Exception):
    """Handler confirms it stopped without unrecorded external effects."""

class JobInterrupted(BaseException):
    """Cooperative application shutdown, separate from handler failures/cancellation."""

class UnknownOutcomeError(Exception):
    code = 'unknown_outcome'

Handler = Callable[[Job, 'JobContext'], Mapping[str, Any] | None]


class JobContext:
    def __init__(self, queue, lease, lease_ms):
        self.queue, self.lease, self.lease_ms = queue, lease, lease_ms

    def checkpoint(self, data):
        self.queue.heartbeat(self.lease, checkpoint=data, lease_ms=self.lease_ms)

    @property
    def cancel_requested(self):
        return self.queue.heartbeat(self.lease, lease_ms=self.lease_ms).cancel_requested


class Worker:
    def __init__(self, queue, handlers, *, worker_id, lease_ms=DEFAULT_LEASE_MS):
        self.queue, self.handlers = queue, dict(handlers)
        self.worker_id, self.lease_ms = worker_id, lease_ms

    def run_once(self):
        lease = self.queue.claim(self.worker_id, kinds=self.handlers, lease_ms=self.lease_ms)
        if lease is None:
            return False
        context = JobContext(self.queue, lease, self.lease_ms)
        try:
            try:
                result = self.handlers[lease.job.kind](lease.job, context)
                if result is not None:
                    json_object(result)
            except JobInterrupted:
                if lease.job.effect == 'safe':
                    self.queue.release_safe(lease)
                else:
                    self.queue.mark_unknown_outcome(lease, code='unknown_outcome', message='Worker interrupted')
            except LeaseLost:
                raise
            except CancelledError:
                self.queue.acknowledge_cancel(lease)
            except UnknownOutcomeError as exc:
                self.queue.mark_unknown_outcome(lease, code=exc.code, message=type(exc).__name__)
            except (RetryableError, PermanentError) as exc:
                self.queue.fail(lease, code=exc.code, message=type(exc).__name__, retryable=isinstance(exc, RetryableError))
            except Exception as exc:
                if lease.job.effect == 'external':
                    self.queue.mark_unknown_outcome(lease, code='internal', message=type(exc).__name__)
                else:
                    self.queue.fail(lease, code='internal', message=type(exc).__name__, retryable=False)
            else:
                self.queue.complete(lease, result=result)
        except LeaseLost:
            pass  # Another owner has fenced this worker; never write another outcome.
        return True
