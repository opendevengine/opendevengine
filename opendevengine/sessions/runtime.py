"""Dedicated runtime worker, data-directory leadership and restart recovery.

Only the process holding the runtime leadership lock may run session/test jobs or
recover/kill owned runners; another application process on the same data directory
reports ``runtime_in_use`` and never touches live work. Recovery never infers success
from an absent process, never reruns an attempt automatically and resolves an uncertain
job only after verified containment.
"""
import fcntl
import os
from pathlib import Path
import secrets
import threading

from opendevengine.execution.runner import RunnerError, RunnerHandle
from opendevengine.platform.errors import PlatformError, StorageBusy
from opendevengine.platform.worker import Worker
from .processes import contain_recorded
from .service import ACTIVE, TERMINAL, _loads

_MEMORY_LEADERS = set()
_MEMORY_LOCK = threading.Lock()


class Leadership:
    def __init__(self, store, data_dir=None):
        self.store, self.fd, self.key = store, None, None
        if data_dir is None and store.persistent:
            with store.read() as db:
                data_dir = str(Path(db.execute('PRAGMA database_list').fetchone()[2]).parent)
        self.path = Path(data_dir) / 'runtime.lock' if data_dir is not None else None

    def acquire(self):
        if self.fd is not None or self.key is not None:
            return True
        if self.path is None:
            with _MEMORY_LOCK:
                if id(self.store) in _MEMORY_LEADERS:
                    return False
                _MEMORY_LEADERS.add(id(self.store))
                self.key = id(self.store)
            return True
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self):
        if self.fd is not None:
            os.close(self.fd)  # closing releases the advisory lock
            self.fd = None
        if self.key is not None:
            with _MEMORY_LOCK:
                _MEMORY_LEADERS.discard(self.key)
            self.key = None

    @property
    def held(self):
        return self.fd is not None or self.key is not None


class Runtime:
    def __init__(self, service, *, data_dir=None):
        self.svc = service
        self.leadership = Leadership(service.store, data_dir)
        self.token = secrets.token_hex(6)
        self.threads = []
        self.lock = threading.Lock()
        self.state = 'stopped'
        self.last_recovery = None

    def worker_ids(self):
        return {'runtime:%s:%d' % (self.token, n) for n in range(self.svc.concurrency)}

    def status(self):
        if self.state == 'running':
            return {'status': 'running', 'workers': len(self.threads), 'last_recovery': self.last_recovery}
        if self.state == 'runtime_in_use':
            return {'status': 'runtime_in_use'}
        return {'status': 'stopped'}

    def handlers(self):
        handlers = {'session.run': self.svc.run_session}
        run_test = getattr(self.svc.execution_profiles, 'run_test', None)
        if callable(run_test):
            handlers['execution.test'] = run_test
        return handlers

    def start(self):
        with self.lock:
            if self.state == 'running':
                return self.status()
            if not self.leadership.acquire():
                self.state = 'runtime_in_use'
                return self.status()
            self.svc._shutdown.clear()
            try:
                self.recover()
            except Exception:
                self.leadership.release()
                self.state = 'stopped'
                raise
            self.state = 'running'
            self.threads = []
            for worker_id in sorted(self.worker_ids()):
                thread = threading.Thread(target=self._loop, args=(worker_id,), name='opendevengine-' + worker_id, daemon=True)
                self.threads.append(thread)
                thread.start()
            return self.status()

    def _loop(self, worker_id):
        worker = Worker(self.svc.jobs, self.handlers(), worker_id=worker_id, lease_ms=self.svc.lease_ms)
        maintenance = worker_id.endswith(':0')
        ticks = 0
        while not self.svc._shutdown.is_set():
            ran = False
            try:
                ran = worker.run_once()
            except StorageBusy:
                ran = False
            except Exception:
                ran = False  # a handler bug cannot stop the dedicated runtime worker
            ticks += 1
            if maintenance and ticks % 20 == 0:
                try:
                    self.reconcile()
                except Exception:
                    pass
            if not ran:
                self.svc._shutdown.wait(self.svc.poll_interval)

    def close(self, timeout=20.0):
        with self.lock:
            if self.state != 'running':
                if self.state == 'runtime_in_use':
                    self.state = 'stopped'
                return self.status()
            self.svc._shutdown.set()
            with self.svc._active_lock:
                attempts = list(self.svc._active.values())
            for attempt in attempts:
                attempt.terminate('shutdown', kill=True)
            for thread in self.threads:
                thread.join(timeout)
            alive = [t for t in self.threads if t.is_alive()]
            self.threads = alive
            self.state = 'stopped'
            if not alive:
                self.leadership.release()
            return {'status': 'stopped', 'workers_alive': len(alive)}

    # ------------------------------------------------------------- recovery
    def _runner(self):
        try:
            return self.svc.runner_factory(self.svc.instance_id())
        except RunnerError:
            return None

    def recover(self):
        """Contain and truthfully classify work left by a previous runtime process."""
        if not self.leadership.held:
            return {'status': 'runtime_in_use'}
        result = self.reconcile(startup=True)
        self.last_recovery = result
        return result

    def reconcile(self, *, startup=False):
        svc = self.svc
        svc.jobs.reconcile_expired()
        live = svc.active_ids()
        ours = self.worker_ids()
        with svc.store.read() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT s.*,j.state AS job_state,j.lease_owner AS job_owner FROM sessions s LEFT JOIN jobs j ON j.id=s.job_id "
                'WHERE s.state IN (%s)' % ','.join('?' * len(ACTIVE)), ACTIVE)]
            pending = [dict(r) for r in db.execute(
                "SELECT r.*,s.state AS session_state,s.job_id FROM session_runners r JOIN sessions s ON s.id=r.session_id "
                "WHERE r.state IN ('provisioning','running','terminating','containment_failed') AND s.state IN (%s)"
                % ','.join('?' * len(TERMINAL)), TERMINAL)]
        stale = []
        for row in rows:
            if row['id'] in live:
                continue
            if row['job_state'] == 'leased' and (row['job_owner'] in ours or not startup):
                continue  # a live local worker owns it (or, outside startup, its lease is still valid)
            if row['state'] == 'queued' and row['job_state'] == 'queued':
                continue  # never started; it will run normally
            stale.append(row)
        report = {'interrupted': [], 'cancelled': [], 'containment_failed': [], 'orphans_removed': 0,
                  'profile_tests': []}
        sweep = startup
        if startup and not (stale or pending):
            # The startup orphan sweep needs Docker. Containers are only ever provisioned for a session
            # or an execution-profile test; with neither recorded there is nothing of ours to sweep, so
            # do not wait on a possibly unresponsive daemon. `startup` keeps its lease semantics below.
            with svc.store.read() as db:
                sweep = (db.execute('SELECT 1 FROM sessions LIMIT 1').fetchone() is not None or
                         db.execute('SELECT 1 FROM execution_profile_checks LIMIT 1').fetchone() is not None)
        runner = self._runner() if (stale or pending or sweep) else None
        owned = None
        if runner is not None:
            try:
                owned = runner.list_owned()
            except RunnerError:
                owned = None
        for row in stale:
            self._recover_session(row, runner, owned, report)
        for row in pending:
            self._recover_runner(row, runner, owned, report)
        tests = self._recover_tests(runner, owned, startup, report)
        if owned is not None and runner is not None:
            with svc.store.read() as db:
                active = {r[0] for r in db.execute('SELECT id FROM sessions WHERE state IN (%s)' % ','.join('?' * len(ACTIVE)), ACTIVE)}
                # Profile-test containers use session_id=job.id. Any queued/leased test job,
                # and every test the producer still reports for recovery, is never swept here.
                tests |= {r[0] for r in db.execute("SELECT id FROM jobs WHERE kind='execution.test' "
                                                   "AND state IN ('queued','leased','needs_reconciliation')")}
            for handle in owned:
                if handle.session_id in tests:
                    continue
                if handle.session_id in active and handle.session_id in svc.active_ids():
                    continue
                if handle.session_id in active and handle.session_id not in {r['id'] for r in stale}:
                    continue
                if self._stop_handle(runner, handle):
                    report['orphans_removed'] += 1
        self._resolve_jobs()
        return report

    def _recover_tests(self, runner, owned, startup, report):
        """Contain dead profile-test runners and resolve them through the producer.

        Returns job ids that the orphan sweep must not touch. A live local test (or,
        outside startup, any unexpired lease) is protected. A dead-owner lease at
        startup is contained only with a recorded process identity; the queue job is
        resolved by the producer only after verified containment, never requeued.
        """
        profiles = self.svc.execution_profiles
        pending_tests = getattr(profiles, 'pending_runtime_tests', None)
        reconcile = getattr(profiles, 'reconcile_test', None)
        if not callable(pending_tests) or not callable(reconcile):
            return set()
        try:
            items = pending_tests()
        except PlatformError:
            return set()
        ours, now, protected = self.worker_ids(), self.svc.clock(), set()
        for item in items:
            job_id, private = item['job_id'], item.get('private_runtime') or {}
            protected.add(job_id)
            try:
                job = self.svc.jobs.get(job_id)
            except PlatformError:
                continue
            if job.state in ('queued', 'succeeded'):
                continue
            if job.state == 'leased' and (job.lease_owner in ours or (not startup and job.lease_expires_at > now)):
                continue  # live work; never stolen from metadata alone
            if private.get('instance_id') not in (None, self.svc.instance_id()):
                continue  # another runtime instance's resources
            if job.state == 'leased' and private.get('harness_pid') and not private.get('harness_start'):
                report['profile_tests'].append({'job_id': job_id, 'confirmed': False, 'reason': 'identity_unknown'})
                continue
            harness = contain_recorded(private.get('harness_pid'), private.get('harness_start'))
            if owned is None or runner is None:
                container = 'unverified'
            else:
                matches = [h for h in owned if h.session_id == job_id]
                container = 'terminated' if all(self._stop_handle(runner, h) for h in matches) else 'unverified'
            confirmed = harness in ('absent', 'terminated') and container in ('absent', 'terminated')
            entry = {'job_id': job_id, 'confirmed': confirmed, 'harness': harness, 'container': container}
            if startup and confirmed and job.state == 'leased' and job.lease_owner not in ours:
                # Leadership proves the previous owner is dead and its resources are contained: fence the
                # unexpired lease as session recovery does, so the check resolves now, not at lease expiry.
                with self.svc.store.transaction() as db:
                    db.execute("UPDATE jobs SET state='needs_reconciliation',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,"
                               "last_error_code='lease_expired',last_error_message='Runtime restarted',updated_at=? "
                               "WHERE id=? AND state='leased' AND lease_owner=?", (self.svc.clock(), job_id, job.lease_owner))
                job = self.svc.jobs.get(job_id)
            if job.state in ('leased', 'needs_reconciliation'):
                try:
                    reconcile(item['version_id'], job_id, containment_confirmed=confirmed)
                    entry['reconciled'] = True
                except PlatformError as exc:
                    entry['reconciled'] = False
                    entry['reason'] = getattr(exc, 'code', 'reconcile_failed')
            report['profile_tests'].append(entry)
        return protected

    def _stop_handle(self, runner, handle):
        try:
            if runner.stop(handle).get('confirmed'):
                runner.discard(handle)
                return True
        except RunnerError:
            pass
        return False

    def _contain(self, row, runner, owned):
        """Return (confirmed, details) for a session's recorded container and harness."""
        with self.svc.store.read() as db:
            rec = db.execute('SELECT * FROM session_runners WHERE session_id=?', (row['id'],)).fetchone()
        if rec is None:
            # Nothing was provisioned (runner rows precede container creation), but a
            # container may still exist only if Docker created one before recording.
            return True, {'container': 'not_provisioned', 'harness': 'not_started'}
        harness = contain_recorded(rec['harness_pid'], rec['harness_start'])
        container = 'unverified'
        if owned is None or runner is None:
            container = 'unverified'
        else:
            matches = [h for h in owned if h.session_id == row['id']]
            if not matches:
                container = 'absent'
            else:
                container = 'terminated' if all(self._stop_handle(runner, h) for h in matches) else 'unverified'
        ok = harness in ('absent', 'terminated') and container in ('absent', 'terminated')
        return ok, {'container': container, 'harness': harness}

    def _recover_session(self, row, runner, owned, report):
        svc = self.svc
        confirmed, details = self._contain(row, runner, owned)
        with svc.store.transaction() as db:
            current = dict(db.execute('SELECT * FROM sessions WHERE id=?', (row['id'],)).fetchone())
            if current['state'] in TERMINAL:
                return
            job = db.execute('SELECT state FROM jobs WHERE id=?', (current['job_id'],)).fetchone()
            if job is not None and job['state'] == 'leased':
                # Leadership proves the previous lease owner is dead; never leave it runnable.
                db.execute("UPDATE jobs SET state='needs_reconciliation',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,"
                           "last_error_code='lease_expired',last_error_message='Runtime restarted',updated_at=? WHERE id=?",
                           (svc.clock(), current['job_id']))
            elif job is not None and job['state'] == 'queued':
                db.execute("UPDATE jobs SET state='cancelled',finished_at=?,updated_at=? WHERE id=?",
                           (svc.clock(), svc.clock(), current['job_id']))
            db.execute('UPDATE session_runners SET state=?,updated_at=? WHERE session_id=?',
                       ('terminated' if confirmed else 'containment_failed', svc.clock(), row['id']))
            svc.append_event(db, row['id'], 'containment', dict(details, confirmed=confirmed, source='recovery'))
            user_stop = current['stop_requested'] and (current['stop_reason'] or 'user_requested') == 'user_requested'
            if confirmed and user_stop:
                state, code = 'cancelled', None
            else:
                state, code = 'interrupted', 'runtime_interrupted' if confirmed else 'containment_failed'
            outcome = _loads(current['outcome_json'], {})
            outcome.update(result=state, edits_since_last_checkpoint_may_be_lost=True,
                           containment=dict(details, confirmed=confirmed))
            from .workspace import canonical
            svc.transition(db, current, state, phase=state, error_code=code,
                           stop_reason=current['stop_reason'] or ('runtime_interrupted' if state == 'interrupted' else None),
                           outcome_json=canonical(outcome))
        report['cancelled' if state == 'cancelled' else 'interrupted'].append(row['id'])
        if not confirmed:
            report['containment_failed'].append(row['id'])

    def _recover_runner(self, row, runner, owned, report):
        confirmed, details = self._contain({'id': row['session_id']}, runner, owned)
        with self.svc.store.transaction() as db:
            db.execute('UPDATE session_runners SET state=?,updated_at=? WHERE session_id=?',
                       ('terminated' if confirmed else 'containment_failed', self.svc.clock(), row['session_id']))
            self.svc.append_event(db, row['session_id'], 'containment', dict(details, confirmed=confirmed, source='recovery'))
        if not confirmed:
            report['containment_failed'].append(row['session_id'])

    def _resolve_jobs(self):
        """Uncertain session.run jobs become failed only once their session is terminal and contained."""
        svc = self.svc
        with svc.store.read() as db:
            ids = [r[0] for r in db.execute(
                "SELECT j.id FROM jobs j JOIN sessions s ON s.job_id=j.id LEFT JOIN session_runners r ON r.session_id=s.id "
                "WHERE j.kind='session.run' AND j.state='needs_reconciliation' AND s.state IN (%s) "
                "AND (r.state IS NULL OR r.state='terminated')" % ','.join('?' * len(TERMINAL)), TERMINAL)]
        for job_id in ids:
            try:
                svc.jobs.resolve_reconciliation(job_id, outcome='failed')
            except PlatformError:
                pass
