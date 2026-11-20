"""UTC timestamps and opaque identifiers."""
import secrets
import time
from datetime import datetime, timezone


def now_ms():
    return int(time.time() * 1000)


def format_ts(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix):
    return f"{prefix}_{secrets.token_hex(12)}"
