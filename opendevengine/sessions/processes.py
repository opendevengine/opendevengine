"""OS process start identity and recorded-harness containment for recovery.

A recorded PID alone is never trusted: a recycled PID with a different start
identity is reported as ``absent`` (the original is gone) and never signalled.
"""
import os
import signal
import subprocess
import time

_PS = '/bin/ps'


def start_identity(pid):
    """Return an opaque start-identity string for ``pid``, None if absent, or raise OSError
    when identity cannot be determined (the caller must then fail closed)."""
    if type(pid) is not int or pid <= 1:
        raise OSError('invalid pid')
    proc = '/proc/%d/stat' % pid
    if os.path.isdir('/proc/self'):
        try:
            with open(proc, 'rb') as handle:
                data = handle.read(4096).decode('ascii', 'replace')
        except FileNotFoundError:
            return None
        fields = data.rsplit(')', 1)[-1].split()
        return 'linux:' + fields[19]  # starttime (field 22) in clock ticks since boot
    if not os.path.exists(_PS):
        raise OSError('ps unavailable')
    # Same normalization as the profile producer: /bin/ps -p PID -o lstart=, C locale, UTC.
    result = subprocess.run([_PS, '-p', str(pid), '-o', 'lstart='], stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=3, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'TZ': 'UTC'})
    text = result.stdout.decode('ascii', 'replace').strip()
    if result.returncode != 0 or not text:
        # ps exits 1 with no output for a missing process.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            raise OSError('identity unavailable') from None
        raise OSError('identity unavailable')
    return 'ps:' + ' '.join(text.split())


def _group_gone(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def contain_recorded(pid, identity, *, budget_seconds=5.0):
    """Kill a recorded harness process group if, and only if, its identity still matches.

    Returns 'absent' (verified gone or recycled PID), 'terminated' or 'unverified'.
    Never signals this process or its own process group.
    """
    if pid is None:
        return 'absent'
    if pid == os.getpid() or pid == os.getpgrp():
        return 'unverified'
    try:
        current = start_identity(pid)
    except (OSError, subprocess.SubprocessError):
        return 'unverified'
    if current is None:
        return 'absent' if _group_gone(pid) else 'unverified'
    if identity is None:
        # A live PID without a recorded start identity can never be proven ours or
        # proven gone: fail closed and keep the containment block.
        return 'unverified'
    if current != identity:
        # A different process now owns that PID; the recorded harness is gone. Its group
        # may still exist only if the reused PID is a new group leader we do not own.
        return 'absent'
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return 'terminated'
        except PermissionError:
            return 'unverified'
        end = time.monotonic() + budget_seconds / 2
        while time.monotonic() < end:
            if _group_gone(pid):
                return 'terminated'
            time.sleep(0.05)
    return 'unverified'
