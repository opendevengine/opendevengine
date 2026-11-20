"""Finite source policy shared by acquisition, activation and citation reads."""
import fnmatch
import hashlib
import re
from pathlib import PurePosixPath
from opendevengine.platform.credentials import contains_credential

CHUNKER = 'lines-symbols-v2'
PARSER = 'text-v1'
DEFAULTS = dict(files=2000, file_bytes=256*1024, source_bytes=16*1024*1024,
                chunks=10000, batch_size=16, batch_tokens=8192,
                embedding_tokens=8*1024*1024, elapsed_ms=81920, disk_bytes=4*1024**3)
# Indexing pauses before leaving less than this free on the data volume.
DISK_RESERVE = 1024**3
# Runs persist expanded caps. Runs created before DEFAULTS_VERSION 2 carry no
# marker; continuing one adopts the current default for a superseded default
# value (512 MiB was then also the highest allowed, so it was never a reduction).
# Marked runs keep every stored cap, including an explicit 512 MiB.
DEFAULTS_VERSION = 2
SUPERSEDED_DEFAULTS = {'disk_bytes': {512*1024*1024}}
BLOCKED = {'node_modules', 'vendor', 'dist', 'build', 'target', '__pycache__',
           '.git', '.svn', 'coverage', 'generated', '.next', 'venv', '.venv'}


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def limits(value):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - (set(DEFAULTS) | {'monetary_cap'}):
        raise ValueError('Invalid index limits.')
    result = dict(DEFAULTS)
    for key, cap in DEFAULTS.items():
        if key in value:
            if type(value[key]) is not int or not 1 <= value[key] <= cap:
                raise ValueError('Limits must lower finite defaults.')
            result[key] = value[key]
    if 'monetary_cap' in value:
        import math
        cap = value['monetary_cap']
        if type(cap) not in (int, float) or not math.isfinite(cap) or cap < 0:
            raise ValueError('Invalid monetary cap.')
        result['monetary_cap'] = cap
    return result


def exclusions(value):
    if not isinstance(value, (list, tuple)) or len(value) > 100 or any(
            not isinstance(x, str) or not safe_path(x.rstrip('/')) or len(x) > 256 for x in value):
        raise ValueError('Invalid exclusions.')
    return list(value)


def safe_path(path):
    return (isinstance(path, str) and 0 < len(path) <= 1024 and
            not path.startswith('/') and '\\' not in path and
            not any(ord(c) < 32 for c in path) and
            all(p not in ('', '.', '..') for p in path.split('/')))


def excluded(path, patterns=()):
    if not safe_path(path):
        return 'unsafe_path'
    parts = PurePosixPath(path).parts
    name = parts[-1].lower()
    if any(p.lower() in BLOCKED for p in parts):
        return 'generated'
    if (any(p.startswith('.') or p.lower() in {'secrets','credentials','keys','private_keys'} for p in parts) or
        re.search(r'(?:^|[._-])(secrets?|credentials?|passwords?|tokens?|private|id_rsa|id_ed25519)(?:[._-]|$)', name) or
        name.endswith(('.pem', '.key', '.p12', '.pfx', '.keystore'))):
        return 'sensitive'
    if name.endswith(('.min.js', '.map', '.lock', '.png', '.jpg', '.zip', '.pdf', '.woff', '.exe', '.so', '.dylib')):
        return 'generated'
    if any(fnmatch.fnmatchcase(path, p) or path == p.rstrip('/') or path.startswith(p.rstrip('/') + '/') for p in patterns):
        return 'configured'
    return None


def decode(content):
    if not isinstance(content, bytes) or b'\0' in content:
        return None
    try:
        text = content.decode('utf-8')
    except UnicodeError:
        return None
    # Conservative content guard, before FTS or provider calls.
    if contains_credential(text):
        return None
    return text.replace('\r\n', '\n')


def chunks(text):
    """Bound normal chunks to 2048 estimated tokens; preserve oversized lines for FTS."""
    lines = text.splitlines()
    start = 0
    while start < len(lines):
        end, size = start, 0
        while end < len(lines) and end - start < 60:
            n = len(lines[end].encode())
            if n > 8192:
                # Oversized lines retain exact line citations; semantic indexing may skip them.
                if end == start:
                    end += 1
                break
            if size + n > 8192:
                break
            if end - start >= 25 and re.match(r'^(?:async )?(?:def |class |function |export (?:function|class) )', lines[end]):
                break
            size += n + 1
            end += 1
        body = '\n'.join(lines[start:end])
        if body.strip():
            yield start + 1, end, body
        start = end if end - start < 10 else end - 5


def estimated_tokens(text):
    """Explicit UTF-8/4 planning estimate, not a tokenizer or a byte count.

    Provider-reported input usage takes precedence for charged totals. Source/disk
    safety remains independently byte-bounded; estimates are not exact model usage.
    """
    return (len(text.encode('utf-8')) + 3) // 4
