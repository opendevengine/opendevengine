"""Isolated Docker session runner (M2 spike).

Every container operation goes through a discovered trusted Docker CLI using
argument arrays, bounded output and deadlines. Containers run with no network,
no bind mounts or volumes, read-only root, all capabilities dropped,
no-new-privileges, an unprivileged UID and separate byte-capped tmpfs mounts for
/workspace and /tmp. Workspace snapshots are parsed from an in-memory tar stream;
nothing is ever extracted on the host. There is no host shell fallback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import io
import json
import math
import os
import re
import secrets
import select
import signal
import stat
import subprocess
import tarfile
import threading
import time

__all__ = [
    'DEFAULT_LIMITS', 'LIMIT_BOUNDS', 'DockerRunner', 'RunnerError', 'RunnerHandle',
    'compare_snapshots', 'validate_relative_path', 'validate_limits', 'run_bounded',
    'parse_workspace_tar', 'discover_docker_binary',
]

DEFAULT_LIMITS = {
    'memory_mib': 512, 'workspace_mib': 64, 'tmp_mib': 16, 'cpus': 1, 'pids': 64,
    'wall_seconds': 600, 'max_files': 2000, 'max_file_bytes': 262144,
    'max_output_bytes': 65536,
}
# Inclusive hard bounds. Callers may tighten or modestly relax defaults inside these,
# never pass Docker options.
LIMIT_BOUNDS = {
    'memory_mib': (128, 4096), 'workspace_mib': (1, 1024), 'tmp_mib': (1, 256),
    'cpus': (1, 4), 'pids': (16, 512), 'wall_seconds': (10, 3600),
    'max_files': (1, 20000), 'max_file_bytes': (1, 16 * 1024 * 1024),
    'max_output_bytes': (1024, 1024 * 1024),
}
SANDBOX_UID = 65534
WORKSPACE = '/workspace'
# Optional read-only reference root: a separate root-owned tmpfs, never under /workspace.
CONTEXT = '/context'
EXECUTABLE_MODE, REGULAR_MODE = '100755', '100644'
LABEL_PREFIX = 'org.opendevengine.'
LABEL_MANAGED = LABEL_PREFIX + 'managed'
LABEL_INSTANCE = LABEL_PREFIX + 'instance'
LABEL_SESSION = LABEL_PREFIX + 'session'
LABEL_ATTEMPT = LABEL_PREFIX + 'attempt'
LABEL_LIMITS = LABEL_PREFIX + 'limits'
LABEL_CONTEXT = LABEL_PREFIX + 'context'
CONTROL_TIMEOUT = 30
STOP_CONFIRM_SECONDS = 10
# Internal budget leaves headroom for process teardown inside the 10 s contract.
STOP_BUDGET_SECONDS = 9.0
JOIN_BUDGET_SECONDS = 0.75
MAX_COMMAND_TIMEOUT = 600
MAX_ARGV_ITEMS = 256
MAX_ARG_BYTES = 8192
MAX_PATH_BYTES = 1024
REQUIRED_TOOLS = ('sh', 'tar', 'find', 'grep', 'timeout', 'mktemp', 'mv', 'rm', 'mkdir', 'cat', 'sleep')

_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$')
_CONTAINER_RE = re.compile(r'^[0-9a-f]{64}$')
_IMAGE_ID_RE = re.compile(r'^sha256:[0-9a-f]{64}$')
_IMAGE_REF_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,254}$')
_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]')
_TRUSTED_DOCKER_PATHS = (
    '/usr/local/bin/docker', '/opt/homebrew/bin/docker', '/usr/bin/docker',
    '/Applications/Docker.app/Contents/Resources/bin/docker',
)
_DOCKER_ENV_PASSTHROUGH = (
    'HOME', 'USER', 'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_CONFIG', 'DOCKER_CERT_PATH',
    'DOCKER_TLS_VERIFY', 'DOCKER_API_VERSION', 'XDG_RUNTIME_DIR',
)


class RunnerError(Exception):
    """Stable, content-free runner failure. ``code`` is safe to persist."""

    def __init__(self, code: str, message: str = ''):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


@dataclass(frozen=True)
class RunnerHandle:
    instance_id: str
    session_id: str
    container_id: str = field(repr=False)
    image_id: str


# ---------------------------------------------------------------- validation

def _validate_id(value, kind: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise RunnerError('invalid_argument', f'invalid {kind}')
    return value


def validate_relative_path(path) -> str:
    """Return a normalized safe workspace-relative path or raise ``unsafe_path``."""
    if not isinstance(path, str) or not path:
        raise RunnerError('unsafe_path', 'path must be a non-empty string')
    if len(path.encode('utf-8', 'surrogatepass')) > MAX_PATH_BYTES or _CONTROL_RE.search(path):
        raise RunnerError('unsafe_path', 'path too long or contains control characters')
    if path.startswith('/') or '\\' in path:
        raise RunnerError('unsafe_path', 'path must be relative')
    while path.startswith('./'):
        path = path[2:]
    parts = path.split('/')
    if not parts or any(p in ('', '.', '..') for p in parts):
        raise RunnerError('unsafe_path', 'path contains empty, dot or parent components')
    if any(len(p.encode('utf-8', 'surrogatepass')) > 255 for p in parts):
        raise RunnerError('unsafe_path', 'path component too long')
    try:
        path.encode('utf-8')
    except UnicodeEncodeError:
        raise RunnerError('unsafe_path', 'path is not valid UTF-8') from None
    return '/'.join(parts)


def validate_limits(limits=None) -> dict:
    if limits is None:
        limits = {}
    if not isinstance(limits, dict):
        raise RunnerError('invalid_argument', 'limits must be an object')
    unknown = set(limits) - set(DEFAULT_LIMITS)
    if unknown:
        raise RunnerError('invalid_argument', 'unknown limit keys')
    merged = dict(DEFAULT_LIMITS)
    for key, value in limits.items():
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, float) and math.isfinite(value) and value.is_integer():
                value = int(value)
            else:
                raise RunnerError('invalid_argument', f'limit {key} must be a finite integer')
        low, high = LIMIT_BOUNDS[key]
        if not low <= value <= high:
            raise RunnerError('invalid_argument', f'limit {key} outside [{low}, {high}]')
        merged[key] = value
    # tmpfs pages are charged to the container memory cgroup; keep headroom.
    if merged['memory_mib'] < merged['workspace_mib'] + merged['tmp_mib'] + 64:
        raise RunnerError('invalid_argument', 'memory_mib must exceed tmpfs sizes plus 64 MiB')
    if merged['max_file_bytes'] > merged['workspace_mib'] * 1024 * 1024:
        raise RunnerError('invalid_argument', 'max_file_bytes exceeds workspace size')
    return merged


def _validate_files(files, limits) -> dict:
    if not isinstance(files, dict):
        raise RunnerError('invalid_argument', 'files must be a mapping')
    if len(files) > limits['max_files']:
        raise RunnerError('limit_exceeded', 'too many files')
    out, total = {}, 0
    for raw, content in files.items():
        path = validate_relative_path(raw)
        if not isinstance(content, (bytes, bytearray)):
            raise RunnerError('invalid_argument', 'file content must be bytes')
        if len(content) > limits['max_file_bytes']:
            raise RunnerError('limit_exceeded', 'file exceeds max_file_bytes')
        if path in out:
            raise RunnerError('invalid_argument', 'duplicate path')
        total += len(content)
        out[path] = bytes(content)
    for path in out:  # a file cannot also be a directory prefix of another file
        parts = path.split('/')
        for i in range(1, len(parts)):
            if '/'.join(parts[:i]) in out:
                raise RunnerError('invalid_argument', 'file path conflicts with directory')
    if total > limits['workspace_mib'] * 1024 * 1024:
        raise RunnerError('limit_exceeded', 'files exceed workspace size')
    return out


def _validate_modes(file_modes, files) -> dict:
    """Only regular 0644/0755 git modes for uploaded files; absent means 0644."""
    if file_modes is None:
        return {}
    if not isinstance(file_modes, dict):
        raise RunnerError('invalid_argument', 'file_modes must be a mapping')
    out = {}
    for raw, mode in file_modes.items():
        path = validate_relative_path(raw)
        if path not in files:
            raise RunnerError('invalid_argument', 'mode for unknown file')
        if mode in (0o755, EXECUTABLE_MODE):
            out[path] = EXECUTABLE_MODE
        elif mode in (0o644, REGULAR_MODE):
            out[path] = REGULAR_MODE
        else:
            raise RunnerError('invalid_argument', 'unsupported file mode')
    return out


def _context_size(context, limits):
    """Validate read-only reference files and return (tmpfs bytes or None, files)."""
    if context is None:
        return None, {}
    files = _validate_files(context, limits)
    total = sum(len(v) for v in files.values())
    size = max(1024 * 1024, total + len(files) * 8192 + 65536)
    if size > limits['workspace_mib'] * 1024 * 1024:
        raise RunnerError('limit_exceeded', 'context exceeds workspace size')
    if limits['memory_mib'] * 1024 * 1024 < (limits['workspace_mib'] + limits['tmp_mib'] + 64) * 1024 * 1024 + size:
        raise RunnerError('limit_exceeded', 'context exceeds memory headroom')
    return size, files


# ------------------------------------------------------- bounded subprocesses

@dataclass(frozen=True)
class BoundedResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool


class _Collector:
    def __init__(self, stream, cap: int):
        self.stream, self.cap = stream, cap
        self.chunks, self.size, self.truncated = [], 0, False

    def run(self):
        fd = self.stream.fileno()
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                room = self.cap - self.size
                if room > 0:
                    keep = chunk[:room]
                    self.chunks.append(keep)
                    self.size += len(keep)
                if len(chunk) > max(room, 0):
                    self.truncated = True  # drain and discard beyond the cap
        except OSError:
            pass
        finally:
            try:
                self.stream.close()
            except OSError:
                pass

    @property
    def data(self) -> bytes:
        return b''.join(self.chunks)


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class _ExitWatcher:
    """Detect leader exit WITHOUT reaping it.

    While the exited leader is an unreaped zombie its pid -- and therefore our
    process-group id -- cannot be reused, so killpg(pgid) can only reach members
    of our own new session/group. The leader is reaped only after that kill.
    macOS/BSD: kqueue EVFILT_PROC/NOTE_EXIT. Linux: waitid(WNOWAIT).
    """

    def __init__(self, pid: int):
        self.pid, self.exited, self.kq = pid, False, None
        self.supported = True
        if hasattr(select, 'kqueue') and hasattr(select, 'KQ_FILTER_PROC'):
            self.kq = select.kqueue()
            ev = select.kevent(pid, filter=select.KQ_FILTER_PROC,
                               flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT, fflags=select.KQ_NOTE_EXIT)
            try:
                self.kq.control([ev], 0)
            except ProcessLookupError:
                self.exited = True  # already a zombie (we have not reaped it)
        elif not hasattr(os, 'waitid') or not hasattr(os, 'WNOWAIT'):
            self.supported = False

    def wait(self, timeout: float) -> bool:
        if self.exited:
            return True
        if self.kq is not None:
            try:
                events = self.kq.control(None, 1, max(0.0, timeout))
            except InterruptedError:
                events = []
            if any(e.ident == self.pid for e in events):
                self.exited = True
            return self.exited
        if not self.supported:
            return False
        end = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                info = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                return False
            if info is not None and info.si_pid == self.pid:
                self.exited = True
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(min(0.01, max(0.0, end - time.monotonic())))

    def close(self):
        if self.kq is not None:
            self.kq.close()
            self.kq = None


_ABSENT_RE = re.compile(rb'^(?:Error: |Error response from daemon: )No such (?:container|object): ([0-9a-f]{64})\s*$')


def _is_verified_absent(res, container_id: str) -> bool:
    """True only for Docker's exact not-found answer about this exact id."""
    if res.timed_out or res.returncode != 1 or res.stdout.strip() not in (b'', b'[]'):
        return False
    lines = [line for line in res.stderr.strip().splitlines() if line.strip()]
    if len(lines) != 1:
        return False
    match = _ABSENT_RE.match(lines[0].strip())
    return bool(match) and match.group(1).decode('ascii') == container_id


def run_bounded(argv, *, timeout: float, max_stdout: int, max_stderr: int = 16384,
                stdin: bytes | None = None, env=None, cancel: threading.Event | None = None) -> BoundedResult:
    """Run argv (no shell) in its own process group with byte caps and a deadline.

    Output beyond the cap is drained and discarded, never buffered. On deadline or
    cancellation the whole process group is killed; leftover group members are
    killed after a normal exit too, so no descendant outlives the call.
    """
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv):
        raise RunnerError('invalid_argument', 'argv must be a list of strings')
    proc = subprocess.Popen(
        list(argv), stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, close_fds=True,
        start_new_session=True,
    )
    out, err = _Collector(proc.stdout, max_stdout), _Collector(proc.stderr, max_stderr)
    threads = [threading.Thread(target=out.run, daemon=True), threading.Thread(target=err.run, daemon=True)]
    if stdin is not None:
        def feed():
            try:
                proc.stdin.write(stdin)
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
        threads.append(threading.Thread(target=feed, daemon=True))
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    watcher = _ExitWatcher(proc.pid)
    try:
        if not watcher.supported:
            # No way to observe exit without reaping: refuse rather than risk
            # killing a recycled process group or leaking descendants.
            _kill_group(proc.pid)
            proc.wait()
            raise RunnerError('unsupported_platform', 'cannot supervise process groups safely')
        while True:
            left = deadline - time.monotonic()
            if watcher.wait(min(0.05, max(0.0, left))):
                # Leader is an unreaped zombie: pgid cannot be recycled, so this
                # kill reaches only our own leftover descendants (even ones that
                # closed every pipe). Reap only afterwards.
                _kill_group(proc.pid)
                proc.wait()
                break
            if left <= 0 or (cancel is not None and cancel.is_set()):
                timed_out = True
                _kill_group(proc.pid)  # leader alive => pgid is ours
                proc.wait()
                break
    finally:
        watcher.close()
    # The group is dead, so pipes normally hit EOF at once. One shared, small join
    # budget keeps the caller's deadline (e.g. stop's 10 s contract) honest.
    join_deadline = time.monotonic() + JOIN_BUDGET_SECONDS
    for t in threads:
        t.join(timeout=max(0.0, join_deadline - time.monotonic()))
    return BoundedResult(proc.returncode, out.data, err.data, timed_out, out.truncated, err.truncated)


# ------------------------------------------------------------ docker binary

def _trusted_file(path: str) -> bool:
    try:
        real = os.path.realpath(path)
        st = os.stat(real)
        parent = os.stat(os.path.dirname(real))
    except OSError:
        return False
    uid = os.getuid()
    if not stat.S_ISREG(st.st_mode) or not os.access(real, os.X_OK):
        return False
    if st.st_uid not in (0, uid) or st.st_mode & 0o022:
        return False
    if parent.st_uid not in (0, uid) or parent.st_mode & 0o022:
        return False
    return True


def discover_docker_binary(docker_binary=None) -> str:
    """Return a trusted Docker CLI path. Never consults PATH or the working directory."""
    if docker_binary is not None:
        if not isinstance(docker_binary, str) or not os.path.isabs(docker_binary):
            raise RunnerError('docker_untrusted', 'docker_binary must be an absolute path')
        if not _trusted_file(docker_binary):
            raise RunnerError('docker_untrusted', 'docker_binary is not a trusted executable')
        return os.path.realpath(docker_binary)
    candidates = list(_TRUSTED_DOCKER_PATHS)
    home = os.path.expanduser('~')
    if os.path.isabs(home):
        candidates.append(os.path.join(home, '.docker', 'bin', 'docker'))
    for candidate in candidates:
        if os.path.lexists(candidate) and _trusted_file(candidate):
            return os.path.realpath(candidate)
    raise RunnerError('docker_unavailable', 'no trusted Docker CLI found')


# ------------------------------------------------------------ tar parsing

def parse_workspace_tar(data: bytes, limits: dict, modes: dict | None = None) -> dict[str, bytes]:
    """Parse a tar stream in memory. Regular files only; never touches the host FS.

    When ``modes`` is a dict it receives each file's git mode (100755 when any
    execute bit is set, else 100644); the returned bytes mapping is unchanged.
    """
    files, total = {}, 0
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode='r:')
    except tarfile.TarError:
        raise RunnerError('snapshot_rejected', 'unreadable archive') from None
    with archive:
        while True:
            try:
                member = archive.next()
            except tarfile.TarError:
                raise RunnerError('snapshot_rejected', 'malformed archive') from None
            if member is None:
                break
            name = member.name
            if name in ('.', './', ''):
                if not member.isdir():
                    raise RunnerError('snapshot_rejected', 'unexpected root entry')
                continue
            path = validate_relative_path(name.rstrip('/') if member.isdir() else name)
            if member.isdir():
                continue
            if member.issym() or member.islnk():
                raise RunnerError('snapshot_rejected', f'link entry rejected: {path}')
            if member.issparse() or not member.isreg():
                raise RunnerError('snapshot_rejected', f'non-regular entry rejected: {path}')
            if member.size > limits['max_file_bytes']:
                raise RunnerError('limit_exceeded', f'file exceeds max_file_bytes: {path}')
            total += member.size
            if total > limits['workspace_mib'] * 1024 * 1024:
                raise RunnerError('limit_exceeded', 'snapshot exceeds workspace size')
            if path in files:
                raise RunnerError('snapshot_rejected', 'duplicate entry')
            if len(files) >= limits['max_files']:
                raise RunnerError('limit_exceeded', 'snapshot exceeds max_files')
            stream = archive.extractfile(member)  # in-memory file object, not extraction
            content = stream.read(member.size + 1) if stream else b''
            if len(content) != member.size:
                raise RunnerError('snapshot_rejected', 'truncated entry')
            files[path] = content
            if modes is not None:
                modes[path] = EXECUTABLE_MODE if member.mode & 0o111 else REGULAR_MODE
    return files


def compare_snapshots(base: dict, current: dict) -> dict:
    """Manifest comparison: added, modified and deleted relative paths (sorted)."""
    def digest(b):
        return hashlib.sha256(b).hexdigest()
    added = sorted(set(current) - set(base))
    deleted = sorted(set(base) - set(current))
    modified = sorted(p for p in set(base) & set(current) if digest(base[p]) != digest(current[p]))
    return {'added': added, 'modified': modified, 'deleted': deleted}


def _tar_files(files: dict, modes: dict | None = None, *, owner: int = SANDBOX_UID) -> bytes:
    buf = io.BytesIO()
    modes = modes or {}
    with tarfile.open(fileobj=buf, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(files):
            info = tarfile.TarInfo(path)
            info.size = len(files[path])
            info.mode = 0o755 if modes.get(path) == EXECUTABLE_MODE else 0o644
            info.uid = info.gid = owner
            info.mtime = 0
            info.type = tarfile.REGTYPE
            archive.addfile(info, io.BytesIO(files[path]))
    return buf.getvalue()


# ------------------------------------------------------ in-container scripts
# Fixed POSIX sh programs. User-controlled values are positional arguments only.
_CHECK_PARENTS = r'''
set -eu
set -f
p=$1
cur=/workspace
IFS=/
set -- $p
unset IFS
n=$#
i=0
for c in "$@"; do
  i=$((i + 1))
  cur=$cur/$c
  if [ "$i" -lt "$n" ]; then
    if [ -L "$cur" ]; then echo unsafe_symlink >&2; exit 90; fi
    if [ -e "$cur" ] && [ ! -d "$cur" ]; then echo not_a_directory >&2; exit 91; fi
  fi
done
'''
_READ_SCRIPT = _CHECK_PARENTS + r'''
if [ -L "$cur" ]; then echo unsafe_symlink >&2; exit 90; fi
if [ ! -e "$cur" ]; then echo not_found >&2; exit 92; fi
if [ ! -f "$cur" ]; then echo not_a_file >&2; exit 94; fi
exec cat -- "$cur"
'''
_WRITE_SCRIPT = _CHECK_PARENTS + r'''
if [ -L "$cur" ]; then echo unsafe_symlink >&2; exit 90; fi
if [ -e "$cur" ] && [ ! -f "$cur" ]; then echo not_a_file >&2; exit 94; fi
dir=${cur%/*}
mkdir -p -- "$dir" || exit 93
tmp=$(mktemp "$dir/.opendevengine-write.XXXXXX") || exit 93
if ! cat > "$tmp"; then rm -f -- "$tmp"; exit 93; fi
mv -f -- "$tmp" "$cur" || { rm -f -- "$tmp"; exit 93; }
'''
_DELETE_SCRIPT = _CHECK_PARENTS + r'''
if [ -L "$cur" ] || [ -f "$cur" ]; then exec rm -f -- "$cur"; fi
if [ ! -e "$cur" ]; then echo not_found >&2; exit 92; fi
echo not_a_file >&2; exit 94
'''
# Run as uid 0 WITHOUT capabilities: it owns the fresh /context files, so it can
# chmod them, but cannot override permissions afterwards (no CAP_DAC_OVERRIDE).
_CONTEXT_LOCK_SCRIPT = r'''
set -eu
find /context -type d -exec chmod 0555 {} +
find /context -type f -exec chmod 0444 {} +
'''
# Run as the unprivileged actor: create, append and rename attempts must all fail.
_CONTEXT_PROBE_SCRIPT = r'''
if touch /context/.odv-probe 2>/dev/null; then exit 1; fi
for f in $(find /context -type f | head -n 1); do
  if sh -c ': >> "$1"' sh "$f" 2>/dev/null; then exit 1; fi
  if mv "$f" "$f.x" 2>/dev/null; then exit 1; fi
done
exit 0
'''
_TOOLS_SCRIPT = r'''
for b in "$@"; do command -v "$b" >/dev/null 2>&1 || { echo "$b" >&2; exit 95; }; done
'''
_SCRIPT_CODES = {90: 'unsafe_path', 91: 'unsafe_path', 92: 'not_found', 93: 'workspace_full', 94: 'not_a_file'}


# ---------------------------------------------------------------- runner

class DockerRunner:
    """Owns containers labelled with this OpenDevEngine instance id only."""

    def __init__(self, instance_id: str, *, docker_binary=None):
        self.instance_id = _validate_id(instance_id, 'instance_id')
        self._docker = discover_docker_binary(docker_binary)
        env = {'PATH': os.pathsep.join([os.path.dirname(self._docker), '/usr/bin', '/bin', '/usr/sbin', '/sbin']),
               'LANG': 'C', 'LC_ALL': 'C'}
        for key in _DOCKER_ENV_PASSTHROUGH:
            if os.environ.get(key):
                env[key] = os.environ[key]
        self._env = env
        self._limits: dict[str, dict] = {}
        self._lock = threading.Lock()

    # -- docker plumbing
    def _cli(self, args, *, timeout=CONTROL_TIMEOUT, max_stdout=1024 * 1024, stdin=None) -> BoundedResult:
        try:
            return run_bounded([self._docker, *args], timeout=timeout, max_stdout=max_stdout,
                               stdin=stdin, env=self._env)
        except OSError:
            raise RunnerError('docker_unavailable', 'docker CLI could not be executed') from None

    def _cli_ok(self, args, code='container_failure', **kw) -> BoundedResult:
        res = self._cli(args, **kw)
        if res.timed_out:
            raise RunnerError('timeout', 'docker control operation timed out')
        if res.returncode != 0:
            raise RunnerError(code, 'docker operation failed')
        return res

    def _inspect_container(self, container_id: str, *, timeout: float = CONTROL_TIMEOUT):
        """Return inspect info, or None only when Docker *verifiably* reports the
        container absent. Any other failure (daemon unreachable, timeout, unknown
        error, garbage output) raises ``inspect_failed``: absence is never inferred."""
        if not _CONTAINER_RE.match(container_id):
            raise RunnerError('invalid_argument', 'invalid container id')
        res = self._cli(['container', 'inspect', '--format', '{{json .}}', '--', container_id], timeout=timeout)
        if res.timed_out:
            raise RunnerError('inspect_failed', 'docker inspect timed out')
        if res.returncode != 0:
            if _is_verified_absent(res, container_id):
                return None
            raise RunnerError('inspect_failed', 'docker inspect failed')
        try:
            info = json.loads(res.stdout.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise RunnerError('inspect_failed', 'unparseable inspect output') from None
        if not isinstance(info, dict) or info.get('Id') != container_id:
            raise RunnerError('inspect_failed', 'inspect returned a different object')
        return info

    def _owned_labels(self, info) -> dict | None:
        labels = ((info or {}).get('Config') or {}).get('Labels') or {}
        if labels.get(LABEL_MANAGED) != '1' or labels.get(LABEL_INSTANCE) != self.instance_id:
            return None
        if not _ID_RE.match(labels.get(LABEL_SESSION, '')) or not _ID_RE.match(labels.get(LABEL_ATTEMPT, '')):
            return None
        return labels

    def _verify_handle(self, handle: RunnerHandle, info=None, *, timeout: float = CONTROL_TIMEOUT):
        if not isinstance(handle, RunnerHandle) or handle.instance_id != self.instance_id:
            raise RunnerError('ownership_unverified', 'handle not owned by this runner')
        if not _CONTAINER_RE.match(handle.container_id):
            raise RunnerError('ownership_unverified', 'invalid handle')
        info = info if info is not None else self._inspect_container(handle.container_id, timeout=timeout)
        if info is None:
            return None
        labels = self._owned_labels(info)
        if (labels is None or labels.get(LABEL_SESSION) != handle.session_id
                or info.get('Image') != handle.image_id or info.get('Id') != handle.container_id):
            raise RunnerError('ownership_unverified', 'container labels do not match handle')
        return info

    def _handle_limits(self, handle: RunnerHandle) -> dict:
        if not isinstance(handle, RunnerHandle) or handle.instance_id != self.instance_id:
            raise RunnerError('ownership_unverified', 'handle not owned by this runner')
        with self._lock:
            limits = self._limits.get(handle.container_id)
        if limits is None:
            info = self._verify_handle(handle)
            if info is None:
                raise RunnerError('not_found', 'container not found')
            limits = self._limits_from_labels(info['Config']['Labels'])
            with self._lock:
                self._limits[handle.container_id] = limits
        return limits

    @staticmethod
    def _limits_from_labels(labels) -> dict:
        try:
            raw = json.loads(labels.get(LABEL_LIMITS, ''))
        except ValueError:
            raise RunnerError('ownership_unverified', 'invalid limits label') from None
        if not isinstance(raw, dict) or set(raw) != set(DEFAULT_LIMITS):
            raise RunnerError('ownership_unverified', 'invalid limits label')
        return validate_limits(raw)

    def _exec(self, handle, argv, *, timeout, max_stdout, stdin=None) -> BoundedResult:
        args = ['exec']
        if stdin is not None:
            args.append('-i')
        args += ['-u', f'{SANDBOX_UID}:{SANDBOX_UID}', '-w', WORKSPACE, '-e', 'HOME=/tmp',
                 '-e', 'TMPDIR=/tmp', handle.container_id, *argv]
        res = self._cli(args, timeout=timeout, max_stdout=max_stdout, stdin=stdin)
        head = res.stderr[:400]
        if (res.returncode != 0 and not res.timed_out and head.startswith(b'Error response from daemon')
                and b'is not running' in head):
            raise RunnerError('not_running', 'session container is not running')
        return res

    def _script(self, handle, script, path, *, max_stdout=4096, stdin=None, timeout=CONTROL_TIMEOUT):
        res = self._exec(handle, ['sh', '-c', script, 'opendevengine', path], timeout=timeout,
                         max_stdout=max_stdout, stdin=stdin)
        if res.timed_out:
            raise RunnerError('timeout', 'file operation timed out')
        if res.returncode != 0:
            raise RunnerError(_SCRIPT_CODES.get(res.returncode, 'container_failure'), 'file operation failed')
        return res

    @staticmethod
    def _create_args(*, image_id, instance_id, session_id, attempt_id, limits, context_bytes=None) -> list[str]:
        """Exact docker create argument array. No caller-provided Docker options."""
        ws = limits['workspace_mib'] * 1024 * 1024
        tmp = limits['tmp_mib'] * 1024 * 1024
        inodes = limits['max_files'] * 2 + 64
        mem = f"{limits['memory_mib']}m"
        labels = {
            LABEL_MANAGED: '1', LABEL_INSTANCE: instance_id, LABEL_SESSION: session_id,
            LABEL_ATTEMPT: attempt_id, LABEL_LIMITS: json.dumps(limits, sort_keys=True, separators=(',', ':')),
        }
        args = [
            'create', '--pull', 'never', '--name', f'opendevengine-{instance_id}-{session_id}-{attempt_id}',
            '--network', 'none', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges=true',
            '--user', f'{SANDBOX_UID}:{SANDBOX_UID}', '--read-only', '--init',
            '--tmpfs', f'{WORKSPACE}:rw,nosuid,nodev,exec,size={ws},nr_inodes={inodes},mode=0700,uid={SANDBOX_UID},gid={SANDBOX_UID}',
            '--tmpfs', f'/tmp:rw,nosuid,nodev,noexec,size={tmp},mode=1777',
            '--memory', mem, '--memory-swap', mem, '--cpus', str(limits['cpus']),
            '--pids-limit', str(limits['pids']), '--ulimit', 'nofile=256:256', '--ulimit', 'core=0:0',
            '--log-driver', 'none', '--workdir', WORKSPACE, '--env', 'HOME=/tmp', '--env', 'TMPDIR=/tmp',
            '--hostname', 'opendevengine-session', '--stop-timeout', '1',
        ]
        if context_bytes is not None:
            args += ['--tmpfs', f'{CONTEXT}:rw,nosuid,nodev,noexec,size={int(context_bytes)},'
                                f'nr_inodes={limits["max_files"] * 2 + 64},mode=0755,uid=0,gid=0']
            labels[LABEL_CONTEXT] = '1'
        for key, value in labels.items():
            args += ['--label', f'{key}={value}']
        # PID 1 (tini) runs a bounded sleep: the container ends itself at the wall limit.
        args += ['--entrypoint', 'sleep', image_id, str(limits['wall_seconds'])]
        return args

    @staticmethod
    def _verify_hostconfig(info, limits, *, context=False) -> None:
        hc = info.get('HostConfig') or {}
        problems = []
        if hc.get('Binds'):
            problems.append('binds')
        if hc.get('Mounts'):
            problems.append('mounts')
        if any((m or {}).get('Type') != 'tmpfs' for m in info.get('Mounts') or []):
            problems.append('volumes')
        if hc.get('NetworkMode') != 'none':
            problems.append('network')
        if hc.get('Privileged') or not hc.get('ReadonlyRootfs'):
            problems.append('privilege')
        if 'ALL' not in (hc.get('CapDrop') or []) or hc.get('CapAdd'):
            problems.append('capabilities')
        if not any(str(o).startswith('no-new-privileges') for o in hc.get('SecurityOpt') or []):
            problems.append('no_new_privileges')
        if hc.get('Devices') or hc.get('VolumesFrom') or hc.get('PidMode') or hc.get('IpcMode') == 'host':
            problems.append('namespaces')
        if set((hc.get('Tmpfs') or {})) != ({WORKSPACE, '/tmp', CONTEXT} if context else {WORKSPACE, '/tmp'}):
            problems.append('tmpfs')
        if hc.get('PidsLimit') != limits['pids'] or hc.get('Memory') != limits['memory_mib'] * 1024 * 1024:
            problems.append('resources')
        if (info.get('Config') or {}).get('User') != f'{SANDBOX_UID}:{SANDBOX_UID}':
            problems.append('user')
        if problems:
            raise RunnerError('isolation_unverified', 'container isolation check failed: ' + ','.join(problems))

    # -- public API
    def preflight(self, image: str) -> dict:
        reasons = []
        result = {
            'available': False, 'image_id': None, 'architecture': None, 'os': None,
            'engine_version': None, 'supported_limits': {
                k: {'default': DEFAULT_LIMITS[k], 'min': LIMIT_BOUNDS[k][0], 'max': LIMIT_BOUNDS[k][1]}
                for k in DEFAULT_LIMITS},
            'blocking_reasons': reasons,
        }
        if not isinstance(image, str) or not _IMAGE_REF_RE.match(image):
            reasons.append('invalid_image_reference')
            return result
        try:
            ver = self._cli(['version', '--format', '{{json .Server}}'], timeout=15)
        except RunnerError:
            reasons.append('docker_unavailable')
            return result
        server = None
        if ver.returncode == 0 and not ver.timed_out:
            try:
                server = json.loads(ver.stdout.decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                server = None
        if not isinstance(server, dict):
            reasons.append('docker_unavailable')
            return result
        result['engine_version'] = str(server.get('Version', ''))[:64] or None
        if str(server.get('Os', '')).lower() != 'linux':
            reasons.append('engine_not_linux')
        img = self._cli(['image', 'inspect', '--format', '{{json .}}', '--', image], timeout=15)
        info = None
        if img.returncode == 0 and not img.timed_out:
            try:
                info = json.loads(img.stdout.decode('utf-8').splitlines()[0])
            except (ValueError, UnicodeDecodeError, IndexError):
                info = None
        if not isinstance(info, dict) or not _IMAGE_ID_RE.match(str(info.get('Id', ''))):
            reasons.append('image_not_present')  # never pulled automatically
        else:
            result['image_id'] = info['Id']
            result['architecture'] = str(info.get('Architecture', ''))[:32] or None
            result['os'] = str(info.get('Os', ''))[:32] or None
            if result['os'] != 'linux':
                reasons.append('image_not_linux')
            if (info.get('Config') or {}).get('Volumes'):
                reasons.append('image_declares_volumes')
        result['available'] = not reasons
        return result

    def provision(self, session_id: str, files: dict, *, image: str, limits: dict,
                  file_modes: dict | None = None, context: dict | None = None) -> RunnerHandle:
        """``file_modes`` (optional) preserves regular 0644/0755 modes. ``context`` (optional)
        is a read-only reference tree under /context: root-owned 0555 dirs and 0444 files
        on its own noexec tmpfs, verified unwritable by the actor; never under /workspace."""
        session_id = _validate_id(session_id, 'session_id')
        limits = validate_limits(limits)
        files = _validate_files(files, limits)
        modes = _validate_modes(file_modes, files)
        context_bytes, context_files = _context_size(context, limits)
        pre = self.preflight(image)
        if pre['blocking_reasons']:
            raise RunnerError('preflight_failed', ','.join(pre['blocking_reasons']))
        image_id = pre['image_id']
        if any(h.session_id == session_id for h in self.list_owned()):
            raise RunnerError('session_exists', 'an owned container already exists for this session')
        attempt_id = secrets.token_hex(6)
        res = self._cli_ok(self._create_args(image_id=image_id, instance_id=self.instance_id,
                                             session_id=session_id, attempt_id=attempt_id, limits=limits,
                                             context_bytes=context_bytes),
                           code='provision_failed', timeout=60)
        container_id = res.stdout.decode('ascii', 'replace').strip()
        if not _CONTAINER_RE.match(container_id):
            raise RunnerError('provision_failed', 'unexpected container id')
        handle = RunnerHandle(self.instance_id, session_id, container_id, image_id)
        try:
            info = self._verify_handle(handle)
            if info is None:
                raise RunnerError('provision_failed', 'created container disappeared')
            self._verify_hostconfig(info, limits, context=context_bytes is not None)
            self._cli_ok(['start', '--', container_id], code='provision_failed', timeout=60)
            with self._lock:
                self._limits[container_id] = limits
            check = self._exec(handle, ['sh', '-c', _TOOLS_SCRIPT, 'opendevengine', *REQUIRED_TOOLS],
                               timeout=CONTROL_TIMEOUT, max_stdout=4096)
            if check.returncode != 0:
                raise RunnerError('image_unsupported', 'image lacks required POSIX tools')
            if files:
                up = self._exec(handle, ['tar', '-x', '-f', '-', '-C', WORKSPACE, '--no-same-owner',
                                         '--no-same-permissions'],
                                timeout=120, max_stdout=4096, stdin=_tar_files(files, modes))
                if up.timed_out or up.returncode != 0:
                    raise RunnerError('workspace_full' if b'No space' in up.stderr else 'provision_failed',
                                      'workspace upload failed')
            if context_bytes is not None:
                self._upload_context(handle, context_files)
        except BaseException:
            try:
                self._force_remove(container_id)
            except RunnerError:
                pass  # surfaced by list_owned()/orphan recovery
            with self._lock:
                self._limits.pop(container_id, None)
            raise
        return handle

    def _root_exec(self, handle, argv, *, stdin=None, timeout=CONTROL_TIMEOUT) -> BoundedResult:
        # uid 0 with every capability dropped and no-new-privileges; used only to
        # populate the trusted read-only reference root before any tool runs.
        args = ['exec'] + (['-i'] if stdin is not None else []) + ['-u', '0:0', '-w', '/', '-e', 'HOME=/tmp',
                                                                   handle.container_id, *argv]
        return self._cli(args, timeout=timeout, max_stdout=4096, stdin=stdin)

    def _upload_context(self, handle, files) -> None:
        if files:
            up = self._root_exec(handle, ['tar', '-x', '-f', '-', '-C', CONTEXT, '--no-same-owner',
                                          '--no-same-permissions'], stdin=_tar_files(files, owner=0), timeout=120)
            if up.timed_out or up.returncode != 0:
                raise RunnerError('provision_failed', 'context upload failed')
        lock = self._root_exec(handle, ['sh', '-c', _CONTEXT_LOCK_SCRIPT])
        if lock.timed_out or lock.returncode != 0:
            raise RunnerError('isolation_unverified', 'context lock failed')
        probe = self._exec(handle, ['sh', '-c', _CONTEXT_PROBE_SCRIPT], timeout=CONTROL_TIMEOUT, max_stdout=4096)
        if probe.timed_out or probe.returncode != 0:
            raise RunnerError('isolation_unverified', 'context is writable by the session user')

    def _force_remove(self, container_id: str) -> None:
        # Only called for a container this call just created and verified by id.
        self._cli(['kill', '--signal', 'KILL', '--', container_id], timeout=15)
        self._cli(['rm', '--', container_id], timeout=15)

    def list_files(self, handle) -> list[str]:
        limits = self._handle_limits(handle)
        cap = limits['max_files'] * (MAX_PATH_BYTES + 3) + 1
        res = self._exec(handle, ['find', '.', '-xdev', '-type', 'f', '-print0'], timeout=CONTROL_TIMEOUT,
                         max_stdout=cap)
        if res.timed_out or res.returncode != 0:
            raise RunnerError('container_failure', 'listing failed')
        names = [n for n in res.stdout.decode('utf-8', 'surrogateescape').split('\0') if n]
        if res.stdout_truncated or len(names) > limits['max_files']:
            raise RunnerError('limit_exceeded', 'workspace exceeds max_files')
        out = []
        for name in names:
            try:
                out.append(validate_relative_path(name))
            except RunnerError:
                continue  # unrepresentable names are not exposed
        return sorted(out)

    def read_file(self, handle, path: str) -> bytes:
        limits = self._handle_limits(handle)
        path = validate_relative_path(path)
        res = self._script(handle, _READ_SCRIPT, path, max_stdout=limits['max_file_bytes'])
        if res.stdout_truncated:
            raise RunnerError('limit_exceeded', 'file exceeds max_file_bytes')
        return res.stdout

    def write_file(self, handle, path: str, content: bytes) -> None:
        limits = self._handle_limits(handle)
        path = validate_relative_path(path)
        if not isinstance(content, (bytes, bytearray)):
            raise RunnerError('invalid_argument', 'content must be bytes')
        if len(content) > limits['max_file_bytes']:
            raise RunnerError('limit_exceeded', 'content exceeds max_file_bytes')
        self._script(handle, _WRITE_SCRIPT, path, stdin=bytes(content))

    def delete_file(self, handle, path: str) -> None:
        self._handle_limits(handle)
        self._script(handle, _DELETE_SCRIPT, validate_relative_path(path))

    def run_command(self, handle, argv: list[str], *, timeout_seconds: int = 30) -> dict:
        limits = self._handle_limits(handle)
        if not isinstance(argv, list) or not argv or len(argv) > MAX_ARGV_ITEMS:
            raise RunnerError('invalid_argument', 'argv must be a non-empty list')
        for arg in argv:
            if not isinstance(arg, str) or '\0' in arg or len(arg.encode('utf-8', 'surrogatepass')) > MAX_ARG_BYTES:
                raise RunnerError('invalid_argument', 'invalid argv element')
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= MAX_COMMAND_TIMEOUT:
            raise RunnerError('invalid_argument', 'timeout_seconds must be an integer in [1, 600]')
        timeout_seconds = min(timeout_seconds, limits['wall_seconds'])
        cap = limits['max_output_bytes']
        started = time.monotonic()
        # In-container coreutils timeout terminates the command tree inside the
        # container; the host deadline only bounds the docker client.
        res = self._exec(handle, ['timeout', '-k', '2', str(timeout_seconds), *argv],
                         timeout=timeout_seconds + 10, max_stdout=cap)
        elapsed = time.monotonic() - started
        timed_out = res.timed_out or (res.returncode in (124, 137) and elapsed >= timeout_seconds - 0.5)
        stderr = res.stderr[:cap]
        return {
            'exit_code': None if res.timed_out else res.returncode,
            'stdout': res.stdout.decode('utf-8', 'replace'),
            'stderr': stderr.decode('utf-8', 'replace'),
            'timed_out': timed_out,
            'truncated': res.stdout_truncated or res.stderr_truncated or len(res.stderr) > cap,
        }

    def snapshot(self, handle, *, include_modes: bool = False):
        """Default: {path: bytes}. With include_modes=True: (files, {path: git mode})."""
        limits = self._handle_limits(handle)
        cap = limits['workspace_mib'] * 1024 * 1024 + (limits['max_files'] * 2 + 64) * 2048 + 20480
        res = self._exec(handle, ['tar', '-C', WORKSPACE, '--numeric-owner', '-cf', '-', '.'],
                         timeout=120, max_stdout=cap)
        if res.stdout_truncated:
            raise RunnerError('limit_exceeded', 'snapshot archive exceeds limit')
        if res.timed_out or res.returncode != 0:
            raise RunnerError('snapshot_failed', 'workspace archive failed')
        if include_modes:
            modes = {}
            return parse_workspace_tar(res.stdout, limits, modes), modes
        return parse_workspace_tar(res.stdout, limits)

    def stop(self, handle) -> dict:
        """Kill and confirm within one total STOP_CONFIRM_SECONDS budget from entry.

        Every inspect/kill/poll gets only the remaining budget. Only a verified
        absent or verified not-running container is ``confirmed``; any control
        failure or exhausted budget returns ``containment_unverified``.
        """
        deadline = time.monotonic() + STOP_BUDGET_SECONDS
        unverified = {'confirmed': False, 'code': 'containment_unverified'}

        def remaining():
            return deadline - time.monotonic()

        if remaining() <= 0:
            return unverified
        try:
            info = self._verify_handle(handle, timeout=remaining())
        except RunnerError as exc:
            if exc.code == 'ownership_unverified':
                raise
            return unverified
        if info is None:
            return {'confirmed': True, 'code': 'absent'}
        if (info.get('State') or {}).get('Running') is False:
            return {'confirmed': True, 'code': 'already_stopped'}
        if remaining() <= 0:
            return unverified
        try:
            self._cli(['kill', '--signal', 'KILL', '--', handle.container_id], timeout=remaining())
        except RunnerError:
            pass  # confirmation below decides; a failed kill cannot be "confirmed"
        while remaining() > 0:
            try:
                info = self._inspect_container(handle.container_id, timeout=remaining())
            except RunnerError:
                info = False  # ambiguous: keep polling within budget, never assume absent
            if info is None:
                return {'confirmed': True, 'code': 'absent'}
            if info and (info.get('State') or {}).get('Running') is False:
                return {'confirmed': True, 'code': 'stopped'}
            pause = min(0.1, remaining())
            if pause > 0:
                time.sleep(pause)
        return unverified

    def discard(self, handle) -> None:
        info = self._verify_handle(handle)
        if info is None:
            with self._lock:
                self._limits.pop(handle.container_id, None)
            return
        if (info.get('State') or {}).get('Running'):
            raise RunnerError('not_stopped', 'stop the session container before discarding')
        self._cli_ok(['rm', '--', handle.container_id], code='discard_failed')
        with self._lock:
            self._limits.pop(handle.container_id, None)

    def list_owned(self) -> list[RunnerHandle]:
        res = self._cli_ok(['ps', '-a', '--no-trunc', '--format', '{{.ID}}',
                            '--filter', f'label={LABEL_MANAGED}=1',
                            '--filter', f'label={LABEL_INSTANCE}={self.instance_id}'], code='docker_unavailable')
        handles = []
        for line in res.stdout.decode('ascii', 'replace').split():
            if not _CONTAINER_RE.match(line):
                continue
            info = self._inspect_container(line)
            labels = self._owned_labels(info)
            if labels is None or info.get('Id') != line or not _IMAGE_ID_RE.match(str(info.get('Image', ''))):
                continue
            try:
                self._limits_from_labels(labels)
            except RunnerError:
                continue
            handles.append(RunnerHandle(self.instance_id, labels[LABEL_SESSION], line, info['Image']))
        return handles
