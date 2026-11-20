"""Durable immutable context; no shared entrypoints or schema mutation."""
from collections import Counter
from pathlib import Path
import json
import re
import time
from ..platform.clock import now_ms, new_id
from ..platform.errors import PlatformError, NotFound, LeaseLost
from ..platform.worker import CancelledError
from .safety import DEFAULTS, DEFAULTS_VERSION, DISK_RESERVE, SUPERSEDED_DEFAULTS, CHUNKER, PARSER, digest, limits as validate_limits, exclusions as validate_exclusions, excluded, safe_path, decode, chunks, estimated_tokens
from .snapshots import local_snapshot, github_snapshot
from .vectors import VectorStore, VectorUnavailable


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


class IndexError(PlatformError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class StopRun(Exception):
    pass


# Automatic retry ceilings are independent of ordinary continuation job IDs.
MAX_TRANSIENT_FAILURES = 3
MAX_RATE_LIMIT_RETRIES = 10
WORKER_WAIT_MS = 30000
TIME_SLICE_WAIT_MS = 5000


class IndexService:
    def __init__(self, store, jobs, github, providers, *, data_dir=None, vector_store=None,
                 snapshot_reader=None, clock=now_ms):
        self.store, self.jobs, self.github, self.providers = store, jobs, github, providers
        self.clock, self.snapshot_reader = clock, snapshot_reader
        if data_dir is None and store.persistent:
            with store.read() as db:
                filename = db.execute('PRAGMA database_list').fetchone()[2]
            data_dir = str(Path(filename).parent)
        self.vectors = vector_store if vector_store is not None else VectorStore(data_dir)
        if github is not None:
            github.register_eligibility_listener(self._eligibility_changed)
            github.register_purge_hook('indexing', self.purge_repository)

    def _one(self, db, table, ident):
        row = db.execute('SELECT * FROM ' + table + ' WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise NotFound('Context record unavailable.')
        return dict(row)

    def _source(self, db, repository_id):
        source = self._one(db, 'index_sources', repository_id)
        source['exclusions'] = json.loads(source['exclusions_json'])
        if source['kind'] == 'github' and self.github is not None:
            source['name'] = self.github.get_repository(source['github_repository_id'])['full_name']
        return source

    def _eligible(self, source):
        metadata = json.loads(source['metadata_json'])
        return not (metadata.get('purging') or metadata.get('purged')) and bool(source['index_enabled']) and (source['kind'] != 'github' or self.github.is_eligible(source['github_repository_id']))

    def _require(self, source):
        if not self._eligible(source):
            raise IndexError('repository_unavailable')
        if source['kind'] == 'github':
            self.github.require_access(source['github_repository_id'])

    def _eligibility_changed(self, repository_id, eligible):
        if eligible:
            with self.store.transaction() as db:
                row = db.execute("SELECT metadata_json FROM index_sources WHERE id=? AND kind='github'", (repository_id,)).fetchone()
                if row:
                    metadata = json.loads(row[0])
                    if not metadata.get('purging'):
                        metadata.pop('purged', None)
                        db.execute("UPDATE index_sources SET index_enabled=1,metadata_json=?,updated_at=? WHERE id=?",(encoded(metadata),self.clock(),repository_id))
        else:
            with self.store.transaction() as db:
                db.execute("UPDATE index_runs SET state='cancelled',error_code='repository_unavailable',updated_at=? WHERE repository_id=? AND state IN ('queued','running','waiting','paused')", (self.clock(), repository_id))

    def register_local(self, name, path, exclusions=()):
        root = Path(path).expanduser().resolve(strict=True)
        if not root.is_dir() or not isinstance(name, str) or not 1 <= len(name) <= 200:
            raise ValueError('Invalid local source.')
        patterns = validate_exclusions(exclusions)
        with self.store.transaction() as db:
            row = db.execute('SELECT id FROM index_sources WHERE local_path=?', (str(root),)).fetchone()
            if row:
                return self.get_state(row[0])
            from uuid import uuid4
            ident, now = 'local:' + str(uuid4()), self.clock()
            db.execute("INSERT INTO index_sources(id,kind,name,local_path,exclusions_json,created_at,updated_at) VALUES(?,'local',?,?,?,?,?)", (ident, name, str(root), encoded(patterns), now, now))
        return self.get_state(ident)

    def register_demo(self, snapshot, *, restore=False):
        if not isinstance(snapshot.name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', snapshot.name):
            raise ValueError('Invalid demo name.')
        ident = 'demo:' + snapshot.name
        with self.store.read() as db:
            existing = db.execute('SELECT metadata_json FROM index_sources WHERE id=?',(ident,)).fetchone()
            metadata = json.loads(existing[0]) if existing else {}
            if metadata.get('purging'):
                raise IndexError('purge_pending')
            if metadata.get('purged') and not restore:
                return self.get_state(ident)
        # Demo source is bounded, filtered and cached privately, never job payload data.
        files, size = {}, 0
        for path, lines in snapshot.files.items():
            if len(files) >= 2000:
                break
            if excluded(path):
                continue
            text = '\n'.join(lines)
            raw = text.encode()
            if len(raw) > 256*1024 or size + len(raw) > 16*1024*1024 or decode(raw) is None:
                continue
            files[path] = text
            size += len(raw)
        metadata = encoded({'files': files, 'revision': digest(encoded(files)), 'limited': bool(snapshot.limited)})
        now = self.clock()
        with self.store.transaction() as db:
            existing = db.execute('SELECT metadata_json FROM index_sources WHERE id=?',(ident,)).fetchone()
            if existing and json.loads(existing[0]).get('purging'):
                raise IndexError('purge_pending')
            if existing and json.loads(existing[0]).get('purged') and not restore:
                return self.get_state(ident)
            db.execute("INSERT INTO index_sources(id,kind,name,metadata_json,created_at,updated_at) VALUES(?,'demo',?,?,?,?) ON CONFLICT(id) DO UPDATE SET metadata_json=excluded.metadata_json,updated_at=excluded.updated_at WHERE index_sources.metadata_json!=excluded.metadata_json", (ident, snapshot.name, metadata, now, now))
        return self.get_state(ident)

    def _ensure_source(self, repository_id):
        with self.store.read() as db:
            existing = db.execute('SELECT 1 FROM index_sources WHERE id=?', (repository_id,)).fetchone()
        if existing:
            return
        if not isinstance(repository_id, str) or not re.fullmatch(r'gh:[1-9][0-9]*', repository_id) or self.github is None:
            raise NotFound('Repository unavailable.')
        repo = self.github.get_repository(repository_id)
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO index_sources(id,kind,github_repository_id,name,created_at,updated_at) VALUES(?,'github',?,?,?,?)", (repository_id, repository_id, repo.get('full_name', repo['name']), self.clock(), self.clock()))

    def request_index(self, repository_id, revision=None, priority=0, embedding_version_id=None, limits=None, *, scheduling_intent=None):
        if scheduling_intent is not None and (not isinstance(scheduling_intent, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", scheduling_intent)):
            raise ValueError("Invalid scheduling intent.")
        caps = validate_limits(limits)
        if revision is not None and (not isinstance(revision, str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', revision)):
            raise ValueError('An immutable revision is required.')
        self._ensure_source(repository_id)
        if embedding_version_id is None and self.providers:
            embedding_version_id = self.providers.roles().get('embedding', {}).get('version_id')
        if embedding_version_id:
            self.providers.embedding_namespace(embedding_version_id)
        with self.store.transaction() as db:
            self._require(self._source(db, repository_id))
            row = db.execute("SELECT id FROM index_runs WHERE repository_id=? AND state IN ('queued','running','waiting','paused')", (repository_id,)).fetchone()
            if row:
                return self._public_run(self._one(db, 'index_runs', row[0]))
            abandoned = [r[0] for r in db.execute("SELECT s.id FROM repository_snapshots s WHERE s.repository_id=? AND s.state='staging' AND NOT EXISTS(SELECT 1 FROM index_runs r WHERE r.snapshot_id=s.id AND r.state IN ('queued','running','waiting','paused'))",(repository_id,))]
            for sid in abandoned:
                self._discard_staging(db,sid)
            ident, now = new_id('ix'), self.clock()
            job = self.jobs.enqueue_in_transaction(db, 'index.run', {'run_id': ident}, priority=priority, idempotency_key=ident + ':0')
            db.execute("INSERT INTO index_runs(id,repository_id,job_id,state,requested_revision,profile_version_id,limits_json,created_at,updated_at,scheduling_intent) VALUES(?,?,?,'queued',?,?,?,?,?,?)", (ident, repository_id, job.id, revision, embedding_version_id, encoded({**caps, 'defaults_version': DEFAULTS_VERSION}), now, now, scheduling_intent))
            return self._public_run(self._one(db, 'index_runs', ident))

    def _public_run(self, row):
        keys = ('id','repository_id','snapshot_id','job_id','state','phase','continuation','error_code','retry_at','created_at','updated_at')
        result = {key: row[key] for key in keys}
        result['progress'] = json.loads(row['progress_json'])
        return result

    def get_run(self, run_id):
        with self.store.read() as db:
            return self._public_run(self._one(db, 'index_runs', run_id))

    def pause(self, run_id):
        with self.store.transaction() as db:
            row = self._one(db, 'index_runs', run_id)
            if row['state'] in ('queued','running','waiting'):
                db.execute("UPDATE index_runs SET state='paused',updated_at=? WHERE id=?", (self.clock(),run_id))
        return self.get_run(run_id)

    def _continue(self, run_id, states):
        with self.store.transaction() as db:
            run = self._one(db, 'index_runs', run_id)
            self._require(self._source(db, run['repository_id']))
            if run['state'] not in states:
                return self._public_run(run)
            conflict = db.execute("SELECT id FROM index_runs WHERE repository_id=? AND id!=? AND state IN ('queued','running','waiting','paused')",(run['repository_id'],run_id)).fetchone()
            if conflict:
                raise IndexError('active_run_conflict')
            if run['job_id']:
                old = self.jobs.get(run['job_id'])
                if old.state == 'leased' and old.lease_expires_at >= self.clock():
                    raise IndexError('checkpoint_pending')
                self.jobs._reconcile(db)
                if self.jobs.get(run['job_id']).state == 'queued':
                    # Cancel the stale pending job in this same transaction.
                    self.jobs._finish(db, run['job_id'], 'cancelled')
            if run['snapshot_id']:
                snapshot = self._one(db,'repository_snapshots',run['snapshot_id'])
                policy = json.loads(snapshot['coverage_json']).get('selection_hash')
                current = digest(encoded(self._source(db,run['repository_id'])['exclusions']))
                if run['error_code'] == 'selection_changed' or snapshot['chunker_version'] != CHUNKER or (policy is not None and policy != current):
                    self._discard_staging(db,run['snapshot_id'])
                    db.execute("UPDATE index_runs SET snapshot_id=NULL,phase='queued',checkpoint_json='{}',progress_json='{}',requested_revision=? WHERE id=?",(snapshot['revision'],run_id))
            if run['state'] in ('failed','cancelled'):
                current = self._one(db,'index_runs',run_id)
                checkpoint = json.loads(current['checkpoint_json'])
                checkpoint.pop('retry',None)
                db.execute('UPDATE index_runs SET checkpoint_json=? WHERE id=?',(encoded(checkpoint),run_id))
            caps = json.loads(run['limits_json'])
            if 'defaults_version' not in caps:
                upgraded = {k: DEFAULTS[k] for k, superseded in SUPERSEDED_DEFAULTS.items() if caps.get(k) in superseded}
                db.execute('UPDATE index_runs SET limits_json=? WHERE id=?',
                           (encoded({**caps, **upgraded, 'defaults_version': DEFAULTS_VERSION}), run_id))
            continuation = run['continuation'] + 1
            job = self.jobs.enqueue_in_transaction(db, 'index.run', {'run_id': run_id}, idempotency_key=f'{run_id}:{continuation}')
            db.execute("UPDATE index_runs SET state='queued',job_id=?,continuation=?,error_code=NULL,retry_at=NULL,updated_at=? WHERE id=?", (job.id,continuation,self.clock(),run_id))
        return self.get_run(run_id)

    def resume(self, run_id):
        return self._continue(run_id, ('paused',))

    def retry(self, run_id):
        return self._continue(run_id, ('failed','waiting','cancelled'))

    def set_local_selection(self, repository_id, index_enabled, exclusions=None):
        if type(index_enabled) is not bool:
            raise ValueError('Invalid selection.')
        patterns = None if exclusions is None else validate_exclusions(exclusions)
        with self.store.transaction() as db:
            source = self._source(db, repository_id)
            if json.loads(source['metadata_json']).get('purging'):
                raise IndexError('purge_pending')
            if source['kind'] == 'github':
                raise IndexError('selection_authoritative_in_github')
            if index_enabled and source['kind'] == 'demo' and 'revision' not in json.loads(source['metadata_json']):
                raise IndexError('demo_registration_required')
            metadata = json.loads(source['metadata_json'])
            if index_enabled:
                metadata.pop('purged', None)
            db.execute('UPDATE index_sources SET index_enabled=?,exclusions_json=?,metadata_json=?,updated_at=? WHERE id=?', (index_enabled, source['exclusions_json'] if patterns is None else encoded(patterns), encoded(metadata), self.clock(), repository_id))
        return self.get_state(repository_id)

    def set_exclusions(self, repository_id, exclusions):
        """Change source policy only, never GitHub indexing/implementation selection."""
        patterns = validate_exclusions(exclusions)
        self._ensure_source(repository_id)
        with self.store.transaction() as db:
            source = self._source(db,repository_id)
            self._require(source)
            db.execute('UPDATE index_sources SET exclusions_json=?,updated_at=? WHERE id=?',
                       (encoded(patterns),self.clock(),repository_id))
        return self.get_state(repository_id)

    def _discard_staging(self, db, snapshot_id):
        snapshot = self._one(db,'repository_snapshots',snapshot_id)
        if snapshot['state'] != 'staging':
            return
        if not self.vectors.available:
            return  # Defer vector cleanup until dependencies are available again.
        self.vectors.discard_snapshot(snapshot_id)
        db.execute('DELETE FROM repository_snapshots WHERE id=?',(snapshot_id,))

    def _schedule(self, db, run, code, retry_at=None):
        continuation = run['continuation'] + 1
        delay = {'worker_limit':WORKER_WAIT_MS,'elapsed_limit':TIME_SLICE_WAIT_MS}.get(code,1000)
        when = max(self.clock()+delay,retry_at or 0)
        job = self.jobs.enqueue_in_transaction(db,'index.run',{'run_id':run['id']},
            idempotency_key=f"{run['id']}:{continuation}",run_after=when)
        db.execute("UPDATE index_runs SET state='waiting',job_id=?,continuation=?,error_code=?,retry_at=?,updated_at=? WHERE id=?",
                   (job.id,continuation,code,when,self.clock(),run['id']))

    def _checkpoint_with_retry(self, db, run_id, checkpoint, *, progressed=False):
        """Keep retry accounting across cursors, phase changes and scheduling slices.

        Only persisted source/vector progress resets consecutive transient failures.
        Rate-limit retries are a lifetime automatic ceiling for this explicit attempt.
        Callers already own the fenced Store transaction.
        """
        current = self._one(db,'index_runs',run_id)
        retry = json.loads(current['checkpoint_json']).get('retry',{})
        if progressed:
            retry['transient_failures'] = 0
        return dict(checkpoint,retry=retry)

    def get_state(self, repository_id):
        self._ensure_source(repository_id)
        with self.store.read() as db:
            source = self._source(db, repository_id)
            eligible = self._eligible(source)
            active = self._one(db, 'repository_snapshots', source['active_snapshot_id']) if source['active_snapshot_id'] else None
            row = db.execute('SELECT * FROM index_runs WHERE repository_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (repository_id,)).fetchone()
            run = dict(row) if row else None
            status = 'metadata_only'
            if active:
                coverage = json.loads(active['coverage_json'])
                status = 'partial' if coverage.get('partial') else 'ready'
                if source['updated_at'] > active['activated_at'] or coverage.get('selection_hash') != digest(encoded(source['exclusions'])):
                    status = 'stale'
                if source['kind'] == 'github':
                    current_repo = self.github.get_repository(repository_id)
                    stamp = digest(encoded([current_repo['default_branch'],current_repo.get('pushed_at')]))
                    if stamp != coverage.get('catalog_stamp'):
                        status = 'stale'
            if run and run['state'] in ('queued','running','waiting','paused'):
                status = 'stale' if active else 'indexing'
            elif run and run['state'] == 'failed':
                status = 'stale' if active else 'failed'
            if not eligible:
                status = 'excluded'
            public_active = None
            if active and eligible:
                public_active = {k: active[k] for k in ('id','revision','revision_kind','branch','activated_at')}
                public_active.update(coverage=json.loads(active['coverage_json']), mode='hybrid' if active['namespace_id'] else 'lexical_only')
            implementation = False
            if source['kind'] == 'github':
                implementation = self.github.get_repository(repository_id)['implementation_enabled']
            metadata = json.loads(source['metadata_json'])
            return dict(repository_id=repository_id,name=source['name'],kind=source['kind'],eligible=eligible,
                        purged=bool(metadata.get('purged')),purging=bool(metadata.get('purging')),
                        implementation_enabled=implementation,status=status,active_snapshot=public_active,
                        active_run=self._public_run(run) if run and run['state'] in ('queued','running','waiting','paused') else None)

    def _fence(self, db, job, context, *, allow_paused=False):
        run = self._one(db, 'index_runs', job.payload['run_id'])
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job.id,)).fetchone()
        lease = context.lease
        if (row is None or row['state'] != 'leased' or row['lease_owner'] != lease.owner or
            row['lease_token'] != lease.token or row['lease_expires_at'] <= self.clock() or run['job_id'] != job.id):
            raise LeaseLost('Index lease lost.')
        if row['cancel_requested']:
            raise CancelledError()
        if run['state'] == 'paused' and not allow_paused:
            raise StopRun()
        if run['state'] not in ('queued','running','waiting','paused'):
            raise StopRun()
        source = self._source(db, run['repository_id'])
        self._require(source)
        if run['snapshot_id']:
            snapshot = self._one(db,'repository_snapshots',run['snapshot_id'])
            policy = json.loads(snapshot['coverage_json']).get('selection_hash')
            if policy is not None and policy != digest(encoded(source['exclusions'])):
                raise IndexError('selection_changed')
        return run, source

    def _boundary(self, job, context):
        # Validate expiry before heartbeat; platform heartbeat itself only checks token.
        with self.store.transaction() as db:
            run, source = self._fence(db, job, context)
        context.checkpoint({'phase':run['phase']})
        return run, source

    def _cancel_guard(self, job, context, paths):
        service = self
        class Guard:
            def is_set(self):
                try:
                    with service.store.read() as db:
                        _, source = service._fence(db,job,context)
                        return any(excluded(path,source['exclusions']) for path in paths)
                except (PlatformError,StopRun,CancelledError):
                    return True
        return Guard()

    def _snapshot(self, source, revision, caps):
        if self.snapshot_reader and source['kind'] != 'demo':
            return self.snapshot_reader(source, revision, caps)
        if source['kind'] == 'local':
            return local_snapshot(source, revision, caps)
        if source['kind'] == 'github':
            return github_snapshot(self.github, source, revision, caps)
        meta = json.loads(source['metadata_json'])
        if revision and revision != meta['revision']:
            raise IndexError('demo_revision_unavailable')
        return dict(revision=meta['revision'],revision_kind='content_hash',branch=None,
                    files=({'path': p,'content': t.encode()} for p,t in sorted(meta['files'].items())), limited=meta['limited'])

    def run_index(self, job, context):
        try:
            return self._run_index(job, context)
        except StopRun:
            return {'run_id':job.payload['run_id'],'state':self.get_run(job.payload['run_id'])['state']}
        except LeaseLost:
            raise
        except CancelledError:
            with self.store.transaction() as db:
                run, _ = self._fence_for_error(db, job, context)
                db.execute("UPDATE index_runs SET state='cancelled',updated_at=? WHERE id=?", (self.clock(),run['id']))
            raise
        except Exception as exc:
            # Only stable content-free codes enter durable diagnostics.
            code = getattr(exc, 'code', 'index_failed')
            code = {'provider_rate_limited':'rate_limited','provider_timeout':'timeout','provider_unavailable':'upstream_unavailable'}.get(code,code)
            if code not in {'repository_unavailable','rate_limited','timeout','upstream_unavailable','auth','cost_unavailable','disk_limit','selection_changed'}:
                code = 'index_failed'
            with self.store.transaction() as db:
                run, _ = self._fence_for_error(db, job, context)
                if run['state'] in ('paused','cancelled'):
                    return {'run_id':run['id'],'state':run['state']}
                source = self._source(db,run['repository_id'])
                if not self._eligible(source):
                    code = 'repository_unavailable'
                elif run['snapshot_id']:
                    policy = json.loads(self._one(db,'repository_snapshots',run['snapshot_id'])['coverage_json']).get('selection_hash')
                    if policy is not None and policy != digest(encoded(source['exclusions'])):
                        code = 'selection_changed'
                checkpoint = json.loads(run['checkpoint_json'])
                retry = checkpoint.setdefault('retry',{})
                state, delay = 'failed', 0
                if code in {'timeout','upstream_unavailable'}:
                    failures = retry.get('transient_failures',0) + 1
                    retry['transient_failures'] = failures
                    if failures < MAX_TRANSIENT_FAILURES:
                        state, delay = 'waiting', 2000*2**(failures-1)
                elif code == 'rate_limited':
                    waits = retry.get('rate_limit_retries',0)
                    if waits < MAX_RATE_LIMIT_RETRIES:
                        retry['rate_limit_retries'] = waits+1
                        state, delay = 'waiting', min(300000,2000*2**min(waits,7))
                progress = json.loads(run['progress_json'])
                progress['retry'] = retry
                db.execute('UPDATE index_runs SET checkpoint_json=?,progress_json=? WHERE id=?',
                           (encoded(checkpoint),encoded(progress),run['id']))
                retry_at = getattr(exc,'retry_at',None) if state == 'waiting' else None
                db.execute('UPDATE index_runs SET state=?,error_code=?,retry_at=?,updated_at=? WHERE id=?', (state,code,retry_at,self.clock(),run['id']))
                if state == 'waiting':
                    self._schedule(db,run,code,max(retry_at or 0,self.clock()+delay))
            return {'run_id':job.payload['run_id'],'state':state,'error_code':code}

    def _fence_for_error(self, db, job, context):
        # Error handling must fence too, but permission loss is itself the error.
        run = self._one(db, 'index_runs', job.payload['run_id'])
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job.id,)).fetchone()
        if not row or run['job_id'] != job.id or row['state'] != 'leased' or row['lease_token'] != context.lease.token or row['lease_owner'] != context.lease.owner or row['lease_expires_at'] <= self.clock():
            raise LeaseLost('Index lease lost.')
        if row['cancel_requested']:
            run['state'] = 'cancelled'
            db.execute("UPDATE index_runs SET state='cancelled',updated_at=? WHERE id=?",(self.clock(),run['id']))
        return run, None

    def _disk_exhausted(self, caps, incoming):
        """The index cap, or the volume's free-space reserve, would be crossed."""
        if self.store.used_bytes() + self.vectors.used_bytes() + incoming > caps['disk_bytes']:
            return True
        try:
            free = self.store.free_bytes()
        except OSError:
            return True  # Unknown free space pauses resumably rather than failing the run.
        return free is not None and free - incoming < DISK_RESERVE

    def _pause_limit(self, db, run, code):
        db.execute("UPDATE index_runs SET state='paused',error_code=?,updated_at=? WHERE id=?", (code,self.clock(),run['id']))

    def _run_index(self, job, context):
        run, source = self._boundary(job, context)
        caps = json.loads(run['limits_json'])
        caps.pop('defaults_version', None)  # Provenance marker, not a limit.
        started = time.monotonic()
        with self.store.transaction() as db:
            self._fence(db,job,context)
            active = db.execute("SELECT count(*) FROM index_runs r JOIN jobs j ON j.id=r.job_id WHERE r.state='running' AND j.state='leased' AND j.lease_expires_at>? AND r.id!=?",(self.clock(),run['id'])).fetchone()[0]
            if active >= 2:
                self._schedule(db,run,'worker_limit')
                return {'state':'waiting'}
            db.execute("UPDATE index_runs SET state='running',phase=CASE WHEN phase='queued' THEN 'acquiring' ELSE phase END,updated_at=? WHERE id=?", (self.clock(),run['id']))
        if run['snapshot_id']:
            with self.store.read() as db:
                snapshot = self._one(db,'repository_snapshots',run['snapshot_id'])
            revision = snapshot['revision']
            if snapshot['chunker_version'] != CHUNKER:
                with self.store.transaction() as db:
                    run,source = self._fence(db,job,context)
                    self._discard_staging(db,run['snapshot_id'])
                    db.execute("UPDATE index_runs SET snapshot_id=NULL,phase='queued',checkpoint_json='{}',progress_json='{}',requested_revision=? WHERE id=?",(revision,run['id']))
                    run = self._one(db,'index_runs',run['id'])
        else:
            revision = run['requested_revision']
        if run['phase'] not in ('embedding','activation'):
            if run['snapshot_id']:
                with self.store.read() as db:
                    source['_ingested_paths'] = {r[0] for r in db.execute('SELECT path FROM snapshot_files WHERE snapshot_id=?',(run['snapshot_id'],))}
            acquisition_policy = digest(encoded(source['exclusions']))
            data = self._snapshot(source, revision, caps)
            rev, kind = data.get('revision'), data.get('revision_kind')
            if kind not in ('git','content_hash') or not isinstance(rev,str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})' if kind == 'git' else r'[0-9a-f]{64}',rev) or (revision and revision != rev):
                raise IndexError('invalid_snapshot')
            if source['kind'] != 'demo' and kind != 'git':
                raise IndexError('invalid_snapshot')
            with self.store.transaction() as db:
                run, source = self._fence(db,job,context)
                if acquisition_policy != digest(encoded(source['exclusions'])):
                    raise IndexError('selection_changed')
                sid = run['snapshot_id'] or new_id('snap')
                if not run['snapshot_id']:
                    db.execute("INSERT INTO repository_snapshots(id,repository_id,revision,revision_kind,branch,state,chunker_version,parser_version,created_at) VALUES(?,?,?,?,?,'staging',?,?,?)", (sid,source['id'],rev,kind,data.get('branch'),CHUNKER,PARSER,self.clock()))
                    db.execute('UPDATE repository_snapshots SET coverage_json=? WHERE id=?',(encoded({'selection_hash':digest(encoded(source['exclusions']))}),sid))
                    db.execute('UPDATE index_runs SET snapshot_id=? WHERE id=?',(sid,run['id']))
            checkpoint = json.loads(run['checkpoint_json'])
            cursor = checkpoint.get('ingest_cursor',0)
            skips = Counter(checkpoint.get('skipped',{}))
            total, count, chunk_count = (checkpoint.get(k,0) for k in ('source_bytes','files','chunks'))
            seen = set()
            def save_ingest(db, position, *, progressed=False):
                self._fence(db,job,context)
                progress = dict(files=count,source_bytes=total,chunks=chunk_count,skipped=dict(skips),limits=caps)
                saved = self._checkpoint_with_retry(db,run['id'],dict(progress,ingest_cursor=position),progressed=progressed)
                db.execute("UPDATE index_runs SET phase='ingesting',progress_json=?,checkpoint_json=?,updated_at=? WHERE id=?",
                    (encoded(progress),encoded(saved),self.clock(),run['id']))
            for position,entry in enumerate(data['files']):
                path = entry.get('path')
                if position < cursor:
                    seen.add(path)
                    continue
                run, source = self._boundary(job,context)
                if position > cursor and time.monotonic() - started > caps['elapsed_ms']/1000:
                    with self.store.transaction() as db:
                        save_ingest(db,position)
                        self._schedule(db,run,'elapsed_limit')
                    return {'state':'waiting'}
                if len(seen) >= caps['files']*4:
                    skips['tree_limit'] += 1
                    break
                reason = excluded(path,source['exclusions']) or entry.get('kind')
                if path in seen:
                    reason = 'duplicate'
                seen.add(path)
                raw = entry.get('content')
                if not reason and (not isinstance(raw,bytes) or len(raw)>caps['file_bytes']):
                    reason = 'file_bytes'
                text = decode(raw) if not reason else None
                if not reason and text is None:
                    reason = 'binary_or_sensitive'
                if reason:
                    skips[reason if reason in {'unsafe_path','generated','sensitive','configured','symlink','submodule','file_bytes','tree_limit','unsupported','duplicate','binary_or_sensitive'} else 'unsupported'] += 1
                    with self.store.transaction() as db:
                        save_ingest(db,position+1,progressed=True)
                    continue
                if count >= caps['files'] or total+len(raw)>caps['source_bytes']:
                    skips['source_limit'] += 1
                    break
                parts = list(chunks(text))
                if chunk_count + len(parts)>caps['chunks']:
                    skips['chunk_limit'] += 1
                    break
                with self.store.transaction() as db:
                    run, current = self._fence(db,job,context)
                    if excluded(path,current['exclusions']):
                        raise IndexError('selection_changed')
                    if self._disk_exhausted(caps, len(raw)*4):
                        self._pause_limit(db,run,'disk_limit')
                        return {'state':'paused'}
                    db.execute('INSERT OR IGNORE INTO snapshot_files VALUES(?,?,?,?,?,?)',(sid,path,digest(raw),len(raw),len(text.splitlines()),text))
                    for first,last,body in parts:
                        cid = digest(encoded([sid,path,first,last]))
                        cur = db.execute('INSERT OR IGNORE INTO code_chunks(id,snapshot_id,path,start_line,end_line,content_hash,text,language) VALUES(?,?,?,?,?,?,?,?)',(cid,sid,path,first,last,digest(body),body,Path(path).suffix.lstrip('.')))
                        if cur.rowcount:
                            db.execute('INSERT INTO code_chunks_fts VALUES(?,?,?)',(cid,re.sub(r'[^\w]',' ',path),body))
                    count += 1
                    total += len(raw)
                    chunk_count += len(parts)
                    save_ingest(db,position+1,progressed=True)
            resource_skips = {'file_bytes','tree_limit','source_limit','chunk_limit'}
            coverage = dict(files=count,source_bytes=total,chunks=chunk_count,skipped=dict(skips),limits=caps,
                            partial=bool(resource_skips & skips.keys()) or bool(data.get('limited')))
            coverage['selection_hash'] = digest(encoded(source['exclusions']))
            if source['kind'] == 'github':
                repo = self.github.get_repository(source['id'])
                coverage['catalog_stamp'] = digest(encoded([repo['default_branch'],repo.get('pushed_at')]))
            with self.store.transaction() as db:
                run,_ = self._fence(db,job,context)
                db.execute('UPDATE repository_snapshots SET coverage_json=? WHERE id=?',(encoded(coverage),sid))
                saved = self._checkpoint_with_retry(db,run['id'],{},progressed=True)
                db.execute("UPDATE index_runs SET phase='embedding',progress_json=?,checkpoint_json=? WHERE id=?",(encoded(coverage),encoded(saved),run['id']))
        run,source = self._boundary(job,context)
        sid = run['snapshot_id']
        # Embedding receives its own time slice after the ingestion checkpoint.
        started = time.monotonic()
        namespace_id = None
        degradation = 'embedding_unassigned'
        if run['profile_version_id'] and self.vectors.available:
            spec = self.providers.embedding_namespace(run['profile_version_id'])
            namespace_id = digest(encoded([spec['key'],run['profile_version_id'],spec['dimensions'],'none',CHUNKER]))
            with self.store.transaction() as db:
                self._fence(db,job,context)
                # canonical identity includes immutable profile version as required.
                provider_key = digest(encoded([spec['key'],run['profile_version_id']]))
                db.execute('INSERT OR IGNORE INTO embedding_namespaces VALUES(?,?,?,?,?,?,?)',(namespace_id,provider_key,run['profile_version_id'],spec['dimensions'],'none',CHUNKER,self.clock()))
                db.execute('UPDATE repository_snapshots SET namespace_id=? WHERE id=?',(namespace_id,sid))
            with self.store.read() as db:
                rows = [dict(r) for r in db.execute('SELECT * FROM code_chunks WHERE snapshot_id=? ORDER BY path,start_line',(sid,))]
            checkpoint = json.loads(run['checkpoint_json'])
            cursor = checkpoint.get('embedded',0)
            tokens = checkpoint.get('tokens',0)
            semantic_skipped = checkpoint.get('semantic_skipped',0)
            context_skipped = checkpoint.get('context_skipped',0)
            config = self.providers.get_version(run['profile_version_id'])['config']
            price = config.get('input_price_per_million')
            offset = cursor
            while offset < len(rows):
                run, source = self._boundary(job,context)
                if offset > cursor and time.monotonic()-started > caps['elapsed_ms']/1000:
                    with self.store.transaction() as db:
                        self._fence(db,job,context)
                        self._schedule(db,run,'elapsed_limit')
                    return {'state':'waiting'}
                batch, values, estimate, skipped, context_skips = [], [], 0, 0, 0
                # Budget/cache decisions are per chunk. One large or unaffordable
                # chunk must not disable vectors already staged for other chunks.
                end = offset
                for row in rows[offset:offset+caps['batch_size']]:
                    value = self.vectors.cached(source['id'],namespace_id,row['content_hash'])
                    charge = estimated_tokens(row['text']) if value is None else 0
                    if value is None and (charge > caps['batch_tokens'] or tokens+charge > caps['embedding_tokens']):
                        skipped += 1
                        end += 1
                        continue
                    if value is None and charge > config.get('context_limit',8192):
                        skipped += 1
                        context_skips += 1
                        end += 1
                        continue
                    if estimate+charge > caps['batch_tokens'] or tokens+estimate+charge > caps['embedding_tokens']:
                        break
                    batch.append(row)
                    values.append(value)
                    estimate += charge
                    end += 1
                if any(excluded(r['path'],source['exclusions']) for r in batch):
                    raise IndexError('selection_changed')
                missing = [i for i,v in enumerate(values) if v is None]
                if missing and 'monetary_cap' in caps and (price is None or (tokens+estimate)*price/1000000>caps['monetary_cap']):
                    with self.store.transaction() as db:
                        self._fence(db,job,context)
                        self._pause_limit(db,run,'cost_unavailable' if price is None else 'cost_limit')
                    return {'state':'paused'}
                charged = 0
                if missing:
                    # A default 60s provider timeout must fit inside the live queue
                    # lease (normally 30s). Extend only while strictly fenced, before
                    # I/O, within the queue's existing finite 600s lease maximum.
                    duration = min(600000,max(context.lease_ms,int(config.get('timeout_seconds',60)*1000)+5000))
                    with self.store.transaction() as db:
                        self._fence(db,job,context)
                        db.execute('UPDATE jobs SET lease_expires_at=? WHERE id=?',(self.clock()+duration,job.id))
                    result = self.providers.embed(run['profile_version_id'],[batch[i]['text'] for i in missing],contains_source=True,cancel_event=self._cancel_guard(job,context,[r['path'] for r in batch]))
                    if result.get('namespace',{}).get('key') != spec['key'] or len(result['vectors']) != len(missing):
                        raise IndexError('namespace_mismatch')
                    for i,v in zip(missing,result['vectors']):
                        values[i] = v
                    usage = result.get('usage') or {}
                    actual = usage.get('input_tokens')
                    charged = actual if type(actual) is int and actual >= 0 else estimate
                with self.store.transaction() as db:
                    run,source = self._fence(db,job,context)
                    if any(excluded(r['path'],source['exclusions']) for r in batch):
                        raise IndexError('selection_changed')
                    if self._disk_exhausted(caps, sum(len(r['text'].encode()) for r in batch)*4+len(batch)*spec['dimensions']*8):
                        self._pause_limit(db,run,'disk_limit')
                        return {'state':'paused'}
                    for row,vector in zip(batch,values):
                        self.vectors.put(namespace_id,spec['dimensions'],source['id'],sid,row['id'],row['content_hash'],vector)
                    tokens += charged
                    semantic_skipped += skipped
                    context_skipped += context_skips
                    saved = self._checkpoint_with_retry(db,run['id'],
                        {'embedded':end,'tokens':tokens,'semantic_skipped':semantic_skipped,'context_skipped':context_skipped},
                        progressed=bool(batch))
                    db.execute('UPDATE index_runs SET checkpoint_json=?,updated_at=? WHERE id=?',
                        (encoded(saved),self.clock(),run['id']))
                offset = end
            semantic_count = self.vectors.count(namespace_id,sid)
            if semantic_count + semantic_skipped != len(rows):
                raise IndexError('vector_incomplete')
            if not semantic_count:
                namespace_id = None
            degradation = 'embedding_context_limit' if context_skipped else 'embedding_token_limit' if semantic_skipped else None
        elif run['profile_version_id']:
            degradation = 'vector_unavailable'
        with self.store.transaction() as db:
            run,source = self._fence(db,job,context)
            files = list(db.execute('SELECT path,source_hash,line_count FROM snapshot_files WHERE snapshot_id=? ORDER BY path',(sid,)))
            if any(excluded(f['path'],source['exclusions']) for f in files):
                raise IndexError('selection_changed')
            evidence = []
            for f in files:
                if Path(f['path']).name.lower() in ('readme.md','pyproject.toml','package.json','cargo.toml','makefile') and f['line_count']:
                    evidence.append(dict(repository_id=source['id'],snapshot_id=sid,path=f['path'],start_line=1,end_line=min(20,f['line_count'])))
            snapshot = self._one(db,'repository_snapshots',sid)
            for item in evidence:
                item['revision'] = snapshot['revision']
            summary = 'Observed repository files: ' + ', '.join(e['path'] for e in evidence[:10]) if evidence else 'No README or manifest evidence retained.'
            observations = []
            for item in evidence[:10]:
                text = db.execute('SELECT text FROM snapshot_files WHERE snapshot_id=? AND path=?',(sid,item['path'])).fetchone()[0]
                for line in text.splitlines()[:item['end_line']]:
                    if re.match(r'^#{1,3}\s+\S',line) or re.match(r'^\s*(?:"?(?:name|description)"?)\s*[:=]',line):
                        observations.append(line.strip()[:200])
            if observations:
                summary += ' Observed headings/metadata (untrusted source quotations): ' + encoded(observations)[:2000]
            summary += ' Architecture, runtime behavior and validation commands have not been inferred.'
            db.execute('INSERT OR REPLACE INTO repository_summaries(snapshot_id,text,evidence_json,generation_json,created_at) VALUES(?,?,?,?,?)',(sid,summary,encoded(evidence),encoded({'method':'deterministic-v1','gaps':['architecture','validation_commands']}),self.clock()))
            coverage = json.loads(snapshot['coverage_json'])
            if degradation in ('embedding_token_limit','embedding_context_limit'):
                coverage['partial'] = True
                skips = coverage.setdefault('skipped',{})
                if semantic_skipped > context_skipped:
                    skips['embedding_token_limit'] = semantic_skipped-context_skipped
                if context_skipped:
                    skips['embedding_context_limit'] = context_skipped
            coverage.update(mode='hybrid' if namespace_id else 'lexical_only',degradation_code=degradation,embedding_tokens=tokens if run['profile_version_id'] and self.vectors.available else 0,
                            token_accounting='provider_reported_or_utf8_div4_estimate',
                            semantic_chunks=semantic_count if run['profile_version_id'] and self.vectors.available else 0)
            manifest = digest(encoded([list(f) for f in files]))
            if source['active_snapshot_id']:
                db.execute("UPDATE repository_snapshots SET state='retained' WHERE id=?",(source['active_snapshot_id'],))
            db.execute("UPDATE repository_snapshots SET state='ready',namespace_id=?,manifest_hash=?,coverage_json=?,profile_text=?,profile_evidence_json=?,activated_at=? WHERE id=?",(namespace_id,manifest,encoded(coverage),summary,encoded(evidence),self.clock(),sid))
            db.execute('UPDATE index_sources SET active_snapshot_id=? WHERE id=?',(sid,source['id']))
            db.execute("UPDATE index_runs SET state='completed',phase='completed',progress_json=?,updated_at=? WHERE id=?",(encoded(coverage),self.clock(),run['id']))
        return {'run_id':run['id'],'state':'completed','snapshot_id':sid}

    def _scopes(self, repository_ids, snapshot_ids):
        if not isinstance(repository_ids,(list,tuple)) or not 1 <= len(repository_ids) <= 50 or len(set(repository_ids)) != len(repository_ids):
            raise ValueError('Invalid repository scopes.')
        if snapshot_ids is not None and (not isinstance(snapshot_ids,dict) or set(snapshot_ids)-set(repository_ids)):
            raise ValueError('Invalid snapshot scopes.')
        scopes = {}
        with self.store.read() as db:
            for ident in repository_ids:
                source = self._source(db,ident)
                self._require(source)
                sid = (snapshot_ids or {}).get(ident,source['active_snapshot_id'])
                if sid is None:
                    continue
                snap = self._one(db,'repository_snapshots',sid)
                if snap['repository_id'] != ident or snap['state'] not in ('ready','retained'):
                    raise IndexError('snapshot_unavailable')
                scopes[ident] = (source,snap)
        return scopes

    def search(self, query, repository_ids, snapshot_ids=None, limit=10, context_token_budget=4000, semantic=True):
        if not isinstance(query,str) or not 1 <= len(query) <= 4000 or type(limit) is not int or not 1 <= limit <= 100 or type(context_token_budget) is not int or not 1 <= context_token_budget <= 32000 or type(semantic) is not bool:
            raise ValueError('Invalid search request.')
        scopes = self._scopes(repository_ids,snapshot_ids)
        ids = [snap['id'] for _,snap in scopes.values()]
        ranked, methods, rows_by_id = {}, {}, {}
        tokens = re.findall(r'\w+',query,flags=re.UNICODE)[:32]
        expression = ' OR '.join('"'+t+'"' for t in tokens)
        if ids:
            with self.store.read() as db:
                placeholders = ','.join('?' for _ in ids)
                if expression:
                    rows = db.execute('SELECT c.*,s.repository_id,s.revision,bm25(code_chunks_fts) AS score FROM code_chunks_fts JOIN code_chunks c ON c.id=code_chunks_fts.chunk_id JOIN repository_snapshots s ON s.id=c.snapshot_id WHERE code_chunks_fts MATCH ? AND c.snapshot_id IN ('+placeholders+') ORDER BY score,c.id LIMIT 1000',(expression,*ids)).fetchall()
                    hidden = {}  # Chunks of one file share the same exclusion verdict.
                    for row in rows:
                        key = (row['repository_id'],row['path'])
                        if key not in hidden:
                            hidden[key] = excluded(row['path'],scopes[row['repository_id']][0]['exclusions'])
                        if hidden[key]:
                            continue
                        rank = len(ranked)+1
                        ranked[row['id']] = 1/(60+rank)
                        methods[row['id']] = ['lexical']
                        rows_by_id[row['id']] = dict(row)
        degradation = None if semantic else 'semantic_disabled'
        semantic_used = False
        if semantic and ids:
            namespaces = {}
            for _,snap in scopes.values():
                code = json.loads(snap['coverage_json']).get('degradation_code')
                if code:
                    degradation = code
                if snap['namespace_id']:
                    namespaces.setdefault(snap['namespace_id'],[]).append(snap['id'])
            if not namespaces:
                degradation = 'embedding_unassigned'
            elif not self.vectors.available:
                degradation = 'vector_unavailable'
            else:
                for namespace,snapshot_list in namespaces.items():
                    try:
                        # Validate all scopes again immediately before query transmission.
                        self._scopes(repository_ids,snapshot_ids)
                        with self.store.read() as db:
                            ns = self._one(db,'embedding_namespaces',namespace)
                        result = self.providers.embed(ns['profile_version_id'],[query],contains_source=False)
                        spec = self.providers.embedding_namespace(ns['profile_version_id'])
                        if result['namespace']['key'] != spec['key'] or digest(encoded([spec['key'],ns['profile_version_id'],spec['dimensions'],'none',ns['chunker_version']])) != namespace:
                            raise IndexError('namespace_mismatch')
                        hits = self.vectors.query(namespace,result['vectors'][0],snapshot_list,min(100,limit*4))
                        semantic_used = True
                        with self.store.read() as db:
                            for rank,(cid,_) in enumerate(hits,1):
                                row = db.execute('SELECT c.*,s.repository_id,s.revision FROM code_chunks c JOIN repository_snapshots s ON s.id=c.snapshot_id WHERE c.id=?',(cid,)).fetchone()
                                if row is None or row['snapshot_id'] not in snapshot_list:
                                    continue
                                if excluded(row['path'],scopes[row['repository_id']][0]['exclusions']):
                                    continue
                                ranked[cid] = ranked.get(cid,0)+1/(60+rank)
                                methods.setdefault(cid,[]).append('semantic')
                                rows_by_id[cid] = dict(row)
                    except Exception:
                        degradation = 'semantic_unavailable'
        # Revalidate eligibility/exclusions after provider I/O, under Store lock.
        result_hits, used, truncated = [], 0, False
        with self.store.read() as db:
            current = self._scopes(repository_ids,snapshot_ids)
            for cid in sorted(ranked,key=lambda c:(-ranked[c],c)):
                row = rows_by_id[cid]
                if row['repository_id'] not in current or row['snapshot_id'] != current[row['repository_id']][1]['id'] or excluded(row['path'],current[row['repository_id']][0]['exclusions']):
                    continue
                cost = estimated_tokens(row['text'])
                if used+cost>context_token_budget:
                    truncated = True
                    continue
                if len(result_hits)>=limit:
                    truncated = True
                    break
                used += cost
                result_hits.append(dict(chunk_id=cid,**{k:row[k] for k in ('repository_id','snapshot_id','revision','path','start_line','end_line','text')},rank=len(result_hits)+1,methods=methods[cid]))
            snapshots = {ident:dict(id=snap['id'],revision=snap['revision'],revision_kind=snap['revision_kind'],coverage=json.loads(snap['coverage_json'])) for ident,(_,snap) in current.items()}
        return dict(mode='hybrid' if semantic_used else 'lexical_only',degradation_code=degradation,hits=result_hits,snapshots=snapshots,truncated=truncated)

    def source(self, repository_id, snapshot_id, path, start_line, end_line, context_lines=5):
        if not safe_path(path) or any(type(v) is not int for v in (start_line,end_line,context_lines)) or not 1 <= start_line <= end_line or end_line-start_line>=500 or not 0 <= context_lines <= 50:
            raise ValueError('Invalid source range.')
        with self.store.read() as db:
            scopes = self._scopes([repository_id],{repository_id:snapshot_id})
            source,snapshot = scopes[repository_id]
            if excluded(path,source['exclusions']):
                raise IndexError('source_unavailable')
            row = db.execute('SELECT text FROM snapshot_files WHERE snapshot_id=? AND path=?',(snapshot_id,path)).fetchone()
            if row is None:
                raise NotFound('Source unavailable.')
            lines = row[0].splitlines()
            if end_line>len(lines):
                raise ValueError('Source range outside retained file.')
            first,last = max(1,start_line-context_lines),min(len(lines),end_line+context_lines)
            return dict(repository_id=repository_id,snapshot_id=snapshot_id,revision=snapshot['revision'],path=path,first_line=first,lines=lines[first-1:last],total_lines=len(lines))

    def catalog_candidates(self, query, pinned_repository_ids=(), limit=20):
        if not isinstance(query,str) or len(query)>4000 or type(limit) is not int or not 1 <= limit <= 100 or not isinstance(pinned_repository_ids,(list,tuple)) or len(pinned_repository_ids)>50:
            raise ValueError('Invalid candidate query.')
        # Catalog metadata is safe without a source snapshot; source-derived summaries
        # are visible only if all their current evidence paths remain allowed.
        if self.github:
            cursor = None
            while True:
                page = self.github.list_repositories(cursor=cursor,limit=100)
                for repo in page['items']:
                    if self.github.is_eligible(repo['id']):
                        self._ensure_source(repo['id'])
                cursor = page['next_cursor']
                if cursor is None:
                    break
        terms = set(re.findall(r'\w+',query.lower()))
        candidates = []
        with self.store.read() as db:
            sources = [self._source(db,r[0]) for r in db.execute('SELECT id FROM index_sources')]
            for source in sources:
                if not self._eligible(source):
                    continue
                fields = {'name':source['name']}
                if source['kind'] == 'github':
                    repo = self.github.get_repository(source['id'])
                    fields.update(description=repo['description'],topics=' '.join(repo['topics']))
                if source['active_snapshot_id']:
                    snap = self._one(db,'repository_snapshots',source['active_snapshot_id'])
                    evidence = json.loads(snap['profile_evidence_json'])
                    if evidence and all(not excluded(e['path'],source['exclusions']) for e in evidence):
                        fields['profile'] = snap['profile_text']
                reasons = [dict(field=key,terms=sorted(terms & set(re.findall(r'\w+',value.lower())))) for key,value in fields.items() if terms & set(re.findall(r'\w+',value.lower()))]
                score = sum(len(r['terms']) for r in reasons)
                pinned = source['id'] in pinned_repository_ids
                if score or pinned:
                    candidates.append(dict(repository_id=source['id'],name=source['name'],score=score,pinned=pinned,reasons=reasons,state=self.get_state(source['id'])))
        return sorted(candidates,key=lambda c:(not c['pinned'],-c['score'],c['repository_id']))[:limit]

    def purge_repository(self, repository_id):
        # First remove visibility. A durable tombstone makes partial vector cleanup
        # retryable even if either process/database fails before final metadata deletion.
        with self.store.transaction() as db:
            row = db.execute('SELECT kind FROM index_sources WHERE id=?',(repository_id,)).fetchone()
            if row:
                db.execute("UPDATE index_sources SET active_snapshot_id=NULL,index_enabled=0,metadata_json=?,updated_at=? WHERE id=?",(encoded({'purging':True}),self.clock(),repository_id))
                db.execute("UPDATE index_runs SET state='cancelled',updated_at=? WHERE repository_id=? AND state IN ('queued','running','waiting','paused')",(self.clock(),repository_id))
        # Always attempt vector deletion, including orphan staging after a crash.
        self.vectors.purge(repository_id)
        with self.store.transaction() as db:
            # Retain the disabled registry identity/local path, not derived source.
            db.execute('DELETE FROM index_runs WHERE repository_id=?',(repository_id,))
            db.execute('DELETE FROM repository_snapshots WHERE repository_id=?',(repository_id,))
            db.execute("UPDATE index_sources SET metadata_json=?,index_enabled=0 WHERE id=?",(encoded({'purged':True}),repository_id))
        return {'repository_id':repository_id,'state':'completed'}
