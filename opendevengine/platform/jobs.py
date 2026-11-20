"""Durable queue; uncertain external effects require explicit reconciliation."""
from dataclasses import dataclass, field, fields
import json
import re
from typing import Literal
from .clock import now_ms, new_id, format_ts
from .errors import NotFound, LeaseLost, InvalidTransition, IdempotencyConflict

JobState = Literal['queued', 'leased', 'succeeded', 'failed', 'cancelled', 'needs_reconciliation']
STATES = ('queued', 'leased', 'succeeded', 'failed', 'cancelled', 'needs_reconciliation')
TERMINAL = frozenset({'succeeded', 'failed', 'cancelled'})
DEFAULT_LEASE_MS = 30_000
BACKOFF_BASE_MS, BACKOFF_CAP_MS = 2_000, 300_000
ERROR_CODES = frozenset({'transient', 'rate_limited', 'timeout', 'auth', 'invalid_input', 'unsupported', 'unknown_outcome', 'lease_expired', 'internal'})


def json_object(value):
    from collections.abc import Mapping
    if not isinstance(value, Mapping):
        raise ValueError('Expected a JSON object.')
    try:
        encoded = json.dumps(dict(value), sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(encoded.encode('utf-8')) > 16384:
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise ValueError('Invalid JSON object.') from None
    return encoded


def lease_duration(value):
    if type(value) is not int or not 1000 <= value <= 600000:
        raise ValueError('Invalid lease duration.')


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    state: JobState
    effect: Literal['safe', 'external']
    priority: int
    payload: dict
    idempotency_key: str | None
    attempts: int
    max_attempts: int
    run_after: int
    lease_owner: str | None
    lease_expires_at: int | None
    cancel_requested: bool
    checkpoint: dict | None
    result: dict | None
    last_error_code: str | None
    last_error_message: str | None
    created_at: int
    updated_at: int
    finished_at: int | None


@dataclass(frozen=True)
class Lease:
    job: Job
    token: str = field(repr=False)
    owner: str
    expires_at: int


@dataclass(frozen=True)
class HeartbeatResult:
    expires_at: int
    cancel_requested: bool


def job_from(row):
    values = {f.name: row[f.name] for f in fields(Job)}
    for key in ('payload', 'checkpoint', 'result'):
        values[key] = json.loads(values[key]) if values[key] is not None else None
    values['cancel_requested'] = bool(values['cancel_requested'])
    return Job(**values)


class JobQueue:
    def __init__(self, store, *, clock=now_ms):
        self.store, self.clock = store, clock

    def _row(self, conn, job_id):
        row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise NotFound('Job not found.')
        return row

    def get(self, job_id):
        with self.store.read() as conn:
            return job_from(self._row(conn, job_id))

    def enqueue(self, kind, payload, *, effect='safe', priority=0, max_attempts=3, idempotency_key=None, run_after=None):
        with self.store.transaction() as conn:
            return self.enqueue_in_transaction(conn, kind, payload, effect=effect, priority=priority,
                                               max_attempts=max_attempts, idempotency_key=idempotency_key,
                                               run_after=run_after)

    def enqueue_in_transaction(self, conn, kind, payload, *, effect='safe', priority=0, max_attempts=3, idempotency_key=None, run_after=None):
        """Caller owns Store.transaction; run records and jobs commit or roll back together."""
        if conn is not self.store.conn or not conn.in_transaction:
            raise ValueError('An owned active transaction is required.')
        if not isinstance(kind, str) or not re.fullmatch(r'[a-z][a-z0-9_.]{1,63}', kind):
            raise ValueError('Invalid job kind.')
        if effect not in ('safe', 'external') or type(priority) is not int or type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise ValueError('Invalid job options.')
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key):
            raise ValueError('Invalid idempotency key.')
        if run_after is not None and type(run_after) is not int:
            raise ValueError('Invalid schedule.')
        payload = json_object(payload)
        if idempotency_key is not None:
            existing = conn.execute('SELECT * FROM jobs WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if existing:
                if (existing['kind'], existing['payload'], existing['effect']) != (kind, payload, effect):
                    raise IdempotencyConflict('Idempotency key has another request.')
                return job_from(existing)
        now, job_id = self.clock(), new_id('job')
        conn.execute('INSERT INTO jobs (id,kind,state,effect,priority,payload,idempotency_key,max_attempts,run_after,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                     (job_id, kind, 'queued', effect, priority, payload, idempotency_key, max_attempts, now if run_after is None else run_after, now, now))
        return job_from(self._row(conn, job_id))

    def _update(self, conn, job_id, **values):
        values['updated_at'] = self.clock()
        conn.execute('UPDATE jobs SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', (*values.values(), job_id))
        return job_from(self._row(conn, job_id))

    def _finish(self, conn, job_id, state, **values):
        values.update(state=state, lease_token=None, lease_owner=None, lease_expires_at=None)
        if state in ('succeeded', 'cancelled'):
            values.update(last_error_code=None, last_error_message=None)
        if state in TERMINAL:
            values['finished_at'] = self.clock()
        return self._update(conn, job_id, **values)

    def _owned(self, conn, lease):
        row = conn.execute('SELECT * FROM jobs WHERE id=?', (lease.job.id,)).fetchone()
        if row is None or row['state'] != 'leased' or row['lease_token'] != lease.token:
            raise LeaseLost('Job lease is no longer owned.')
        return row

    def _reconcile(self, conn):
        rows = conn.execute("SELECT * FROM jobs WHERE state='leased' AND lease_expires_at<? ORDER BY id", (self.clock(),)).fetchall()
        for row in rows:
            state = ('needs_reconciliation' if row['effect'] == 'external' else
                     'cancelled' if row['cancel_requested'] else
                     'queued' if row['attempts'] < row['max_attempts'] else 'failed')
            self._finish(conn, row['id'], state, run_after=self.clock(), last_error_code='lease_expired')
        return [row['id'] for row in rows]

    def reconcile_expired(self):
        with self.store.transaction() as conn:
            return self._reconcile(conn)

    def claim(self, worker_id, *, kinds=None, lease_ms=DEFAULT_LEASE_MS):
        lease_duration(lease_ms)
        if not isinstance(worker_id, str) or not worker_id:
            raise ValueError('Invalid worker ID.')
        with self.store.transaction() as conn:
            self._reconcile(conn)
            params = [self.clock()]
            query = "SELECT * FROM jobs WHERE state='queued' AND run_after<=?"
            if kinds is not None:
                kinds = tuple(kinds)
                if not kinds:
                    return None
                query += ' AND kind IN (' + ','.join('?' for _ in kinds) + ')'
                params.extend(kinds)
            row = conn.execute(query + ' ORDER BY priority DESC, run_after, created_at, id LIMIT 1', params).fetchone()
            if row is None:
                return None
            token, expires = new_id('job'), self.clock() + lease_ms
            job = self._update(conn, row['id'], state='leased', attempts=row['attempts'] + 1, lease_owner=worker_id, lease_token=token, lease_expires_at=expires)
            return Lease(job, token, worker_id, expires)

    def heartbeat(self, lease, *, checkpoint=None, lease_ms=DEFAULT_LEASE_MS):
        lease_duration(lease_ms)
        encoded = json_object(checkpoint) if checkpoint is not None else None
        with self.store.transaction() as conn:
            row = self._owned(conn, lease)
            values = {'lease_expires_at': self.clock() + lease_ms}
            if checkpoint is not None:
                values['checkpoint'] = encoded
            job = self._update(conn, row['id'], **values)
            return HeartbeatResult(job.lease_expires_at, job.cancel_requested)

    def release_safe(self, lease):
        """Yield owned safe work on shutdown without consuming a failure attempt."""
        with self.store.transaction() as conn:
            row = self._owned(conn, lease)
            if row['effect'] != 'safe':
                raise InvalidTransition('External work requires reconciliation.')
            if row['cancel_requested']:
                return self._finish(conn, row['id'], 'cancelled')
            return self._finish(conn, row['id'], 'queued', run_after=self.clock(),
                                attempts=max(0, row['attempts'] - 1))

    def complete(self, lease, *, result=None):
        encoded = json_object(result) if result is not None else None
        with self.store.transaction() as conn:
            row = self._owned(conn, lease)
            return self._finish(conn, row['id'], 'succeeded', result=encoded)

    def _error(self, code, message):
        if code not in ERROR_CODES or not isinstance(message, str):
            raise ValueError('Invalid job error.')
        return {'last_error_code': code, 'last_error_message': message[:500]}

    def fail(self, lease, *, code, message='', retryable):
        values = self._error(code, message)
        with self.store.transaction() as conn:
            row = self._owned(conn, lease)
            if retryable and row['cancel_requested']:
                return self._finish(conn, row['id'], 'cancelled')
            retry = retryable and row['attempts'] < row['max_attempts']
            if retry:
                values['run_after'] = self.clock() + min(BACKOFF_BASE_MS * 2 ** (row['attempts'] - 1), BACKOFF_CAP_MS)
            return self._finish(conn, row['id'], 'queued' if retry else 'failed', **values)

    def mark_unknown_outcome(self, lease, *, code, message=''):
        values = self._error(code, message)
        with self.store.transaction() as conn:
            row = self._owned(conn, lease)
            return self._finish(conn, row['id'], 'needs_reconciliation', **values)

    def acknowledge_cancel(self, lease):
        with self.store.transaction() as conn:
            row = self._owned(conn, lease)
            if not row['cancel_requested']:
                raise InvalidTransition('Cancellation was not requested.')
            return self._finish(conn, row['id'], 'cancelled')

    def cancel(self, job_id):
        with self.store.transaction() as conn:
            row = self._row(conn, job_id)
            if row['state'] == 'needs_reconciliation':
                raise InvalidTransition('Reconcile the job before cancellation.')
            if row['state'] in TERMINAL:
                return job_from(row)
            if row['state'] == 'leased':
                return self._update(conn, job_id, cancel_requested=1)
            return self._finish(conn, job_id, 'cancelled')

    def resolve_reconciliation(self, job_id, *, outcome):
        with self.store.transaction() as conn:
            row = self._row(conn, job_id)
            if row['state'] != 'needs_reconciliation' or outcome not in ('succeeded', 'failed', 'requeue'):
                raise InvalidTransition('Invalid reconciliation outcome.')
            if outcome == 'requeue' and row['cancel_requested']:
                raise InvalidTransition('Cancellation is pending; resolve a known outcome.')
            if outcome == 'requeue' and row['attempts'] >= row['max_attempts']:
                raise InvalidTransition('Job attempts are exhausted.')
            return self._finish(conn, job_id, 'queued' if outcome == 'requeue' else outcome, run_after=self.clock())

    def counts(self):
        with self.store.read() as conn:
            counts = dict.fromkeys(STATES, 0)
            counts.update(dict(conn.execute('SELECT state,count(*) FROM jobs GROUP BY state')))
            return counts

    def recent_failures(self, *, limit=5):
        if type(limit) is not int or limit < 0:
            raise ValueError('Invalid failure limit.')
        with self.store.read() as conn:
            rows = conn.execute("SELECT id,kind,last_error_code,updated_at FROM jobs WHERE state IN ('failed','needs_reconciliation') AND last_error_code IS NOT NULL ORDER BY updated_at DESC,id LIMIT ?", (limit,)).fetchall()
            return [dict(job_id=r[0], kind=r[1], code=r[2], at=format_ts(r[3])) for r in rows]
