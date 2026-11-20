"""Private SQLite snapshots and process-wide startup serialization (POSIX)."""
from contextlib import contextmanager, closing
import fcntl
import os
from pathlib import Path
import sqlite3
import tempfile


@contextmanager
def startup_lock(db_path):
    if db_path is None:
        yield
        return
    fd = os.open(str(db_path) + '.startup.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # Closing releases the advisory process lock, including on errors.


def private_backup(conn, target):
    """Publish a completed snapshot atomically; even its temporary file starts private."""
    target = Path(target)
    fd, temporary = tempfile.mkstemp(prefix='.snapshot-', dir=target.parent)
    os.close(fd)
    try:
        with closing(sqlite3.connect(temporary)) as backup:
            conn.backup(backup)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
