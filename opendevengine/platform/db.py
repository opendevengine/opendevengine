"""Serialized SQLite access with explicit persistence."""
from contextlib import contextmanager
from pathlib import Path
import shutil
import sqlite3
from threading import RLock
from .clock import now_ms
from .errors import StorageBusy, PlatformError
from .migrations import apply_migrations
from .storage_files import startup_lock, private_backup

DB_FILENAME = "opendevengine.sqlite3"


class Store:
    @classmethod
    def open(cls, data_dir, *, clock=now_ms):
        self = cls()
        self.lock = RLock()
        self.persistent = data_dir is not None
        path = None
        try:
            if data_dir is not None:
                directory = Path(data_dir)
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                path = directory / DB_FILENAME
            with startup_lock(path):
                self.conn = sqlite3.connect(str(path) if path else ":memory:", isolation_level=None, check_same_thread=False)
                self.conn.row_factory = sqlite3.Row
                if path:
                    path.chmod(0o600)
                for pragma in ("foreign_keys=ON", "busy_timeout=5000", "journal_mode=WAL", "synchronous=FULL"):
                    self.conn.execute("PRAGMA " + pragma)
                apply_migrations(self.conn, db_path=path, clock=clock)
            return self
        except Exception as exc:
            if hasattr(self, "conn"):
                self.conn.close()
            if isinstance(exc, PlatformError):
                raise
            if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
                raise StorageBusy("Storage is busy.") from None
            raise PlatformError("Unable to open platform storage.") from None

    @contextmanager
    def read(self):
        with self.lock:
            try:
                yield self.conn
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc) or "busy" in str(exc):
                    raise StorageBusy("Storage is busy.") from None
                raise

    @contextmanager
    def transaction(self):
        with self.read() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise

    def schema_version(self):
        with self.read() as conn:
            return conn.execute("SELECT coalesce(max(version), 0) FROM schema_migrations").fetchone()[0]

    def size_bytes(self):
        with self.read() as conn:
            if not self.persistent:
                return 0
            filename = conn.execute("PRAGMA database_list").fetchone()[2]
            total = 0
            for suffix in ("", "-wal", "-shm"):
                try:
                    total += Path(filename + suffix).stat().st_size
                except FileNotFoundError:
                    pass  # A concurrent checkpoint may have removed its WAL.
            return total

    def used_bytes(self):
        """Live database pages; rows deleted by a purge free pages for reuse."""
        with self.read() as conn:
            if not self.persistent:
                return 0
            pages, free, size = (conn.execute(f"PRAGMA {name}").fetchone()[0]
                                 for name in ("page_count", "freelist_count", "page_size"))
            return (pages - free) * size

    def free_bytes(self):
        """Free space on the database volume, or None for in-memory stores."""
        with self.read() as conn:
            if not self.persistent:
                return None
            filename = conn.execute("PRAGMA database_list").fetchone()[2]
        return shutil.disk_usage(Path(filename).parent).free

    def backup_to(self, target):
        with self.read() as conn:
            try:
                private_backup(conn, target)
            except (OSError, sqlite3.Error):
                raise PlatformError("Storage backup failed.") from None

    def close(self):
        with self.lock:
            self.conn.close()
