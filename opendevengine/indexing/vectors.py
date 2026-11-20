"""Private APSW/sqlite-vec store. Every KNN is snapshot-filtered inside SQL."""
import json
import math
from pathlib import Path
from threading import RLock
from .safety import digest


class VectorUnavailable(Exception):
    pass


class VectorStore:
    def __init__(self, data_dir=None):
        self.lock = RLock()
        self.conn = None
        self.path = str(Path(data_dir) / 'vectors.sqlite3') if data_dir is not None else None
        try:
            import apsw
            import sqlite_vec
            path = ':memory:'
            if data_dir is not None:
                directory = Path(data_dir)
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = str(directory / 'vectors.sqlite3')
            self.conn = apsw.Connection(path)
            self.conn.enableloadextension(True)
            try:
                self.conn.loadextension(sqlite_vec.loadable_path())
            finally:
                self.conn.enableloadextension(False)
            if path != ':memory:':
                Path(path).chmod(0o600)
            self.conn.execute('PRAGMA busy_timeout=5000')
            self.conn.execute('CREATE TABLE IF NOT EXISTS namespaces(id TEXT PRIMARY KEY, dimensions INTEGER NOT NULL)')
            self.conn.execute('CREATE TABLE IF NOT EXISTS cache(repository_id TEXT, namespace_id TEXT, hash TEXT, vector TEXT NOT NULL, PRIMARY KEY(repository_id,namespace_id,hash))')
        except Exception as exc:
            if self.conn:
                self.conn.close()
            self.conn = None
            self.error = type(exc).__name__

    @property
    def available(self):
        return self.conn is not None

    def _require(self):
        if not self.available:
            raise VectorUnavailable('Vector dependency unavailable.')

    def _table(self, namespace, dimensions=None):
        self._require()
        table = 'vec_' + digest(namespace)
        row = self.conn.execute('SELECT dimensions FROM namespaces WHERE id=?', (namespace,)).fetchone()
        if row is None:
            if dimensions is None or type(dimensions) is not int or not 1 <= dimensions <= 4096:
                raise VectorUnavailable('Unknown vector namespace.')
            self.conn.execute(f'CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0(chunk_id TEXT PRIMARY KEY, snapshot_id TEXT, repository_id TEXT, embedding float[{dimensions}] distance_metric=cosine)')
            self.conn.execute('INSERT INTO namespaces VALUES(?,?)', (namespace, dimensions))
        elif dimensions is not None and row[0] != dimensions:
            raise ValueError('Incompatible dimensions.')
        return table

    @staticmethod
    def validate(vector, dimensions):
        if not isinstance(vector, (list, tuple)) or len(vector) != dimensions or any(
                type(v) not in (int, float) or not math.isfinite(v) for v in vector) or not any(vector):
            raise ValueError('Invalid embedding.')
        return json.dumps(vector, allow_nan=False)

    def cached(self, repository, namespace, content_hash):
        with self.lock:
            self._require()
            row = self.conn.execute('SELECT vector FROM cache WHERE repository_id=? AND namespace_id=? AND hash=?', (repository, namespace, content_hash)).fetchone()
            return json.loads(row[0]) if row else None

    def put(self, namespace, dimensions, repository, snapshot, chunk_id, content_hash, vector):
        encoded = self.validate(vector, dimensions)
        with self.lock:
            table = self._table(namespace, dimensions)
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                self.conn.execute(f'DELETE FROM {table} WHERE chunk_id=?', (chunk_id,))
                self.conn.execute(f'INSERT INTO {table}(chunk_id,snapshot_id,repository_id,embedding) VALUES(?,?,?,?)', (chunk_id, snapshot, repository, encoded))
                self.conn.execute('INSERT OR REPLACE INTO cache VALUES(?,?,?,?)', (repository, namespace, content_hash, encoded))
                self.conn.execute('COMMIT')
            except BaseException:
                self.conn.execute('ROLLBACK')
                raise

    def query(self, namespace, vector, snapshots, limit):
        with self.lock:
            table = self._table(namespace)
            dims = self.conn.execute('SELECT dimensions FROM namespaces WHERE id=?', (namespace,)).fetchone()[0]
            encoded = self.validate(vector, dims)
            hits = []
            for snapshot in snapshots:
                hits.extend(self.conn.execute(f'SELECT chunk_id,distance FROM {table} WHERE embedding MATCH ? AND k=? AND snapshot_id=? ORDER BY distance', (encoded, limit, snapshot)).fetchall())
            return sorted(hits, key=lambda x: (x[1], x[0]))[:limit]

    def count(self, namespace, snapshot):
        with self.lock:
            self._require()
            if not self.conn.execute('SELECT 1 FROM namespaces WHERE id=?',(namespace,)).fetchone():
                return 0
            table = self._table(namespace)
            return self.conn.execute(f'SELECT count(*) FROM {table} WHERE snapshot_id=?', (snapshot,)).fetchone()[0]

    def discard_snapshot(self, snapshot):
        """Remove only unactivated staging rows; reusable content cache is retained."""
        with self.lock:
            if not self.available and (self.path is None or not Path(self.path).exists()):
                return
            self._require()
            for namespace, in list(self.conn.execute('SELECT id FROM namespaces')):
                table = self._table(namespace)
                self.conn.execute(f'DELETE FROM {table} WHERE snapshot_id=?',(snapshot,))

    def size_bytes(self):
        if self.path:
            return sum(Path(p).stat().st_size for p in (self.path,self.path+'-wal',self.path+'-shm') if Path(p).exists())
        if self.conn:
            return self.conn.execute('PRAGMA page_count').fetchone()[0] * self.conn.execute('PRAGMA page_size').fetchone()[0]
        return 0

    def used_bytes(self):
        """Live pages only: deleted rows leave reusable free pages that do not count."""
        with self.lock:
            if not self.conn:
                return self.size_bytes()
            pages, free, size = (self.conn.execute(f'PRAGMA {name}').fetchone()[0]
                                 for name in ('page_count', 'freelist_count', 'page_size'))
            return (pages - free) * size

    def purge(self, repository):
        with self.lock:
            if not self.available and (self.path is None or not Path(self.path).exists()):
                return
            self._require()
            for namespace, in list(self.conn.execute('SELECT id FROM namespaces')):
                table = self._table(namespace)
                self.conn.execute(f'DELETE FROM {table} WHERE repository_id=?', (repository,))
            self.conn.execute('DELETE FROM cache WHERE repository_id=?', (repository,))

    def close(self):
        with self.lock:
            if self.conn:
                self.conn.close()
                self.conn = None
