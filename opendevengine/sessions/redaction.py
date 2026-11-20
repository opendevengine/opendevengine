"""Bounded redaction for persisted/streamed session payloads and logs."""
import json
import re
from opendevengine.platform.credentials import contains_credential, redact_credentials

MAX_EVENT_BYTES = 8192
_SECRET_RES = (
    re.compile(r'(?i)\b(?:authorization|password|passwd|secret|client[_-]?secret|api[_-]?key|access[_-]?token|refresh[_-]?token|token|bearer)\s*[:=]\s*[^\s,;\'"]+'),
    re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}'),
)
_HOST_PATH_RE = re.compile(r'(?:/Users/|/home/|/private/|/var/folders/|/tmp/odv-|/root/|[A-Z]:\\Users\\)[^\s"\'<>]*')
_CONTROL_RE = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')
# Keys that are private runtime state; never included in normal payloads.
PRIVATE_KEYS = frozenset({'container_id', 'socket_path', 'token_file', 'token', 'harness_pid', 'pid', 'pgid',
                          'harness_start', 'executable', 'binary', 'endpoint', 'private'})


def redact_text(value, limit=4000):
    text = value if isinstance(value, str) else str(value)
    text = _CONTROL_RE.sub('', text)
    text = redact_credentials(text, '[redacted credential]')
    for pattern in _SECRET_RES:
        text = pattern.sub('[redacted credential]', text)
    text = _HOST_PATH_RE.sub('[private host path]', text)
    if limit is not None and len(text) > limit:
        text = text[:limit] + '…'
    return text


def contains_secret(data):
    """Conservative content guard for acquisition (bytes) and artifacts."""
    if isinstance(data, bytes):
        data = data.decode('utf-8', 'replace')
    return contains_credential(data)


def _clean(value, depth=0):
    if depth > 6:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if value == value and value not in (float('inf'), float('-inf')) else None
    if isinstance(value, str):
        return redact_text(value, 2000)
    if isinstance(value, (list, tuple)):
        return [_clean(v, depth + 1) for v in list(value)[:100]]
    if isinstance(value, dict):
        out = {}
        for key, item in list(value.items())[:64]:
            key = str(key)[:64]
            if key in PRIVATE_KEYS:
                continue
            out[key] = _clean(item, depth + 1)
        return out
    return redact_text(str(value), 200)


def event_payload(payload):
    """Sanitize and bound one event payload to <= MAX_EVENT_BYTES canonical JSON."""
    clean = _clean(payload if isinstance(payload, dict) else {'value': payload})
    encoded = json.dumps(clean, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    if len(encoded.encode('utf-8')) <= MAX_EVENT_BYTES:
        return encoded
    # Shrink long strings first, then fall back to an explicit truncation marker.
    for limit in (500, 120, 40):
        def shrink(v):
            if isinstance(v, str):
                return v[:limit]
            if isinstance(v, list):
                return [shrink(x) for x in v[:20]]
            if isinstance(v, dict):
                return {k: shrink(x) for k, x in v.items()}
            return v
        candidate = json.dumps(dict(shrink(clean), truncated=True), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        if len(candidate.encode('utf-8')) <= MAX_EVENT_BYTES:
            return candidate
    return json.dumps({'truncated': True}, separators=(',', ':'))
