"""Bounded background indexing reconciliation; failed signatures need explicit retry."""
import hashlib
import json
from .platform.clock import now_ms, new_id
from .platform.errors import PlatformError


class IndexScheduler:
    def __init__(self, platform, index, github, providers, *, clock=now_ms, queue_limit=64):
        if type(queue_limit) is not int or not 1 <= queue_limit <= 256:
            raise ValueError('Invalid background queue limit.')
        self.platform, self.index, self.github, self.providers = platform, index, github, providers
        self.clock, self.queue_limit = clock, queue_limit
        self.local_markers = {}
        self.error_code = None
        github.register_eligibility_listener(self._eligibility_changed)

    def _eligibility_changed(self, repository_id, eligible):
        # Explicit selection/reconnect changes are a new scheduling intent even
        # under a constant clock. No timestamp is used as a generation fence.
        with self.platform.store.transaction() as conn:
            conn.execute('DELETE FROM index_schedule_state WHERE repository_id=?',(repository_id,))

    def register(self, repository_id, revision):
        self.local_markers[repository_id] = revision

    def _pending(self):
        with self.platform.store.read() as conn:
            return conn.execute("SELECT count(*) FROM index_runs WHERE state IN ('queued','running','waiting','paused')").fetchone()[0]

    def _schedule(self, repository_id, marker, embedding_version):
        state = self.index.get_state(repository_id)
        if not state['eligible'] or state['active_run']:
            return False
        signature = hashlib.sha256(json.dumps([marker,embedding_version],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        with self.platform.store.read() as conn:
            row = conn.execute('SELECT signature FROM index_schedule_state WHERE repository_id=?',(repository_id,)).fetchone()
        if row is not None and row[0] == signature:
            return False
        intent = new_id("schedule")
        run = self.index.request_index(repository_id, priority=-10, embedding_version_id=embedding_version,
                                       scheduling_intent=intent)
        with self.platform.store.transaction() as conn:
            current = conn.execute('SELECT profile_version_id,scheduling_intent FROM index_runs WHERE id=?',(run['id'],)).fetchone()
            if current is None or current[0] != embedding_version or current[1] != intent:
                return False  # A concurrent explicit run is not this scheduling intent.
            conn.execute('INSERT INTO index_schedule_state VALUES(?,?,?,?) ON CONFLICT(repository_id) DO UPDATE SET signature=excluded.signature,run_id=excluded.run_id,requested_at=excluded.requested_at',
                         (repository_id,signature,run['id'],self.clock()))
        return True

    def run_once(self):
        available = max(0,self.queue_limit-self._pending())
        if not available:
            return 0
        embedding = self.providers.roles().get('embedding',{})
        # A configured role must pass its synthetic capability check before
        # background source transmission. No role permits explicit lexical mode.
        if embedding.get('version_id') and embedding.get('status') != 'ready':
            self.error_code = 'embedding_not_verified'
            return 0
        version = embedding.get('version_id')
        count, errors = 0, []
        candidates = [(repo,marker) for repo,marker in self.local_markers.items()]
        cursor = None
        while len(candidates) < 10000:
            page = self.github.list_repositories(cursor=cursor,limit=100,index_enabled=True)
            for repo in page['items']:
                candidates.append((repo['id'],[repo.get('pushed_at'),repo.get('default_branch'),repo.get('full_name')]))
            cursor = page['next_cursor']
            if cursor is None:
                break
        for repository_id, marker in candidates:
            if count >= available:
                break
            try:
                count += int(self._schedule(repository_id,marker,version))
            except PlatformError:
                errors.append('index_schedule_unavailable')
        self.error_code = errors[0] if errors else 'catalog_limit' if cursor is not None else None
        return count
