"""One immutable session checkpoint to one GitHub draft pull request.

Source bytes enter only through SessionService.publication_snapshot and are retained
only in session artifacts.  Installation tokens stay in this call stack.
"""

import base64
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
from functools import wraps
from threading import RLock

from opendevengine.platform.errors import InvalidTransition

from opendevengine.github.errors import GitHubError
from opendevengine.platform.clock import new_id, now_ms
from opendevengine.platform.worker import UnknownOutcomeError
from .errors import PublicationError
from opendevengine.sessions.publication import is_omitted
from opendevengine.sessions.redaction import redact_text

# One local server process owns publication. Serialize recovery readers
# with retries across service instances; SQLite still rechecks durable job identity.
RECOVERY_LOCK = RLock()


def serialized_recovery(method):
    @wraps(method)
    def call(*args, **kwargs):
        with RECOVERY_LOCK:
            return method(*args, **kwargs)
    return call


RUN_JOB = ContextVar('publication_run_job', default=None)

TERMINAL = frozenset({'published', 'blocked', 'failed', 'no_changes'})
ACTIVE = frozenset({'queued', 'preparing', 'creating_objects', 'creating_branch', 'creating_pr', 'reconciling'})
SHA = re.compile(r'^[0-9a-f]{40}(?:[0-9a-f]{24})?$')
KEY = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$')
EMAIL = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
PATH = re.compile(r'^(?!/)(?!.*(?:^|/)\.\.?(?:/|$))(?!.*[\x00-\x1f\\])[^\n\r]{1,1024}$')
SAFE_URL = re.compile(r'^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*$')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _text(value, low, high, field):
    if not isinstance(value, str) or not low <= len(value) <= high or any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise PublicationError('validation', details=[field])
    return value


class PublicationService:
    def __init__(self, platform, github, sessions, *, clock=now_ms):
        self.platform, self.store, self.jobs = platform, platform.store, platform.jobs
        self.github, self.sessions, self.clock = github, sessions, clock
        try:
            github.register_purge_hook('publications', self.purge_repository)
        except AttributeError:
            pass

    # ----------------------------------------------------------- settings
    def update_settings(self, commit_name, commit_email):
        name = _text(commit_name, 1, 100, 'commit_name').strip()
        email = _text(commit_email, 3, 254, 'commit_email').strip()
        if not name or not EMAIL.fullmatch(email):
            raise PublicationError('validation')
        with self.store.transaction() as db:
            db.execute('INSERT INTO publication_settings(singleton,commit_name,commit_email,updated_at) VALUES(1,?,?,?) '
                       'ON CONFLICT(singleton) DO UPDATE SET commit_name=excluded.commit_name,'
                       'commit_email=excluded.commit_email,updated_at=excluded.updated_at',
                       (name, email, self.clock()))
        return {'commit_name': name, 'commit_email': email}

    def settings(self):
        with self.store.read() as db:
            row = db.execute('SELECT commit_name,commit_email FROM publication_settings WHERE singleton=1').fetchone()
        return {'commit_name': row['commit_name'] if row else None, 'commit_email': row['commit_email'] if row else None}

    # ------------------------------------------------------------- helpers
    def _snapshot(self, session_id):
        if not isinstance(session_id, str) or not session_id:
            raise PublicationError('validation')
        try:
            snap = self.sessions.publication_snapshot(session_id)
        except Exception as exc:
            code = getattr(exc, 'code', None)
            raise PublicationError(code if isinstance(code, str) else 'session_unavailable') from None
        required = {'session_id','plan_id','plan_version_id','repository_id','base_sha','base_branch','diff_hash',
                    'checkpoint_artifact_id','patch_artifact_id','checkpoint','patch','policy','outcome','validations','source_available'}
        if not isinstance(snap, dict) or not required <= set(snap) or snap['session_id'] != session_id:
            raise PublicationError('session_snapshot_invalid')
        if not snap['source_available']:
            raise PublicationError('source_unavailable')
        if snap['policy'] not in ('manual_draft', 'on_success_draft') or not SHA.fullmatch(snap['base_sha']):
            raise PublicationError('session_snapshot_invalid')
        checkpoint, patch = snap['checkpoint'], snap['patch']
        if not isinstance(checkpoint, dict) or not isinstance(patch, dict):
            raise PublicationError('session_snapshot_invalid')
        if checkpoint.get('base_sha') != snap['base_sha'] or patch.get('base_sha') != snap['base_sha']:
            raise PublicationError('session_snapshot_invalid')
        if any(not is_omitted(r) for r in checkpoint.get('rejected', []) + patch.get('rejected', [])):
            raise PublicationError('rejected_paths')
        omitted = sorted(r['path'] for r in checkpoint.get('rejected', []))
        if (checkpoint.get('rejected', []) != patch.get('rejected', [])
                or snap.get('omitted', []) != omitted
                or set(omitted) & (set(checkpoint.get('changed', {})) | set(checkpoint.get('deleted', [])))):
            raise PublicationError('session_snapshot_invalid')
        self._validate_paths(checkpoint)
        return snap

    @staticmethod
    def _validate_paths(checkpoint):
        changed, deleted = checkpoint.get('changed', {}), checkpoint.get('deleted', [])
        if not isinstance(changed, dict) or not isinstance(deleted, list):
            raise PublicationError('session_snapshot_invalid')
        paths = list(changed) + deleted
        if any(not isinstance(p, str) or not PATH.fullmatch(p) or
               any(not c or c.lower() == '.git' for c in p.split('/')) for p in paths):
            raise PublicationError('session_snapshot_invalid')
        if len(set(paths)) != len(paths) or any(
                a.startswith(b + '/') for a in paths for b in paths if a != b):
            raise PublicationError('session_snapshot_invalid')

    def _identity(self):
        value = self.settings()
        if not value['commit_name'] or not value['commit_email']:
            raise PublicationError('publication_identity_required')
        return value

    def _check_access(self, repository_id):
        try:
            self.github.require_access(repository_id, 'contents_write')
            self.github.require_access(repository_id, 'pull_requests_write')
            return self.github.get_repository(repository_id)
        except GitHubError as exc:
            raise PublicationError('publication_permission_denied', details=[exc.code]) from None

    @staticmethod
    def _stats(snap):
        stats = snap['patch'].get('stats')
        return stats if isinstance(stats, dict) else {'files': len(snap['patch'].get('files', []))}

    @staticmethod
    def _automatic_ok(snap):
        outcome = snap['outcome'] if isinstance(snap['outcome'], dict) else {}
        validations = snap['validations'] if isinstance(snap['validations'], dict) else {}
        return (outcome.get('result') == 'reviewable' and outcome.get('publication_eligible') is True
                and outcome.get('containment', {}).get('confirmed') is True
                and not outcome.get('artifacts_stale') and validations.get('fully_validated') is True)

    def _defaults(self, snap):
        # Read the immutable version identified by the trusted snapshot, never latest.
        with self.store.read() as db:
            row = db.execute('SELECT content_json FROM plan_versions WHERE id=? AND plan_id=? AND deleted_at IS NULL',
                             (snap['plan_version_id'], snap['plan_id'])).fetchone()
        content = json.loads(row['content_json']) if row and row['content_json'] else {}

        def safe(value, limit=300):
            # Redact before truncating so a cut token cannot escape the detector.
            return ' '.join(redact_text(value, None).split())[:limit]

        goal = safe(content.get('goal') or '', 1000)
        title = safe(goal or snap.get('title') or 'OpenDevEngine session ' + snap['session_id'], 256).rstrip('.').strip()
        title = title or 'OpenDevEngine session'
        lines = ['## Goal', '', goal or 'No plan goal recorded.', '', '## Plan steps', '']
        steps = content.get('steps', [])
        lines += ['- ' + safe(step.get('title', 'Untitled step'), 150) for step in steps[:20]]
        if not steps:
            lines.append('No plan steps recorded.')
        if len(steps) > 20:
            lines.append('- … %d more steps omitted.' % (len(steps) - 20))
        lines += ['', '## Local validation', '']
        checks = snap['validations'] if isinstance(snap['validations'], dict) else {}
        items = checks.get('items', [])
        limited = not items or checks.get('fully_validated') is not True
        for item in items[:20]:
            command = item.get('command', {})
            argv = command.get('argv', []) if isinstance(command, dict) else command
            command_text = ' '.join(str(arg) for arg in argv) if isinstance(argv, list) else str(argv)
            status = item.get('status')
            label = status if status in ('passed', 'failed') else 'not run'
            limited = limited or label != 'passed'
            lines.append('- %s — %s' % (safe(command_text or 'Unrecorded command', 150), label))
        if not items:
            lines.append('- No recorded validation commands — not run')
        if len(items) > 20:
            lines.append('- … %d more recorded checks omitted.' % (len(items) - 20))
            limited = True
        # Summary-only fixtures and historical records must also disclose failures.
        limited = limited or any(n for status, n in checks.get('summary', {}).items() if status != 'passed')
        omitted = snap.get('omitted', [])
        if omitted:
            lines += ['', '## Omitted generated artifacts (%d)' % len(omitted), '']
            lines += ['- ' + safe(path, 150) for path in omitted[:20]]
            if len(omitted) > 20:
                lines.append('- … %d more paths omitted.' % (len(omitted) - 20))
        if limited:
            lines += ['', 'Limitations: Validation is incomplete or failed; review the recorded checks before merging.']
        return title, redact_text('\n'.join(lines) + '\n', None)[:12000]

    # -------------------------------------------------------------- public
    def preview(self, session_id):
        snap = self._snapshot(session_id)
        blockers = []
        try:
            repo = self._check_access(snap['repository_id'])
        except PublicationError as exc:
            repo, blockers = None, [exc.code]
        try:
            self._identity()
        except PublicationError as exc:
            blockers.append(exc.code)
        if snap['policy'] == 'on_success_draft' and not self._automatic_ok(snap):
            blockers.append('automatic_policy_not_satisfied')
        observation = {'sha': None, 'diverged': None}
        if repo:
            try:
                _, token = self._read_remote(snap['repository_id'])
                current = self.github.client.default_branch_sha(token, repo['owner'], repo['name'], snap['base_branch'])
                observation = {'sha': current, 'diverged': current != snap['base_sha']}
                self._divergence(snap, repo, token, current)
            except (PublicationError, GitHubError) as exc:
                blockers.append(getattr(exc, 'code', 'github_unavailable'))
        title, body = self._defaults(snap)
        changed = list(snap['checkpoint'].get('changed', {})) + list(snap['checkpoint'].get('deleted', []))
        return {'session_id': session_id, 'repository_id': snap['repository_id'],
                'repository': repo['full_name'] if repo else None, 'base_sha': snap['base_sha'],
                'base_branch': snap['base_branch'], 'head_branch': 'opendevengine/%s-<suffix>' % re.sub('[^A-Za-z0-9._-]', '-', session_id)[:80],
                'diff_hash': snap['diff_hash'], 'stats': self._stats(snap),
                'checks': snap['validations'], 'policy': snap['policy'], 'title': title, 'body': body,
                'omitted_paths': snap.get('omitted', []),
                'intent_version': 1, 'changed_paths': sorted(set(changed)),
                'base_observation': observation,
                'blocking_reasons': sorted(set(blockers)), 'publishable': not blockers}

    def publish(self, session_id, title, body, expected_diff_hash, intent_version, idempotency_key):
        title, body = _text(title, 1, 256, 'title').strip(), _text(body, 0, 65536, 'body')
        if (not title or not isinstance(expected_diff_hash, str) or not SHA.fullmatch(expected_diff_hash)
                or intent_version != 1 or not isinstance(idempotency_key, str) or not KEY.fullmatch(idempotency_key)):
            raise PublicationError('validation')
        request = {'session_id': session_id, 'title': title, 'body': body,
                   'expected_diff_hash': expected_diff_hash, 'intent_version': intent_version}
        request_hash = digest(request)
        with self.store.read() as db:
            existing = db.execute('SELECT * FROM publications WHERE idempotency_key=? OR session_id=?',
                                  (idempotency_key, session_id)).fetchone()
        if existing:
            if existing['idempotency_key'] != idempotency_key or existing['request_hash'] != request_hash:
                raise PublicationError('idempotency_conflict')
            return self.get(existing['id'])
        snap = self._snapshot(session_id)
        if snap['diff_hash'] != expected_diff_hash:
            raise PublicationError('stale_diff')
        identity = self._identity()
        self._check_access(snap['repository_id'])
        if snap['policy'] == 'on_success_draft' and not self._automatic_ok(snap):
            raise PublicationError('automatic_policy_not_satisfied')
        if snap['policy'] == 'manual_draft' and not snap['validations'].get('fully_validated'):
            disclosure = '\n\n## Local validation\n\n' + canonical(snap['validations'].get('summary', {}))
            body = body[:65536-len(disclosure)] + disclosure
        now, ident = self.clock(), new_id('pub')
        stamp = datetime.fromtimestamp(now / 1000, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
        suffix = request_hash[:10]
        branch = 'opendevengine/%s-%s' % (re.sub('[^A-Za-z0-9._-]', '-', session_id)[:80], suffix)
        message = ('OpenDevEngine session %s\n\nPlan-Version: %s' % (session_id, snap['plan_version_id']))[:4096]
        try:
            with self.store.transaction() as db:
                again = db.execute('SELECT * FROM publications WHERE idempotency_key=? OR session_id=?',
                                   (idempotency_key, session_id)).fetchone()
                if again:
                    if again['idempotency_key'] != idempotency_key or again['request_hash'] != request_hash:
                        raise PublicationError('idempotency_conflict')
                    return self._public(again)
                job = self.jobs.enqueue_in_transaction(db, 'publication.run', {'publication_id': ident},
                                                       effect='external', max_attempts=1,
                                                       idempotency_key='publication.run:' + ident)
                db.execute('INSERT INTO publications(id,session_id,repository_id,job_id,idempotency_key,request_hash,'
                           'intent_version,policy,expected_diff_hash,checkpoint_artifact_id,patch_artifact_id,'
                           'expected_base_sha,base_branch,head_branch,title,body,commit_name,commit_email,commit_date,'
                           'commit_message,state,created_at,updated_at) VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (ident, session_id, snap['repository_id'], job.id, idempotency_key, request_hash, snap['policy'],
                            expected_diff_hash, snap['checkpoint_artifact_id'], snap['patch_artifact_id'], snap['base_sha'],
                            snap['base_branch'], branch, title, body, identity['commit_name'], identity['commit_email'],
                            stamp, message, 'queued', now, now))
                self._event(db, ident, 'publication.checks',
                            {'fully_validated': snap['validations'].get('fully_validated') is True})
                self._event(db, ident, 'publication.state', {'state': 'queued'})
        except sqlite3.IntegrityError:
            raise PublicationError('idempotency_conflict') from None
        return self.get(ident)

    def _row(self, db, ident):
        row = db.execute('SELECT * FROM publications WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise PublicationError('not_found')
        return row

    def _public(self, row):
        result = {k: row[k] for k in ('id','session_id','repository_id','intent_version','policy','expected_diff_hash',
                                    'expected_base_sha','base_branch','head_branch','state','remote_base_sha','commit_sha',
                                    'head_sha','pull_request_id','pull_request_number','pull_request_url',
                                    'pull_request_state','error_code','created_at','updated_at','finished_at')}
        result['checks'] = {'status': 'unavailable'}
        with self.store.read() as db:
            event = db.execute("SELECT payload_json FROM publication_events WHERE publication_id=? "
                               "AND type='publication.checks' ORDER BY seq DESC LIMIT 1", (row['id'],)).fetchone()
        if event:
            result['checks'] = json.loads(event['payload_json'])
        return result

    def get(self, publication_id):
        with self.store.read() as db:
            return self._public(self._row(db, publication_id))

    def _event(self, db, ident, kind, payload):
        seq = db.execute('SELECT coalesce(max(seq),0)+1 FROM publication_events WHERE publication_id=?', (ident,)).fetchone()[0]
        db.execute('INSERT INTO publication_events VALUES(?,?,?,?,?)', (ident, seq, kind, canonical(payload), self.clock()))

    def _state(self, ident, state, *, error=None, **fields):
        now = self.clock()
        values = dict(fields, state=state, error_code=error, updated_at=now)
        if state in TERMINAL:
            values['finished_at'] = now
        with self.store.transaction() as db:
            row = self._row(db, ident)
            running = RUN_JOB.get()
            if running is not None:
                job = db.execute('SELECT * FROM jobs WHERE id=?', (running.id,)).fetchone()
                if (row['job_id'] != running.id or job['state'] != 'leased' or
                        job['lease_owner'] != running.lease_owner or job['lease_expires_at'] < self.jobs.clock()):
                    raise PublicationError('publication_stopped')
            if row['state'] in TERMINAL:
                raise PublicationError(row['error_code'] or 'publication_stopped')
            db.execute('UPDATE publications SET '+','.join(k+'=?' for k in values)+' WHERE id=?', (*values.values(), ident))
            self._event(db, ident, 'publication.state', {'state': state, 'error_code': error})
            return self._public(self._row(db, ident))

    # ---------------------------------------------------------- remote run
    def _bound_snapshot(self, row):
        snap = self._snapshot(row['session_id'])
        bindings = {'diff_hash':'expected_diff_hash', 'base_sha':'expected_base_sha',
                    'repository_id':'repository_id', 'checkpoint_artifact_id':'checkpoint_artifact_id',
                    'patch_artifact_id':'patch_artifact_id', 'base_branch':'base_branch', 'policy':'policy'}
        if any(snap[k] != row[v] for k, v in bindings.items()):
            raise PublicationError('stale_diff')
        if snap['policy'] == 'on_success_draft' and not self._automatic_ok(snap):
            raise PublicationError('automatic_policy_not_satisfied')
        return snap

    def _read_remote(self, repository_id):
        repo = self.github.get_repository(repository_id)
        token = self.github.mint_installation_token(repository_id,
                    {'contents':'read', 'pull_requests':'read'})['token']
        return repo, token

    def _remote(self, row):
        snap = self._bound_snapshot(row)
        repo = self._check_access(row['repository_id'])
        token = self.github.mint_installation_token(row['repository_id'],
                    {'contents':'write','pull_requests':'write'})['token']
        return snap, repo, token

    def _fence(self, ident, job, context, *, snapshot=True):
        with self.store.read() as db:
            row = dict(self._row(db, ident))
        current_job = self.jobs.get(row['job_id'])
        if (row['state'] not in ('queued','preparing','creating_objects','creating_branch','creating_pr') or row['job_id'] != job.id or
                current_job.state != 'leased' or current_job.lease_owner != job.lease_owner or
                current_job.lease_expires_at < self.jobs.clock() or
                current_job.cancel_requested or (context and context.cancel_requested)):
            raise PublicationError(row['error_code'] or 'publication_stopped')
        if snapshot:
            self._bound_snapshot(row)
        elif hasattr(self.sessions, 'source_policy'):
            # Blob objects share one bound capture. Check live source authority
            # without decoding that capture again for every file.
            try:
                available = self.sessions.source_policy(row['session_id'])['available']
            except Exception:
                raise PublicationError('source_unavailable') from None
            if not available:
                raise PublicationError('source_unavailable')
        self._check_access(row['repository_id'])

    def _divergence(self, snap, repo, token, current):
        if current == snap['base_sha']:
            return
        comparison = self.github.client.compare(token, repo['owner'], repo['name'], snap['base_sha'], current)
        if not comparison['complete']:
            raise PublicationError('divergence_unknown')
        paths = {p for item in comparison['files'] for p in
                 (item['filename'], item.get('previous_filename')) if p}
        changed = set(snap['checkpoint'].get('changed', {})) | set(snap['checkpoint'].get('deleted', []))
        if changed & paths:
            raise PublicationError('base_conflict')

    @staticmethod
    def _entries(checkpoint, client, token, owner, name, authority):
        changed, modes = checkpoint.get('changed', {}), checkpoint.get('modes', {})
        deleted = checkpoint.get('deleted', [])
        if not isinstance(changed, dict) or not isinstance(modes, dict) or not isinstance(deleted, list):
            raise PublicationError('session_snapshot_invalid')
        if set(r['path'] for r in checkpoint.get('rejected', [])) & (set(changed) | set(deleted)):
            raise PublicationError('rejected_paths')
        entries = []
        for path in sorted(changed):
            authority()
            if not PATH.fullmatch(path) or modes.get(path) not in ('100644','100755') or not isinstance(changed[path], str):
                raise PublicationError('session_snapshot_invalid')
            try:
                data = base64.b64decode(changed[path], validate=True)
            except (ValueError, TypeError):
                raise PublicationError('session_snapshot_invalid') from None
            sha = client.create_blob(token, owner, name, data)
            entries.append({'path': path, 'mode': modes[path], 'type': 'blob', 'sha': sha})
        for path in sorted(deleted):
            authority()
            if not PATH.fullmatch(path) or modes.get(path) not in ('100644','100755'):
                raise PublicationError('session_snapshot_invalid')
            entries.append({'path': path, 'mode': modes[path], 'type': 'blob', 'sha': None})
        return entries

    def run(self, job, context):
        marker = RUN_JOB.set(job)
        try:
            return self._run(job, context)
        finally:
            RUN_JOB.reset(marker)

    def _record_facts(self, ident, **facts):
        # Confirmed remote identifiers survive a concurrent purge, without source.
        with self.store.transaction() as db:
            db.execute('UPDATE publications SET ' + ','.join(k+'=?' for k in facts) + ' WHERE id=?',
                       (*facts.values(), ident))

    def _run(self, job, context):
        ident = job.payload.get('publication_id')
        with self.store.read() as db:
            row = dict(self._row(db, ident))
        if row['state'] in TERMINAL:
            return {'publication_id': ident, 'state': row['state']}
        if row['state'] == 'reconciling':
            raise UnknownOutcomeError()
        try:
            # Empty changes are a local trusted-snapshot decision: never mint a
            # write token or make a remote observation for an empty operation.
            snap = self._bound_snapshot(row)
            changed_paths = set(snap['checkpoint'].get('changed', {})) | set(snap['checkpoint'].get('deleted', []))
            if not changed_paths:
                return self._state(ident, 'no_changes')
            snap, repo, token = self._remote(row)
            authority = lambda: self._fence(ident, job, context)
            authority()
            if row['state'] == 'creating_pr':
                return self._publish_pr(ident, row, repo, token, authority)
            if row['state'] == 'creating_branch' and row['commit_sha']:
                row = self._create_branch(ident, row, repo, token, row['commit_sha'], authority)
                self._state(ident, 'creating_pr', branch_created=1, head_sha=row['commit_sha'])
                return self._publish_pr(ident, row, repo, token, authority)
            authority()
            self._state(ident, 'preparing')
            authority()
            current = self.github.client.default_branch_sha(token, repo['owner'], repo['name'], row['base_branch'])
            self._divergence(snap, repo, token, current)
            base = self.github.client.commit(token, repo['owner'], repo['name'], row['expected_base_sha'])
            self._state(ident, 'creating_objects', remote_base_sha=current, base_tree_sha=base['tree_sha'])
            authority()
            entries = self._entries(snap['checkpoint'], self.github.client, token, repo['owner'], repo['name'],
                                    lambda: self._fence(ident, job, context, snapshot=False))
            authority()
            tree = self.github.client.create_tree(token, repo['owner'], repo['name'], base['tree_sha'], entries)
            identity = {'name': row['commit_name'], 'email': row['commit_email'], 'date': row['commit_date']}
            self._record_facts(ident, tree_sha=tree)
            self._state(ident, 'creating_objects', tree_sha=tree)
            authority()
            commit = self.github.client.create_commit(token, repo['owner'], repo['name'], row['commit_message'],
                                                      tree, row['expected_base_sha'], identity)
            self._record_facts(ident, tree_sha=tree, commit_sha=commit)
            self._state(ident, 'creating_branch', tree_sha=tree, commit_sha=commit)
            authority()
            row = self._create_branch(ident, row, repo, token, commit, authority)
            self._state(ident, 'creating_pr', branch_created=1, head_sha=commit)
            return self._publish_pr(ident, dict(row, commit_sha=commit), repo, token, authority)
        except UnknownOutcomeError:
            raise
        except PublicationError as exc:
            current = self.get(ident)
            if (current['state'] in TERMINAL or self._job_id(ident) != job.id or
                    self.jobs.get(job.id).state != 'leased'):
                return current
            return self._state(ident, 'blocked', error=exc.code)
        except GitHubError as exc:
            code = 'publication_permission_denied' if exc.code in ('auth','capability_missing','repository_unavailable','not_found') else 'github_unavailable'
            if code == 'github_unavailable':
                self._state(ident, 'reconciling', error=code)
                raise UnknownOutcomeError() from None
            return self._state(ident, 'blocked', error=code)

    def _create_branch(self, ident, row, repo, token, commit, authority):
        """Create/adopt without updates; collision names are durably advanced."""
        base = row['head_branch']
        for ordinal in range(6):
            branch = base if ordinal == 0 else '%s-%d' % (base, ordinal)
            if branch != row['head_branch']:
                with self.store.transaction() as db:
                    db.execute('UPDATE publications SET head_branch=?,updated_at=? WHERE id=?',
                               (branch, self.clock(), ident))
                    self._event(db, ident, 'publication.branch_collision', {'head_branch': branch})
                row = dict(row, head_branch=branch)
            authority()
            try:
                created = self.github.client.create_ref(token, repo['owner'], repo['name'], branch, commit)
            except GitHubError as exc:
                if exc.code in ('timeout','upstream_unavailable'):
                    self._state(ident, 'reconciling', error='branch_outcome_unknown')
                    raise UnknownOutcomeError() from None
                raise
            if created:
                self._record_facts(ident, branch_created=1, head_sha=commit)
                return row
            try:
                existing = self.github.client.get_ref(token, repo['owner'], repo['name'], branch)
            except GitHubError as exc:
                if exc.code == 'not_found':
                    continue
                raise
            if existing == commit:
                return row
        raise PublicationError('branch_collision')

    def _publish_pr(self, ident, row, repo, token, authority):
        found = self._find_pr(row, repo, token)
        if found is not None:
            return self._confirm_pr(ident, row, repo, token, found)
        authority()
        try:
            value = self.github.client.create_pull_request(token, repo['owner'], repo['name'],
                        row['title'], row['body'], row['head_branch'], row['base_branch'])
            number = value.get('number') if isinstance(value, dict) else None
            if type(number) is not int or number <= 0:
                raise GitHubError('malformed_output')
            self._record_facts(ident, pull_request_number=number)
            self._state(ident, 'creating_pr', pull_request_number=number)
            return self._confirm_pr(ident, row, repo, token, value)
        except GitHubError:
            self._state(ident, 'reconciling', error='pr_outcome_unknown')
            raise UnknownOutcomeError() from None

    def _confirm_pr(self, ident, row, repo, token, value):
        number = value.get('number') if isinstance(value, dict) else None
        if type(number) is not int or number <= 0:
            raise PublicationError('pull_request_invalid')
        fetched = self.github.client.pull_request(token, repo['owner'], repo['name'], number)
        if (not isinstance(fetched, dict) or any(not isinstance(fetched.get(k), dict) or
                not isinstance(fetched[k].get('repo'), dict) for k in ('head', 'base'))):
            raise PublicationError('pull_request_invalid')
        head = fetched.get('head', {}).get('sha')
        head_ref = fetched.get('head', {}).get('ref')
        head_repo = fetched.get('head', {}).get('repo', {}).get('full_name')
        base = fetched.get('base', {}).get('ref')
        base_repo = fetched.get('base', {}).get('repo', {}).get('full_name')
        url = fetched.get('html_url')
        provider_id = fetched.get('id')
        state = 'merged' if fetched.get('merged') is True else fetched.get('state')
        if ((head != row.get('commit_sha') and head != row.get('head_sha')) or head_ref != row['head_branch']
                or head_repo != repo['full_name'] or base_repo != repo['full_name']
                or base != row['base_branch'] or fetched.get('draft') is not True):
            raise PublicationError('pull_request_mismatch')
        expected_url = 'https://github.com/%s/pull/%d' % (repo['full_name'], number)
        if (fetched.get('number') != number or state not in ('open','closed','merged') or type(provider_id) is not int or provider_id <= 0
                or url != expected_url or not SAFE_URL.fullmatch(url)):
            raise PublicationError('pull_request_invalid')
        return self._state(ident, 'published', pull_request_id=str(fetched.get('id')),
                           pull_request_number=number, pull_request_url=url, pull_request_state=state,
                           head_sha=head, branch_created=1)

    # ------------------------------------------------------ reconciliation
    def _find_pr(self, row, repo, token):
        values = self.github.client.pull_requests(token, repo['owner'], repo['name'],
                                                  repo['owner'] + ':' + row['head_branch'], row['base_branch'])
        if not isinstance(values, list) or any(not isinstance(v, dict) or
                not isinstance(v.get('head'), dict) or not isinstance(v.get('base'), dict) or
                any(not isinstance(v[k].get('repo'), dict) for k in ('head', 'base')) for v in values):
            raise PublicationError('pull_request_invalid')
        matches = [v for v in values if v['head'].get('sha') == row['commit_sha']
                   and v['head'].get('ref') == row['head_branch'] and v['base'].get('ref') == row['base_branch']]
        if len(matches) > 1:
            raise PublicationError('pull_request_conflict')
        return matches[0] if matches else None

    @serialized_recovery
    def reconcile(self, publication_id):
        with self.store.read() as db:
            row = dict(self._row(db, publication_id))
        if row['state'] != 'reconciling':
            return self._public(row)
        try:
            if not row['commit_sha']:
                return self._state(publication_id, 'queued')
            repo, token = self._read_remote(row['repository_id'])
            if row['pull_request_number']:
                return self._confirm_pr(publication_id, row, repo, token, {'number': row['pull_request_number']})
            try:
                head = self.github.client.get_ref(token, repo['owner'], repo['name'], row['head_branch'])
            except GitHubError as exc:
                if exc.code == 'not_found':
                    return self._state(publication_id, 'creating_branch', error=None)
                raise
            if head != row['commit_sha']:
                if row['branch_created']:
                    return self._state(publication_id, 'blocked', error='branch_collision')
                return self._state(publication_id, 'creating_branch', head_branch=row['head_branch'] + '-' + new_id('c')[-8:])
            with self.store.transaction() as db:
                db.execute('UPDATE publications SET branch_created=1,head_sha=?,updated_at=? WHERE id=?',
                           (head, self.clock(), publication_id))
            found = self._find_pr(row, repo, token)
            if found is None:
                return self._state(publication_id, 'creating_pr', error=None)
            return self._confirm_pr(publication_id, dict(row, head_sha=head), repo, token, found)
        except PublicationError as exc:
            if self.get(publication_id)['state'] in TERMINAL:
                return self.get(publication_id)
            return self._state(publication_id, 'blocked', error=exc.code)
        except GitHubError:
            return self.get(publication_id)

    def _job_id(self, ident):
        with self.store.read() as db:
            return self._row(db, ident)['job_id']

    @serialized_recovery
    def recover(self):
        self.jobs.reconcile_expired()
        with self.store.read() as db:
            rows = [dict(r) for r in db.execute("SELECT p.* FROM publications p JOIN jobs j ON j.id=p.job_id WHERE j.state='needs_reconciliation'")]
        summary = {'resolved': 0, 'pending': 0}
        for row in rows:
            try:
                # Recheck both identities while holding the write lock. A stale
                # observation must never overwrite a replacement worker's state.
                with self.store.transaction() as db:
                    current = db.execute("SELECT p.* FROM publications p JOIN jobs j ON j.id=p.job_id "
                                         "WHERE p.id=? AND p.job_id=? AND j.state='needs_reconciliation'",
                                         (row['id'], row['job_id'])).fetchone()
                    if current is None:
                        continue
                    if current['state'] not in TERMINAL:
                        db.execute("UPDATE publications SET state='reconciling',updated_at=? WHERE id=?",
                                   (self.clock(), row['id']))
                        self._event(db, row['id'], 'publication.state', {'state': 'reconciling'})
                result = self.reconcile(row['id'])
                if result['state'] != 'reconciling':
                    try:
                        self.jobs.resolve_reconciliation(row['job_id'],
                            outcome='succeeded' if result['state'] == 'published' else 'failed')
                    except InvalidTransition:
                        if self.jobs.get(row['job_id']).state == 'needs_reconciliation':
                            raise
                    summary['resolved'] += 1
                else:
                    summary['pending'] += 1
            except (PublicationError, GitHubError, InvalidTransition):
                summary['pending'] += 1
        return summary

    @serialized_recovery
    def retry(self, publication_id, idempotency_key):
        if not isinstance(idempotency_key, str) or not KEY.fullmatch(idempotency_key):
            raise PublicationError('validation')
        self.recover()
        resolved = self.get(publication_id)
        if resolved['state'] not in ('queued','creating_branch','creating_pr'):
            if resolved['state'] in TERMINAL:
                return resolved
            raise PublicationError('reconciliation_pending')
        with self.store.transaction() as db:
            row = self._row(db, publication_id)
            current_job = db.execute('SELECT state FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
            if current_job['state'] == 'needs_reconciliation' or row['state'] == 'reconciling':
                raise PublicationError('reconciliation_pending')
            if row['state'] in TERMINAL:
                return self._public(row)
            desired_key = 'publication.retry:' + publication_id + ':' + idempotency_key
            active = db.execute("SELECT idempotency_key FROM jobs WHERE kind='publication.run' AND payload=? "
                                "AND state IN ('queued','leased') ORDER BY created_at,id LIMIT 1",
                                (canonical({'publication_id': publication_id}),)).fetchone()
            if active and active['idempotency_key'] != desired_key:
                raise PublicationError('idempotency_conflict')
            job = self.jobs.enqueue_in_transaction(db, 'publication.run', {'publication_id': publication_id},
                                                   effect='external', max_attempts=1,
                                                   idempotency_key=desired_key)
            db.execute("UPDATE publications SET job_id=?,error_code=NULL,updated_at=? WHERE id=?",
                       (job.id, self.clock(), publication_id))
            self._event(db, publication_id, 'publication.retry', {'state': row['state']})
        return self.get(publication_id)

    def refresh(self, publication_id):
        with self.store.read() as db:
            row = dict(self._row(db, publication_id))
        if row['pull_request_number'] is None:
            return self._public(row)
        try:
            repo, token = self._read_remote(row['repository_id'])
            pr = self.github.client.pull_request(token, repo['owner'], repo['name'], row['pull_request_number'])
            if not isinstance(pr, dict) or type(pr.get('number')) is not int or pr['number'] != row['pull_request_number']:
                raise PublicationError('pull_request_invalid')
            state = 'merged' if pr.get('merged') is True else pr.get('state')
            if state not in ('open','closed','merged'):
                raise PublicationError('pull_request_invalid')
            with self.store.transaction() as db:
                db.execute('UPDATE publications SET pull_request_state=?,updated_at=? WHERE id=?',
                           (state, self.clock(), publication_id))
            return self.get(publication_id)
        except (GitHubError, PublicationError):
            return self._public(row)

    def purge_repository(self, repository_id):
        with self.store.transaction() as db:
            rows = db.execute('SELECT id,state,job_id FROM publications WHERE repository_id=?', (repository_id,)).fetchall()
            for row in rows:
                db.execute("UPDATE publications SET title='[purged]',body='[purged]',commit_message='[purged]' WHERE id=?", (row['id'],))
                if row['state'] not in TERMINAL:
                    db.execute("UPDATE publications SET state='blocked',error_code='repository_purged',"
                               'checkpoint_artifact_id=NULL,patch_artifact_id=NULL,updated_at=?,finished_at=? WHERE id=?',
                               (self.clock(), self.clock(), row['id']))
                    self._event(db, row['id'], 'publication.state', {'state':'blocked','error_code':'repository_purged'})
                else:
                    db.execute('UPDATE publications SET checkpoint_artifact_id=NULL,patch_artifact_id=NULL,updated_at=? WHERE id=?',
                               (self.clock(), row['id']))
        for row in rows:
            job = self.jobs.get(row['job_id'])
            if job.state in ('queued', 'leased'):
                self.jobs.cancel(job.id)
        return {'repository_id': repository_id, 'purged': True}
