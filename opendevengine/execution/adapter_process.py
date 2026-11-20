"""Bounded host harness process ownership; never executes repository commands."""
from __future__ import annotations

from dataclasses import dataclass
import os
import selectors
import select
import signal
import subprocess
import time
from typing import Callable, Mapping


@dataclass(frozen=True)
class ProcessOutcome:
    exit_code: int | None
    reason: str
    stop_confirmed: bool
    output_bytes: int


class ProcessContainmentError(RuntimeError):
    pass


class ProcessLimitsError(ValueError):
    """Rejected limits before any process was started."""


class ProcessSpawnError(OSError):
    """The subprocess could not be started; no process exists to contain.

    Raised only at the spawn boundary, so callers can tell it apart from an
    OSError raised by an ``on_line``/``on_started`` callback after containment.
    """


class _LeaderExit:
    """Observe leader exit without reaping, so its process group cannot be reused."""
    def __init__(self, pid):
        self.pid, self.exited, self.queue = pid, False, None
        if hasattr(select, "kqueue"):
            self.queue = select.kqueue()
            event = select.kevent(pid, filter=select.KQ_FILTER_PROC,
                                  flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                  fflags=select.KQ_NOTE_EXIT)
            try:
                self.queue.control([event], 0)
            except ProcessLookupError:
                self.exited = True  # unreaped zombie still reserves the pid
            except BaseException:
                self.queue.close()
                raise
        elif not hasattr(os, "waitid") or not hasattr(os, "WNOWAIT"):
            raise ProcessContainmentError("unsupported_platform")

    def ready(self):
        if self.exited:
            return True
        if self.queue is not None:
            try:
                self.exited = bool(self.queue.control(None, 1, 0))
            except InterruptedError:
                return False
        else:
            info = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            self.exited = info is not None and info.si_pid == self.pid
        return self.exited

    def close(self):
        if self.queue is not None:
            self.queue.close()


def _terminate_group(process: subprocess.Popen, watcher: _LeaderExit) -> bool:
    """Kill the reserved group before reaping, then verify no group survives."""
    # Do not poll/wait the leader before this kill: its unreaped pid pins the pgid.
    for sig, duration in ((signal.SIGTERM, 0.5), (signal.SIGKILL, 0.5)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        except PermissionError:
            pass  # Darwin reports EPERM when only the unreaped leader remains.
        end = time.monotonic() + duration
        while not watcher.ready() and time.monotonic() < end:
            time.sleep(0.02)
        if watcher.ready():
            # A TERM handler may exit without terminating its descendants.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                pass  # Verify disappearance after reaping; never assume success.
            break
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        return False
    end = time.monotonic() + 2
    while time.monotonic() < end:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass  # Darwin may deny signal 0 while a signalled child exits.
        time.sleep(0.02)
    return False


def run_stream(
    argv: list[str], *, cwd: str, env: Mapping[str, str],
    on_line: Callable[[bytes], None], timeout_seconds: float,
    max_output_bytes: int, stop_event=None, on_started=None, stdin_file=None,
) -> ProcessOutcome:
    """Consume bounded JSON lines, discard stderr, and contain every exit path.

    A callback exception aborts the group before propagating. Output limits apply
    to combined streams before buffering, including a single unterminated line.
    """
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or
        not 0 < timeout_seconds <= 3600 or type(max_output_bytes) is not int or
        not 1024 <= max_output_bytes <= 4_194_304):
        raise ProcessLimitsError("invalid process limits")
    if stop_event is not None and stop_event.is_set():
        return ProcessOutcome(None, "cancelled", True, 0)
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=dict(env), stdin=stdin_file if stdin_file is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, close_fds=True,
        )
    except (OSError, ValueError) as exc:
        raise ProcessSpawnError("spawn_failed") from exc
    watcher = selector = None
    try:
        watcher = _LeaderExit(process.pid)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, True)
        selector.register(process.stderr, selectors.EVENT_READ, False)
    except BaseException:
        # No one has reaped the leader, so its group id is still reserved.
        if stop_event is not None:
            stop_event.set()
        try:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                raise ProcessContainmentError("containment_failure") from None
        finally:
            if watcher is not None:
                watcher.close()
            if selector is not None:
                selector.close()
            process.stdout.close()
            process.stderr.close()
        raise ProcessContainmentError("supervision_unavailable") from None
    count = 0
    pending = b""
    reason = "exited"
    deadline = time.monotonic() + timeout_seconds
    try:
        if on_started is not None:
            on_started(process.pid)
        while selector.get_map():
            if stop_event is not None and stop_event.is_set():
                reason = "cancelled"
                break
            if time.monotonic() >= deadline:
                reason = "timeout"
                break
            ready = selector.select(timeout=0.05)
            if not ready and watcher.ready():
                break
            for key, _ in ready:
                chunk = os.read(key.fileobj.fileno(), 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                count += len(chunk)
                if count > max_output_bytes:
                    reason = "output_limit"
                    break
                if key.data:
                    pending += chunk
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        if line.strip():
                            on_line(line)
            if reason != "exited":
                break
        if reason == "exited" and pending.strip():
            on_line(pending)
        # A CLI may close its pipes before exiting; that is still deadline-bound.
        while reason == "exited" and not watcher.ready():
            if stop_event is not None and stop_event.is_set():
                reason = "cancelled"
                break
            if time.monotonic() >= deadline:
                reason = "timeout"
                break
            time.sleep(0.02)
    finally:
        if stop_event is not None:
            stop_event.set()  # revoke the broker guard before signalling the CLI
        try:
            confirmed = _terminate_group(process, watcher)
        except Exception:
            # A failed supervision primitive proves nothing about the group: kill
            # it best-effort and report containment as unconfirmed, never as an
            # ordinary error that callers would read as a contained exit.
            confirmed = False
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
        finally:
            watcher.close()
            selector.close()
            process.stdout.close()
            process.stderr.close()
        if not confirmed:
            raise ProcessContainmentError("containment_failure")
    return ProcessOutcome(process.returncode, reason, confirmed, count)
