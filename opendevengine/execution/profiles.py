"""Immutable execution profiles and explicit, durable synthetic coding checks.

Reads use cached evidence only. Testing is an external, single-attempt job; neither
CLI discovery nor a claimed successful model response is a passing coding proof.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from functools import wraps
import json
import math
import os
import re
import sqlite3
import threading
import time

from opendevengine.platform.clock import new_id, now_ms
from opendevengine.platform.errors import LeaseLost, PlatformError
from opendevengine.platform.worker import CancelledError, JobInterrupted, UnknownOutcomeError
from .broker import ToolBroker, TOOL_NAMES
from .adapter_process import ProcessContainmentError
from .claude import AdapterError, ClaudeAdapter
from .runner import DockerRunner, RunnerError, validate_limits


AGENT_DEFAULTS = {
    'wall_seconds': 180, 'max_turns': 12, 'max_budget_usd': 1.0,
    'max_output_bytes': 524288,
}
_CAPABILITIES = frozenset({'headless', 'streaming', 'cancellation', 'resume',
                         'checkpoint', 'follow_up', 'ask_user', 'ask_user_requires_callback'})
_IMAGE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,254}$')
_IMAGE_ID = re.compile(r'^sha256:[0-9a-f]{64}$')
_IMAGE_COMPONENT = re.compile(r'^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$')
_IMAGE_TAG = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$')
_REGISTRY_HOST = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$')
_CLI_VERSION = re.compile(r'^[0-9][A-Za-z0-9 .()_+\-]{0,127}$')
_PROBE_CODES = frozenset({
    'cli_unavailable', 'unsupported_host_policy', 'unsupported_managed_policy',
    'authentication_unverified', 'authentication_required', 'unsupported_auth_profile',
    'customization_unverified', 'invalid_broker_endpoint', 'unsupported_auth_mode',
    'invalid_source_policy', 'external_source_denied', 'invalid_prompt', 'invalid_limits',
    'invalid_output', 'unexpected_customization', 'unexpected_tool_inventory',
    'missing_initialization', 'turn_limit', 'budget_exhausted', 'rate_limit', 'authentication',
    'context_overflow', 'agent_error', 'timeout', 'output_limit', 'cancelled', 'cli_exit',
    'containment_failure', 'supervision_unavailable', 'docker_unavailable', 'preflight_failed',
    'provision_failed', 'image_unsupported', 'image_not_present', 'container_failure',
    'workspace_full', 'limit_exceeded', 'snapshot_failed', 'ownership_unverified',
    'isolation_unverified', 'not_running', 'not_found', 'unsafe_path', 'discard_failed',
})
_FIELDS = frozenset({'name', 'adapter', 'auth_mode', 'image', 'allow_external_source',
                     'runner_limits', 'agent_limits', 'agent_model'})
# Claude Code model alias or id (e.g. "sonnet", "opus", "claude-opus-5-5", "opus[1m]"); None uses the CLI default.
AGENT_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:\-\[\]]{0,79}')
_FIXTURE = {'calc.sh': b'#!/bin/sh\nprintf "%s\\n" "$(($1 - $2))"\n'}
_VALIDATION = ('set -eu\n'
               'test "$(sh ./calc.sh 2 3)" = 5\n'
               'test "$(sh ./calc.sh -1 2)" = 1\n'
               'test "$(sh ./calc.sh 0 0)" = 0\n')
_PROMPT = ('This is an authorized synthetic coding fixture in an isolated container. '
           'Use only the opendevengine tools to inspect calc.sh and fix its arithmetic so '
           'it adds its two integer arguments instead of subtracting. Keep a POSIX '
           'shell implementation. Validate positive, negative and zero arguments '
           'inside the container with run_command. No other work is requested.')


class ExecutionProfileError(PlatformError):
    """Stable content-free failure; the HTTP layer maps codes to HTTP status."""

    def __init__(self, code):
        self.code = code
        self.http_status = 400 if code == 'validation' else 404 if code == 'not_found' else 409
        super().__init__('Execution profile operation failed.')


def _error(code):
    raise ExecutionProfileError(code)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _storage_boundary(call):
    @wraps(call)
    def safe(*args, **kwargs):
        outcome = None
        try:
            return call(*args, **kwargs)
        except sqlite3.IntegrityError:
            outcome = 'conflict'
        except sqlite3.Error:
            outcome = 'execution_storage_unavailable'
        _error(outcome)
    return safe


def _validate(spec):
    if not isinstance(spec, dict) or set(spec) - _FIELDS:
        _error('validation')
    if not {'name', 'adapter', 'auth_mode', 'image'} <= set(spec):
        _error('validation')
    name = spec['name']
    if (not isinstance(name, str) or not name.strip() or len(name.encode('utf-8', 'surrogatepass')) > 100
            or any(not char.isprintable() for char in name)):
        _error('validation')
    if spec['adapter'] != 'claude_code' or spec['auth_mode'] != 'cli_login':
        _error('unsupported')
    image = spec['image']
    if not _valid_image(image):
        _error('validation')
    consent = spec.get('allow_external_source', False)
    if type(consent) is not bool:
        _error('validation')
    try:
        limits = validate_limits(spec.get('runner_limits', {}))
    except RunnerError:
        _error('validation')
    agent = spec.get('agent_limits', {})
    if not isinstance(agent, dict) or set(agent) - set(AGENT_DEFAULTS):
        _error('validation')
    agent = {**AGENT_DEFAULTS, **agent}
    for key, lower, upper in (('wall_seconds', 1, 3600), ('max_turns', 1, 40),
                               ('max_output_bytes', 1024, 1024 * 1024)):
        if type(agent[key]) is not int or not lower <= agent[key] <= upper:
            _error('validation')
    model = spec.get('agent_model')
    if isinstance(model, str):
        model = model.strip() or None
    if model is not None and (not isinstance(model, str) or not AGENT_MODEL.fullmatch(model)):
        _error('validation')
    budget = agent['max_budget_usd']
    if (isinstance(budget, bool) or not isinstance(budget, (int, float))
            or not math.isfinite(budget) or not 0 < budget <= 10):
        _error('validation')
    return {'name': name.strip(), 'adapter': 'claude_code', 'auth_mode': 'cli_login',
            'config': {'image': image, 'allow_external_source': consent,
                       'runner_limits': limits, 'agent_limits': agent, 'agent_model': model}}


def _valid_image(image):
    """Local Docker references only; userinfo/URLs are never persisted as images."""
    if (not isinstance(image, str) or not _IMAGE.fullmatch(image)
            or '://' in image or '//' in image):
        return False
    if image.startswith('sha256:'):
        return _IMAGE_ID.fullmatch(image) is not None
    if '@' in image:
        if image.count('@') != 1:
            return False
        image, digest = image.split('@')
        if not _IMAGE_ID.fullmatch(digest):
            return False
    parts = image.split('/')
    if len(parts) > 1 and ('.' in parts[0] or ':' in parts[0] or parts[0] == 'localhost'):
        registry = parts.pop(0)
        host, separator, port = registry.partition(':')
        if not _REGISTRY_HOST.fullmatch(host) or '..' in host:
            return False
        if separator and (not port.isdigit() or not 1 <= int(port) <= 65535):
            return False
    name, separator, tag = parts[-1].partition(':')
    if separator:
        if not _IMAGE_TAG.fullmatch(tag):
            return False
        parts[-1] = name
    return all(_IMAGE_COMPONENT.fullmatch(part) for part in parts)


class ExecutionProfileService:
    def __init__(self, platform, providers, *, runner_factory=None, adapter_factory=None, clock=now_ms):
        self.platform, self.store, self.jobs = platform, platform.store, platform.jobs
        self.providers, self.clock = providers, clock
        self.runner_factory = runner_factory if runner_factory is not None else DockerRunner
        self.adapter_factory = adapter_factory if adapter_factory is not None else ClaudeAdapter
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO runtime_instance VALUES(1,?,?)', (new_id('runtime'), clock()))
            self.instance_id = db.execute('SELECT id FROM runtime_instance WHERE singleton=1').fetchone()[0]
            db.execute('INSERT OR IGNORE INTO execution_settings VALUES(1,1,?)', (clock(),))

    @staticmethod
    def _row(db, table, ident):
        if not isinstance(ident, str) or not ident or len(ident) > 100:
            _error('not_found')
        row = db.execute('SELECT * FROM ' + table + ' WHERE id=?', (ident,)).fetchone()
        if row is None:
            _error('not_found')
        return row

    def _check(self, db, version_id):
        version = self._row(db, 'execution_profile_versions', version_id)
        row = None
        # A presentation-only rename may reuse evidence, including a probe already
        # in flight. Do not duplicate its private runtime/job recovery record. Any
        # intervening executable/source/limit change breaks the inheritance chain.
        for candidate in db.execute(
                'SELECT v.adapter,v.auth_mode,v.config_json,c.* FROM execution_profile_versions v '
                'LEFT JOIN execution_profile_checks c ON c.version_id=v.id '
                'WHERE v.profile_id=? AND v.version<=? ORDER BY v.version DESC',
                (version['profile_id'], version['version'])):
            if any(candidate[key] != version[key] for key in ('adapter', 'auth_mode', 'config_json')):
                break
            if candidate['version_id'] is not None:
                row = candidate
                break
        if row is None:
            return None
        data = json.loads(row['result_json'])
        status, code = row['status'], row['error_code']
        job = db.execute('SELECT state,lease_expires_at FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
        active = job is not None and (job['state'] == 'queued' or
                 job['state'] == 'leased' and job['lease_expires_at'] > self.clock())
        if status in ('queued', 'running') and not active:
            status, code = 'interrupted', 'execution_test_interrupted'
        elif status == 'passed' and (job is None or job['state'] != 'succeeded'):
            status, code = ('running', None) if active else ('interrupted', 'execution_test_interrupted')
        return {'status': status, 'capabilities': data.get('capabilities', {}) if status == 'passed' else {},
                'image_id': data.get('image_id'), 'cli_version': data.get('cli_version'),
                'checked_at': row['checked_at'], 'error_code': code}

    def _version(self, db, row):
        profile = self._row(db, 'execution_profiles', row['profile_id'])
        return {'id': row['id'], 'profile_id': row['profile_id'], 'version': row['version'],
                'adapter': row['adapter'], 'auth_mode': row['auth_mode'],
                'config': json.loads(row['config_json']), 'created_at': row['created_at'],
                'profile_deleted': profile['deleted_at'] is not None, 'check': self._check(db, row['id'])}

    def _public(self, db, row):
        version = self._version(db, self._row(db, 'execution_profile_versions', row['active_version_id']))
        return {'id': row['id'], 'name': row['name'], 'active_version_id': row['active_version_id'],
                'version': version, 'check': version['check'], 'deleted_at': row['deleted_at'],
                'created_at': row['created_at'], 'updated_at': row['updated_at']}

    @_storage_boundary
    def create(self, spec):
        spec = _validate(spec)
        profile_id, version_id, now = new_id('exec'), new_id('ever'), self.clock()
        with self.store.transaction() as db:
            db.execute('INSERT INTO execution_profiles VALUES(?,?,?,NULL,?,?)',
                       (profile_id, spec['name'], version_id, now, now))
            db.execute('INSERT INTO execution_profile_versions VALUES(?,?,1,?,?,?,?)',
                       (version_id, profile_id, spec['adapter'], spec['auth_mode'], _json(spec['config']), now))
            return self._public(db, self._row(db, 'execution_profiles', profile_id))

    @_storage_boundary
    def update(self, ident, changes):
        if not isinstance(changes, dict) or not changes or set(changes) - _FIELDS:
            _error('validation')
        with self.store.transaction() as db:
            row = self._row(db, 'execution_profiles', ident)
            if row['deleted_at'] is not None:
                _error('execution_profile_deleted')
            previous = self._row(db, 'execution_profile_versions', row['active_version_id'])
            values = {'name': row['name'], 'adapter': previous['adapter'], 'auth_mode': previous['auth_mode'],
                      **json.loads(previous['config_json']), **changes}
            spec, version_id, now = _validate(values), new_id('ever'), self.clock()
            db.execute('INSERT INTO execution_profile_versions VALUES(?,?,?,?,?,?,?)',
                       (version_id, ident, previous['version'] + 1, spec['adapter'], spec['auth_mode'],
                        _json(spec['config']), now))
            db.execute('UPDATE execution_profiles SET name=?,active_version_id=?,updated_at=? WHERE id=?',
                       (spec['name'], version_id, now, ident))
            return self._public(db, self._row(db, 'execution_profiles', ident))

    @_storage_boundary
    def delete(self, ident):
        with self.store.transaction() as db:
            row = self._row(db, 'execution_profiles', ident)
            if row['deleted_at'] is None:
                now = self.clock()
                db.execute('UPDATE execution_profiles SET deleted_at=?,updated_at=? WHERE id=?', (now, now, ident))
            return self._public(db, self._row(db, 'execution_profiles', ident))

    @_storage_boundary
    def get(self, ident):
        with self.store.read() as db:
            return self._public(db, self._row(db, 'execution_profiles', ident))

    @_storage_boundary
    def get_version(self, version_id):
        with self.store.read() as db:
            return self._version(db, self._row(db, 'execution_profile_versions', version_id))

    @_storage_boundary
    def default_profile_id(self):
        """Select only a unique live profile whose active version passed its check."""
        with self.store.read() as db:
            passed = []
            for row in db.execute('SELECT id,active_version_id FROM execution_profiles WHERE deleted_at IS NULL'):
                check = self._check(db, row['active_version_id'])
                if check and check['status'] == 'passed':
                    passed.append(row['id'])
                    if len(passed) > 1:
                        return None
            return passed[0] if passed else None

    @_storage_boundary
    def list_profiles(self, limit=50, cursor=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            _error('validation')
        params, where = [], 'deleted_at IS NULL'
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 512:
                    raise ValueError
                stamp, ident = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))
                if (type(stamp) is not int or not -(2 ** 63) <= stamp <= 2 ** 63 - 1
                        or not isinstance(ident, str) or len(ident) > 100):
                    raise ValueError
            except (ValueError, TypeError, UnicodeError):
                _error('validation')
            where += ' AND (created_at,id)>(?,?)'
            params += [stamp, ident]
        with self.store.read() as db:
            rows = db.execute('SELECT * FROM execution_profiles WHERE ' + where +
                              ' ORDER BY created_at,id LIMIT ?', (*params, limit + 1)).fetchall()
            nxt = None
            if len(rows) > limit:
                rows = rows[:limit]
                nxt = base64.urlsafe_b64encode(_json([rows[-1]['created_at'], rows[-1]['id']]).encode()).decode()
            return {'items': [self._public(db, row) for row in rows], 'next_cursor': nxt}

    @_storage_boundary
    def settings(self):
        with self.store.read() as db:
            return {'max_active_sessions': db.execute(
                'SELECT max_active_sessions FROM execution_settings WHERE singleton=1').fetchone()[0]}

    @_storage_boundary
    def update_settings(self, max_active_sessions):
        if type(max_active_sessions) is not int or not 1 <= max_active_sessions <= 4:
            _error('validation')
        with self.store.transaction() as db:
            db.execute('UPDATE execution_settings SET max_active_sessions=?,updated_at=? WHERE singleton=1',
                       (max_active_sessions, self.clock()))
        return self.settings()

    def _policy(self, version, *, synthetic=False):
        if self.providers.settings().get('local_only') is not False:
            _error('execution_local_only')
        if not synthetic and not version['config']['allow_external_source']:
            _error('execution_external_source_denied')

    @_storage_boundary
    def require_usable(self, ident):
        with self.store.read() as db:
            profile = self._row(db, 'execution_profiles', ident)
            return self.require_version_usable(profile['active_version_id'])

    @_storage_boundary
    def require_version_usable(self, version_id):
        """Resolve pinned execution settings with current, revocable permission.

        A new active version cannot change a recorded budget/image. Current
        consent, profile deletion, workspace privacy and cached-check validity
        still gate use of every historical version.
        """
        with self.store.read() as db:
            version = self._version(db, self._row(db, 'execution_profile_versions', version_id))
            profile = self._row(db, 'execution_profiles', version['profile_id'])
            if profile['deleted_at'] is not None:
                _error('execution_profile_deleted')
            self._policy(version)
            active = self._row(db, 'execution_profile_versions', profile['active_version_id'])
            if json.loads(active['config_json'])['allow_external_source'] is not True:
                _error('execution_external_source_denied')
            check = version['check']
            if check is None:
                _error('execution_test_required')
            if check['status'] != 'passed':
                _error({'failed': 'execution_test_failed', 'interrupted': 'execution_test_interrupted'}
                       .get(check['status'], 'execution_test_pending'))
            return version

    @_storage_boundary
    def request_test(self, profile_id):
        with self.store.transaction() as db:
            profile = self._row(db, 'execution_profiles', profile_id)
            if profile['deleted_at'] is not None:
                _error('execution_profile_deleted')
            version = self._version(db, self._row(db, 'execution_profile_versions', profile['active_version_id']))
            self._policy(version, synthetic=True)
            for previous in db.execute('SELECT c.result_json,c.error_code FROM execution_profile_checks c '
                                       'JOIN execution_profile_versions v ON v.id=c.version_id WHERE v.profile_id=?',
                                       (profile_id,)):
                if previous['error_code'] == 'containment_unverified':
                    _error('execution_test_reconciliation_required')
            active = db.execute('SELECT c.version_id,c.job_id,j.state,j.lease_expires_at FROM execution_profile_checks c '
                                'JOIN execution_profile_versions v ON v.id=c.version_id '
                                'JOIN jobs j ON j.id=c.job_id WHERE v.profile_id=? '
                                "AND j.state IN ('queued','leased','needs_reconciliation') ORDER BY j.created_at DESC",
                                (profile_id,)).fetchall()
            for row in active:
                if (row['state'] == 'needs_reconciliation' or row['state'] == 'leased'
                        and row['lease_expires_at'] <= self.clock()):
                    _error('execution_test_reconciliation_required')
                return {'job_id': row['job_id'], 'version_id': row['version_id'], 'status': 'queued'
                        if row['state'] == 'queued' else 'running'}
            job = self.jobs.enqueue_in_transaction(db, 'execution.test', {'version_id': version['id']},
                                                   effect='external', max_attempts=1)
            db.execute("INSERT INTO execution_profile_checks VALUES(?,?,'queued','{}',NULL,NULL) "
                       "ON CONFLICT(version_id) DO UPDATE SET job_id=excluded.job_id,status='queued',"
                       "result_json='{}',error_code=NULL,checked_at=NULL", (version['id'], job.id))
            return {'job_id': job.id, 'version_id': version['id'], 'status': 'queued'}

    def _owned_test(self, db, job, context, *, allow_stopped=False):
        lease = context.lease
        queue = db.execute('SELECT * FROM jobs WHERE id=?', (job.id,)).fetchone()
        check = db.execute('SELECT * FROM execution_profile_checks WHERE version_id=?',
                           (job.payload.get('version_id'),)).fetchone()
        if (queue is None or check is None or job.kind != 'execution.test' or queue['kind'] != job.kind
                or queue['effect'] != 'external' or check['job_id'] != job.id
                or queue['payload'] != _json({'version_id': check['version_id']})
                or job.payload != {'version_id': check['version_id']}
                or lease.job.id != job.id or queue['state'] != 'leased'
                or queue['lease_owner'] != lease.owner or queue['lease_token'] != lease.token
                or queue['lease_expires_at'] <= self.clock()):
            raise LeaseLost('Execution check lease lost.')
        version = self._version(db, self._row(db, 'execution_profile_versions', check['version_id']))
        if not allow_stopped:
            if getattr(context, 'shutdown_requested', False):
                raise JobInterrupted()
            if queue['cancel_requested']:
                raise CancelledError()
            if version['profile_deleted']:
                _error('execution_profile_deleted')
            self._policy(version, synthetic=True)
        return version, check

    @contextmanager
    def _monitor(self, job, context, stop, deadline):
        """Renew only live leases and relay policy/shutdown while the CLI blocks."""
        done, errors = threading.Event(), []
        interval = max(0.05, min(5., context.lease_ms / 3000))
        next_heartbeat = time.monotonic() + interval

        def check():
            if errors:
                raise errors[0]
            with self.store.read() as db:
                self._owned_test(db, job, context)
            if time.monotonic() >= deadline:
                _error('execution_test_timeout')

        def watch():
            nonlocal next_heartbeat
            while not done.wait(0.05):
                try:
                    check()
                    if time.monotonic() >= next_heartbeat:
                        # Ownership and expiry are checked under the same Store lock
                        # as heartbeat; the queue alone does not reject stale expiry.
                        with self.store.read() as db:
                            self._owned_test(db, job, context)
                            self.jobs.heartbeat(context.lease, lease_ms=context.lease_ms)
                        next_heartbeat = time.monotonic() + interval
                except (Exception, JobInterrupted) as error:
                    errors.append(error)
                    stop.set()
                    return

        watcher = threading.Thread(target=watch, name='execution-test-lease', daemon=False)
        watcher.start()
        try:
            yield check
        finally:
            done.set()
            watcher.join()

    def _runtime(self, job, context, **changes):
        with self.store.transaction() as db:
            _, row = self._owned_test(db, job, context)
            data = json.loads(row['result_json'])
            private = data.setdefault('private_runtime', {})
            private.update(changes)
            db.execute('UPDATE execution_profile_checks SET result_json=? WHERE version_id=? AND job_id=?',
                       (_json(data), row['version_id'], job.id))

    @staticmethod
    def _process_start(pid):
        # A trusted child PID, never an HTTP argument. Recovery compares this OS
        # start identity before signalling a potentially reused process group.
        import subprocess
        if type(pid) is not int or pid <= 1:
            _error('execution_runtime_identity_unverified')
        if os.path.isdir('/proc/self'):
            try:
                with open('/proc/%d/stat' % pid, 'rb') as stream:
                    fields = stream.read(4096).decode('ascii').rsplit(')', 1)[-1].split()
                return 'linux:' + fields[19]
            except (OSError, UnicodeError, IndexError):
                _error('execution_runtime_identity_unverified')
        try:
            result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=2,
                                    env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'TZ': 'UTC'})
            identity = result.stdout.decode('ascii').strip()
        except (OSError, UnicodeError, subprocess.SubprocessError):
            _error('execution_runtime_identity_unverified')
        if result.returncode != 0 or not re.fullmatch(r'[A-Za-z0-9 :]{15,64}', identity):
            _error('execution_runtime_identity_unverified')
        return 'ps:' + ' '.join(identity.split())

    @_storage_boundary
    def run_test(self, job, context):
        stop, gate = threading.Event(), threading.Event()
        with self.store.transaction() as db:
            version, previous = self._owned_test(db, job, context, allow_stopped=True)
            if previous['status'] != 'queued':
                _error('execution_test_reconciliation_required')
            try:
                self._owned_test(db, job, context)
            except LeaseLost:
                raise
            except (Exception, JobInterrupted) as failure:
                # This attempt has not called a factory or touched the runtime.
                # Record its known, safe rejection instead of making the external
                # worker invent an unknown outcome requiring manual containment.
                code = (failure.code if isinstance(failure, ExecutionProfileError) else
                        'execution_test_interrupted' if isinstance(failure, (JobInterrupted, CancelledError))
                        else 'execution_test_failed')
                db.execute("UPDATE execution_profile_checks SET status='failed',result_json=?,"
                           'error_code=?,checked_at=? WHERE version_id=? AND job_id=?',
                           (_json({'checks': {'runtime_started': False, 'containment_confirmed': True}}),
                            code, self.clock(), version['id'], job.id))
                return {'version_id': version['id'], 'status': 'failed', 'error_code': code}
            db.execute("UPDATE execution_profile_checks SET status='running',result_json='{}',"
                       'error_code=NULL,checked_at=NULL WHERE version_id=? AND job_id=?',
                       (version['id'], job.id))
        config = version['config']
        wall = min(config['runner_limits']['wall_seconds'], config['agent_limits']['wall_seconds'])
        deadline = time.monotonic() + wall
        runner = handle = broker = None
        provision_started = False
        error = None
        harness_confirmed = True
        container_confirmed = True
        broker_confirmed = True
        discarded = True
        data = {'image_id': None, 'cli_version': None, 'capabilities': {}, 'checks': {},
                'usage': None, 'reported_cost_usd': None}
        calls = set()
        capabilities = {}
        with self._monitor(job, context, stop, deadline) as check:
            def guard():
                try:
                    check()
                except LeaseLost:
                    stop.set()
                    return 'lease_lost'
                except (CancelledError, JobInterrupted):
                    stop.set()
                    return 'stop_requested'
                except Exception:
                    stop.set()
                    return 'policy_denied'
                return None if gate.is_set() and not stop.is_set() else 'policy_denied'

            def emit(kind, payload):
                # Do not retain summaries, tool arguments, output or private reasoning.
                if kind == 'tool_finished' and payload.get('ok') is True and payload.get('tool') in TOOL_NAMES:
                    calls.add(payload['tool'])

            def inventory_ready():
                check()
                data['checks']['inventory_verified'] = True
                gate.set()

            def started(pid):
                self._runtime(job, context, harness_pid=pid, harness_start=self._process_start(pid))

            try:
                check()
                runner = self.runner_factory(self.instance_id)
                preflight = runner.preflight(config['image'])
                if preflight.get('available') is not True or not _IMAGE_ID.fullmatch(str(preflight.get('image_id', ''))):
                    _error('execution_runner_unavailable')
                data['image_id'] = preflight['image_id']
                check()
                adapter = self.adapter_factory()
                detected = adapter.preflight()
                cli_version = detected.get('version')
                if (detected.get('available') is not True or not isinstance(cli_version, str)
                        or not _CLI_VERSION.fullmatch(cli_version)):
                    _error('execution_cli_unverified')
                data['cli_version'] = cli_version
                capabilities = {key: value for key, value in detected.get('capabilities', {}).items()
                                if key in _CAPABILITIES and type(value) is bool}
                capabilities.update(resume=False, checkpoint=False, follow_up=False)
                check()
                self._runtime(job, context, instance_id=self.instance_id, container_id=None,
                              harness_pid=None, harness_start=None)
                provision_started = True
                container_confirmed = discarded = False
                handle = runner.provision(job.id, dict(_FIXTURE), image=data['image_id'],
                                          limits=config['runner_limits'])
                if handle.image_id != data['image_id']:
                    _error('execution_image_changed')
                self._runtime(job, context, instance_id=self.instance_id, container_id=handle.container_id,
                              harness_pid=None, harness_start=None)
                broker = ToolBroker(runner, handle, guard=guard, emit=emit)
                endpoint = broker.start()
                check()
                harness_confirmed = False
                try:
                    result = adapter.run(
                        endpoint, _PROMPT, auth_mode=version['auth_mode'], synthetic=True,
                        local_only=False, external_source_consent=False,
                        on_inventory_verified=inventory_ready, emit=lambda *_: None,
                        stop_event=stop, on_started=started,
                        timeout_seconds=max(0.001, deadline - time.monotonic()),
                        max_turns=config['agent_limits']['max_turns'],
                        max_budget_usd=config['agent_limits']['max_budget_usd'],
                        max_output_bytes=config['agent_limits']['max_output_bytes'],
                        model=config.get('agent_model'))
                    harness_confirmed = result.get('stop_confirmed') is True
                except ProcessContainmentError:
                    harness_confirmed = False
                    raise
                except (Exception, JobInterrupted):
                    # The adapter's process boundary confirms termination before
                    # propagating callback errors. Only ProcessContainmentError or
                    # an explicit stop_confirmed=False means containment is unknown.
                    harness_confirmed = True
                    raise
                finally:
                    gate.clear()
                    stop.set()  # Revoke model tools on every adapter exit, even success.
                check()  # Trusted validation ignores the model's closed stop gate.
                if (result.get('status') != 'completed' or result.get('code') != 'success'
                        or result.get('exit_code') != 0 or not harness_confirmed):
                    code = result.get('code')
                    _error((code if code in _PROBE_CODES else 'execution_agent_failed')
                           if harness_confirmed else 'containment_unverified')
                snapshot = runner.snapshot(handle)
                check()
                data['checks']['tools_used'] = bool(calls & {'write_file', 'run_command'})
                data['checks']['fixture_changed'] = (isinstance(snapshot.get('calc.sh'), bytes)
                                                     and snapshot['calc.sh'] != _FIXTURE['calc.sh'])
                # The validation program is trusted service input, never a workspace
                # test file that the coding agent could replace to manufacture a pass.
                validated = runner.run_command(handle, ['sh', '-c', _VALIDATION],
                                               timeout_seconds=max(1, min(30, int(deadline - time.monotonic()))))
                check()
                data['checks']['independent_validation'] = (validated.get('exit_code') == 0
                                                            and validated.get('timed_out') is False
                                                            and validated.get('truncated') is False)
                if not all(data['checks'].get(key) is True for key in
                           ('inventory_verified', 'tools_used', 'fixture_changed', 'independent_validation')):
                    _error('execution_coding_unverified')
                usage = result.get('usage')
                if isinstance(usage, dict):
                    data['usage'] = {key: value if type(value) is int and value >= 0 else None
                                     for key, value in usage.items()
                                     if key in ('input_tokens', 'output_tokens', 'cache_creation_input_tokens',
                                                'cache_read_input_tokens')}
                cost = result.get('reported_cost_usd')
                if (type(cost) in (int, float) and math.isfinite(cost) and cost >= 0):
                    data['reported_cost_usd'] = cost
                    if cost > config['agent_limits']['max_budget_usd']:
                        _error('budget_exhausted')
            except (Exception, JobInterrupted) as failure:
                error = failure
            finally:
                gate.clear()
                stop.set()
                if broker is not None:
                    try:
                        broker_confirmed = broker.close().get('closed') is True
                    except Exception:
                        broker_confirmed = False
                if handle is not None:
                    try:
                        container_confirmed = runner.stop(handle).get('confirmed') is True
                        if container_confirmed:
                            runner.discard(handle)
                            discarded = True
                    except Exception:
                        discarded = False
                elif provision_started:
                    # provision can fail after creating a labelled container. An
                    # absent return handle is not proof that nothing was created.
                    try:
                        owned = [item for item in runner.list_owned() if item.session_id == job.id]
                        container_confirmed = discarded = True
                        for orphan in owned:
                            stopped = runner.stop(orphan).get('confirmed') is True
                            container_confirmed = container_confirmed and stopped
                            if stopped:
                                runner.discard(orphan)
                            else:
                                discarded = False
                    except Exception:
                        container_confirmed = discarded = False
                try:
                    check()
                except (Exception, JobInterrupted) as failure:
                    error = failure
        contained = harness_confirmed and container_confirmed and broker_confirmed and discarded
        data['checks']['containment_confirmed'] = contained
        if not contained:
            status, code = 'interrupted', 'containment_unverified'
        elif isinstance(error, (JobInterrupted, CancelledError)):
            status, code = 'interrupted', 'execution_test_interrupted'
        elif error is not None:
            status = 'failed'
            code = (error.code if isinstance(error, ExecutionProfileError) or
                    isinstance(error, (AdapterError, RunnerError)) and error.code in _PROBE_CODES
                    else 'execution_test_failed')
        else:
            status, code = 'passed', None
            data['capabilities'] = capabilities
        with self.store.transaction() as db:
            _, current = self._owned_test(db, job, context, allow_stopped=True)
            if not contained:
                data['private_runtime'] = json.loads(current['result_json']).get('private_runtime', {})
            db.execute('UPDATE execution_profile_checks SET status=?,result_json=?,error_code=?,checked_at=? '
                       'WHERE version_id=? AND job_id=?',
                       (status, _json(data), code, self.clock(), version['id'], job.id))
        if not contained:
            raise UnknownOutcomeError()
        if isinstance(error, (JobInterrupted, CancelledError, LeaseLost)):
            raise error
        return {'version_id': version['id'], 'status': status, 'error_code': code}

    @_storage_boundary
    def pending_runtime_tests(self):
        """Trusted leader-only recovery metadata. Never include in an HTTP response."""
        with self.store.read() as db:
            rows = db.execute('SELECT c.version_id,c.job_id,c.result_json,j.state FROM execution_profile_checks c '
                              'JOIN jobs j ON j.id=c.job_id').fetchall()
            result = []
            for row in rows:
                private = json.loads(row['result_json']).get('private_runtime', {})
                if private or row['state'] == 'needs_reconciliation':
                    result.append({'version_id': row['version_id'], 'job_id': row['job_id'],
                                   'private_runtime': private})
            return result

    @_storage_boundary
    def reconcile_test(self, version_id, job_id, *, containment_confirmed):
        if type(containment_confirmed) is not bool:
            _error('validation')
        # Hold the Store lock across queue reconciliation and the check update;
        # queue owns its own transaction. No process/container action happens here.
        with self.store.read() as db:
            row = db.execute('SELECT * FROM execution_profile_checks WHERE version_id=? AND job_id=?',
                             (version_id, job_id)).fetchone()
            if row is None:
                _error('not_found')
            job = self.jobs.get(job_id)
            if job.state in ('queued', 'leased'):
                _error('execution_test_active' if job.state == 'queued' or job.lease_expires_at > self.clock()
                       else 'execution_test_reconciliation_required')
            data = json.loads(row['result_json'])
            if containment_confirmed:
                if job.state == 'needs_reconciliation':
                    self.jobs.resolve_reconciliation(job_id, outcome='failed')
                data.pop('private_runtime', None)
            data['capabilities'] = {}
            with self.store.transaction() as tx:
                tx.execute("UPDATE execution_profile_checks SET status='interrupted',result_json=?,error_code=?,checked_at=? "
                           'WHERE version_id=? AND job_id=?',
                           (_json(data), 'execution_test_interrupted' if containment_confirmed else 'containment_unverified',
                            self.clock(), version_id, job_id))
        return {'version_id': version_id, 'status': 'interrupted',
                'error_code': 'execution_test_interrupted' if containment_confirmed else 'containment_unverified'}
