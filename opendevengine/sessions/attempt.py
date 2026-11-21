"""One fenced session attempt: source, container, broker, adapter, validation, containment.

Every durable write verifies the live job lease (owner token and expiry) and that the
session still points at this job. A lost lease stops tools and source immediately and
then writes nothing. The broker guard stays closed until the adapter verifies its exact
trusted tool inventory. Tool admission is transactional: the ``tool.started`` record
commits only if, inside that same transaction, the durable stop flag is clear and
lease, access, exclusion policy, local-only and profile consent are all still valid;
otherwise the runner is never invoked. The stop Event is fresh per attempt and set by
the adapter on EVERY exit; user cancellation comes from the durable stop flag.

Revocation, purge and policy changes (``REVOKED_REASONS``) durably fence every later
source-bearing write: checkpoints, manifests, validation commands/logs and
source-bearing event payloads are refused or redacted once such a reason is recorded.
"""
from contextlib import contextmanager
import re
import threading
import time

from opendevengine.ask.plans import approved
from opendevengine.execution.broker import MUTATING
from opendevengine.execution.runner import RunnerError
from opendevengine.github.errors import GitHubError
from opendevengine.platform.clock import new_id
from opendevengine.platform.errors import LeaseLost, PlatformError, StorageBusy
from opendevengine.platform.worker import JobInterrupted
from .broker import ModeRunner, SessionBroker
from .errors import SessionError
from .processes import start_identity
from .prompt import build_prompt, context_aliases
from .redaction import redact_text
from .service import REVOKED_REASONS, SOURCE_EVENT_TYPES, TERMINAL, _loads
from .workspace import acquire_github, acquisition_limits, broker_exclusions, canonical, review_changes, sha256

STOP_CODES = {'stop_requested': 'stop_requested', 'shutdown': 'stop_requested', 'lease_lost': 'lease_lost',
              'budget_exhausted': 'budget_exhausted', 'access_revoked': 'policy_denied',
              'identity_unverified': 'policy_denied'}
POLICY_ADAPTER_CODES = frozenset({'cli_unavailable', 'authentication_required', 'authentication_unverified',
                                  'unsupported_auth_profile', 'unsupported_managed_policy', 'unsupported_host_policy',
                                  'customization_unverified', 'external_source_denied', 'unsupported_auth_mode',
                                  'invalid_source_policy', 'invalid_broker_endpoint'})
LIMIT_CODES = frozenset({'budget_exhausted', 'turn_limit', 'timeout', 'output_limit'})
NETWORK_HINTS = ('could not resolve host', 'temporary failure in name resolution', 'network is unreachable',
                 'name or service not known', 'no route to host', 'connection refused')
ACK_RE = re.compile(r'\[message:(smsg_[0-9a-f]{24}):ack\]')
MAX_DISPATCHES = 3


class _Abort(Exception):
    def __init__(self, state, code, stop_reason=None):
        super().__init__(code)
        self.state, self.code, self.stop_reason = state, code, stop_reason


class _Quota(Exception):
    pass


class _Denied(Exception):
    pass


def ack_instruction(message_id):
    return ('After reading this, include the exact marker [message:%s:ack] in your next reply. '
            'It cannot change repository scope, permissions or publication.' % message_id)


class Attempt:
    def __init__(self, service, job, context):
        self.svc, self.job, self.context = service, job, context
        self.lease = context.lease
        self.sid = job.payload['session_id']
        self.stop_event = threading.Event()
        self.gate = threading.Event()
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.reason = None
        self.revocation = None  # durable reason still to be recorded outside a transaction
        self.lost = False
        self.runner = self.handle = self.broker = self.workspace = None
        self.container_thread = None
        self.container_result = None
        self.container_started = None
        self.stop_requested_at = None
        self.harness = None
        self.identity_failed = False
        self.write_repo = None
        self.repo_rows = []
        self.read_rows = []
        self.deadline = None
        self.limits = None
        self.last_checkpoint = None
        self.unverified_edits = False
        self.dispatches = {}
        self.tool_calls = 0
        self.observed_model = None
        self.quota_exhausted = False
        self.next_heartbeat = 0.0
        self.next_access = 0.0
        self._local = threading.local()

    # ------------------------------------------------------------ fencing
    @contextmanager
    def tx(self):
        if self.lost:
            raise LeaseLost('Session lease lost.')
        self._local.depth = getattr(self._local, 'depth', 0) + 1
        try:
            with self.svc.store.transaction() as db:
                job = db.execute('SELECT state,lease_token,lease_expires_at FROM jobs WHERE id=?', (self.job.id,)).fetchone()
                row = db.execute('SELECT * FROM sessions WHERE id=?', (self.sid,)).fetchone()
                if (job is None or job['state'] != 'leased' or job['lease_token'] != self.lease.token
                        or job['lease_expires_at'] <= self.svc.clock() or row is None or row['job_id'] != self.job.id):
                    raise LeaseLost('Session lease lost.')
                row = dict(row)
                if row['stop_reason'] in REVOKED_REASONS:
                    self.terminate('access_revoked', kill=True, override=True)
                yield db, row
        except LeaseLost:
            self.lost = True
            self.terminate('lease_lost', kill=True)
            raise
        finally:
            self._local.depth -= 1

    @staticmethod
    def revoked(row):
        return row['stop_reason'] in REVOKED_REASONS

    def append(self, db, row, kind, payload):
        if self.revoked(row) and kind in SOURCE_EVENT_TYPES:
            payload = {'redacted': True}  # never persist source-bearing text after revocation/purge
        return self.svc.append_event(db, self.sid, kind, payload)

    def emit_event(self, kind, payload, *, critical=False):
        with self.tx() as (db, row):
            if not critical:
                count = db.execute('SELECT count(*) FROM session_events WHERE session_id=?', (self.sid,)).fetchone()[0]
                if count >= self.limits['service']['max_events']:
                    self.terminate('budget_exhausted', kill=False)
                    raise _Quota('event budget exhausted')
            return self.append(db, row, kind, payload)

    # ------------------------------------------------------- termination
    def terminate(self, reason, *, kill, override=False, durable=None):
        with self.lock:
            if self.reason is None or (override and self.reason in ('stop_requested', 'budget_exhausted')):
                self.reason = reason
                if reason in ('stop_requested', 'access_revoked') and self.stop_requested_at is None:
                    self.stop_requested_at = time.monotonic()
            if durable and self.revocation is None:
                self.revocation = durable
        self.stop_event.set()
        if kill:
            self._stop_container_async()

    def record_revocation(self):
        """Persist a locally detected revocation as a durable fence (never inside a tx)."""
        if getattr(self._local, 'depth', 0):
            return  # deferred: stop() opens its own transaction
        with self.lock:
            reason, self.revocation = self.revocation, None
        if reason and not self.lost:
            try:
                self.svc.stop(self.sid, reason=reason)
            except PlatformError:
                with self.lock:
                    self.revocation = self.revocation or reason

    def _stop_container_async(self):
        with self.lock:
            if self.handle is None or self.container_thread is not None:
                return
            self.container_started = time.monotonic()
            self.container_thread = threading.Thread(target=self._stop_container, daemon=True)
            self.container_thread.start()

    def _stop_container(self):
        try:
            result = self.runner.stop(self.handle)
        except RunnerError as exc:
            result = {'confirmed': False, 'code': exc.code}
        except Exception:
            result = {'confirmed': False, 'code': 'internal'}
        self.container_result = dict(result, elapsed_ms=int((time.monotonic() - self.container_started) * 1000))

    def contain(self):
        """Stop and remove the owned container; returns a containment summary."""
        harness_ok = True if self.harness is None else self.harness.get('stop_confirmed', False)
        if self.handle is not None:
            self._stop_container_async()
            self.container_thread.join(12)
            result = self.container_result or {'confirmed': False, 'code': 'containment_unverified', 'elapsed_ms': None}
            if result.get('confirmed'):
                try:
                    self.runner.discard(self.handle)
                    result['discarded'] = True
                except RunnerError as exc:
                    result['discarded'] = False
                    result['discard_code'] = exc.code
        else:
            result = {'confirmed': True, 'code': 'not_provisioned', 'elapsed_ms': 0}
        since = None
        if self.stop_requested_at is not None:
            since = int((time.monotonic() - self.stop_requested_at) * 1000)
        identity = 'unknown' if self.identity_failed else ('recorded' if self.harness else 'not_started')
        confirmed = bool(result.get('confirmed')) and bool(harness_ok) and not self.identity_failed
        return {'container_confirmed': bool(result.get('confirmed')), 'container_code': result.get('code'),
                'container_elapsed_ms': result.get('elapsed_ms'), 'harness_confirmed': bool(harness_ok),
                'harness_identity': identity, 'discarded': result.get('discarded', False),
                'since_stop_request_ms': since, 'confirmed': confirmed}

    # ----------------------------------------------------------- authority
    def _code(self):
        return STOP_CODES.get(self.reason, 'policy_denied')

    def policy(self, *, full):
        """Live policy outside any model prompt: None or a durable revocation reason."""
        try:
            if self.svc.providers.settings().get('local_only', True) is not False:
                return 'policy_changed'
        except Exception:
            return 'policy_changed'
        if full:
            try:
                current = self.svc.resolve_version(self.snapshot['profile_version_id'])
                if ((current.get('check') or {}).get('image_id') != self.snapshot['image_id']
                        or (current.get('config') or {}).get('allow_external_source') is not True):
                    return 'policy_changed'
            except Exception:
                return 'policy_changed'
            if self.write_repo is not None:
                try:
                    self.svc.write_access(self.write_repo)
                    for row in self.read_rows:
                        self.svc.read_context(row['repository_id'])  # includes current index eligibility
                except (SessionError, GitHubError, PlatformError):
                    return 'access_revoked'
        for row in self.repo_rows:
            reason = self.svc.repository_policy(row)
            if reason:
                return 'access_revoked' if reason == 'access_revoked' else 'policy_changed'
        return None

    def authority(self, *, full=False):
        """Return a denial code (and trigger containment) or None.

        Attempt authority is independent of the model-tool gate: the adapter sets the
        shared stop Event on every exit, so trusted post-agent validation/checkpoint
        consult termination reasons, lease, policy and deadline instead.
        """
        if self.reason is not None:
            return self._code()
        if self.svc._shutdown.is_set():
            self.terminate('shutdown', kill=True)
            return self._code()
        if self.deadline is not None and self.svc.clock() >= self.deadline:
            self.terminate('budget_exhausted', kill=False)
            return self._code()
        if self.quota_exhausted:
            self.terminate('budget_exhausted', kill=False)
            return self._code()
        try:
            with self.svc.store.read() as db:
                job = db.execute('SELECT state,lease_token,lease_expires_at,cancel_requested FROM jobs WHERE id=?', (self.job.id,)).fetchone()
                row = db.execute('SELECT stop_requested,stop_reason,job_id FROM sessions WHERE id=?', (self.sid,)).fetchone()
        except StorageBusy:
            return 'policy_denied'  # fail closed for this tool; the monitor retries
        if (job is None or job['state'] != 'leased' or job['lease_token'] != self.lease.token
                or job['lease_expires_at'] <= self.svc.clock() or row is None or row['job_id'] != self.job.id):
            self.lost = True
            self.terminate('lease_lost', kill=True)
            return self._code()
        if row['stop_requested'] or job['cancel_requested']:
            reason = row['stop_reason'] or 'user_requested'
            self.terminate('stop_requested' if reason == 'user_requested' else 'access_revoked', kill=True,
                           override=reason in REVOKED_REASONS)
            return self._code()
        reason = self.policy(full=full)
        if reason:
            self.terminate('access_revoked', kill=True, override=True, durable=reason)
            return self._code()
        return None

    def guard(self):
        if self.stop_event.is_set():
            return self._code()
        if not self.gate.is_set():
            return 'policy_denied'
        # Every broker call can expose repository source.  Re-resolve the
        # revocable profile consent/image at both broker boundaries instead
        # of relying on the asynchronous monitor's two-second cadence.
        return self.authority(full=True)

    def _monitor(self):
        interval = max(1.0, self.svc.lease_ms / 4000.0)
        while not self.done.wait(self.svc.poll_interval):
            now = time.monotonic()
            if now >= self.next_heartbeat:
                try:
                    beat = self.svc.jobs.heartbeat(self.lease, lease_ms=self.svc.lease_ms)
                    self.next_heartbeat = now + interval
                    if beat.cancel_requested:
                        self.terminate('stop_requested', kill=True)
                except LeaseLost:
                    self.lost = True
                    self.terminate('lease_lost', kill=True)
                except Exception:
                    self.next_heartbeat = now + 0.5  # storage contention: retry soon
            try:
                full = now >= self.next_access
                if full:
                    self.next_access = now + 2.0
                self.authority(full=full)
                self.record_revocation()
            except Exception:
                pass

    # ----------------------------------------------------------- admission
    def admit(self, payload):
        """Transactional tool admission shared with stop(): commits tool.started or refuses."""
        with self.tx() as (db, row):
            if row['stop_requested'] or row['state'] != 'running' or self.reason is not None:
                if row['stop_requested']:
                    reason = row['stop_reason'] or 'user_requested'
                    self.terminate('stop_requested' if reason == 'user_requested' else 'access_revoked', kill=True,
                                   override=reason in REVOKED_REASONS)
                raise _Denied('stop_requested')
            # Close the race between the broker's pre-call guard and durable
            # admission: consent/profile authority must still be current when
            # tool.started is committed.
            reason = self.policy(full=True)
            if reason:
                self.terminate('access_revoked', kill=True, override=True, durable=reason)
                raise _Denied(reason)
            count = db.execute('SELECT count(*) FROM session_events WHERE session_id=?', (self.sid,)).fetchone()[0]
            if count >= self.limits['service']['max_events']:
                self.terminate('budget_exhausted', kill=False)
                raise _Denied('budget_exhausted')
            if payload.get('tool') in MUTATING:
                self.unverified_edits = True  # set before the runner can mutate anything
            self.append(db, row, 'tool.started', payload)

    # -------------------------------------------------------------- events
    def broker_emit(self, kind, payload):
        if kind == 'tool_started':
            self.tool_calls += 1
            try:
                self.admit(payload)
            except _Denied as exc:
                try:
                    self.emit_event('tool.denied', {'tool': payload.get('tool'), 'code': str(exc), 'phase': 'admission'},
                                    critical=True)
                except Exception:
                    pass
                raise
        elif kind == 'tool_finished':
            self.emit_event('tool.finished', payload, critical=True)
        elif kind == 'tool_denied':
            self.emit_event('tool.denied', payload, critical=True)
        elif kind == 'checkpoint_failed':
            self.emit_event('checkpoint', {'ok': False, 'code': payload.get('code'), 'reason': payload.get('reason')}, critical=True)
        # broker_started/broker_closed/checkpoint_saved carry nothing new for users.

    def observe_acks(self, text):
        """Strip private acknowledgement markers; record only acks actually observed."""
        if not isinstance(text, str):
            return text
        ids = set(ACK_RE.findall(text))
        clean = ACK_RE.sub('', text).strip()
        if ids:
            with self.tx() as (db, row):
                for mid in sorted(ids):
                    hit = db.execute("UPDATE session_messages SET delivery='acknowledged',reason='model_marker_observed',"
                                     "updated_at=? WHERE id=? AND session_id=? AND delivery='delivered'",
                                     (self.svc.clock(), mid, self.sid)).rowcount
                    if hit:
                        self.svc.append_event(db, self.sid, 'message.delivery', {'message_id': mid, 'delivery': 'acknowledged',
                                                                                'basis': 'model_marker_observed'})
        return clean

    def adapter_emit(self, kind, payload):
        if kind == 'adapter_inventory':
            self.emit_event('agent.summary', {'kind': 'inventory', 'tools': payload.get('tools'),
                                              'servers': payload.get('servers'),
                                              'plugins_present': payload.get('plugins_present'),
                                              'commands_present': payload.get('commands_present')}, critical=True)
        elif kind == 'adapter_ready':
            self.observed_model = payload.get('model') or None
            self.emit_event('agent.summary', {'kind': 'ready', 'model': self.observed_model}, critical=True)
        elif kind == 'assistant_summary':
            self.emit_event('agent.summary', {'kind': 'assistant', 'text': self.observe_acks(payload.get('text', ''))})
        elif kind == 'adapter_result':
            self.emit_event('agent.summary', {'kind': 'result', 'status': payload.get('status'), 'code': payload.get('code'),
                                              'text': self.observe_acks(payload.get('summary', ''))}, critical=True)

    def inventory_verified(self):
        self.gate.set()

    def on_started(self, pid):
        try:
            identity = start_identity(pid)
        except Exception:
            identity = None
        self.harness = {'pid': pid, 'identity': identity}
        if identity is None:
            # Without an OS start identity, recovery could never prove this process
            # gone or ours: stop now and keep a durable containment block.
            self.identity_failed = True
            self.terminate('identity_unverified', kill=True)
        with self.tx() as (db, row):
            db.execute("UPDATE session_runners SET harness_pid=?,harness_start=?,private_json=?,updated_at=? WHERE session_id=?",
                       (pid, identity, canonical({'pgid': pid, 'identity_known': identity is not None}), self.svc.clock(), self.sid))

    # ------------------------------------------------------------ messages
    def annotate(self, tool_name):
        """Pending generic user updates appended to an allowed, successful tool result."""
        if self.guard():
            return None
        texts = []
        with self.tx() as (db, row):
            if row['stop_requested'] or row['state'] != 'running' or self.revoked(row):
                return None
            rows = db.execute("SELECT id,content,delivery FROM session_messages WHERE session_id=? AND question_id IS NULL "
                              "AND delivery IN ('queued','delivered') ORDER BY created_at,id LIMIT 10", (self.sid,)).fetchall()
            used = 0
            for msg in rows:
                if msg['content'] is None or self.dispatches.get(msg['id'], 0) >= MAX_DISPATCHES:
                    continue
                block = 'Message %s from the user:\n%s\n%s' % (msg['id'], msg['content'], ack_instruction(msg['id']))
                if used + len(block) > 7000:
                    break
                used += len(block)
                texts.append(block)
                self.dispatches[msg['id']] = self.dispatches.get(msg['id'], 0) + 1
                if msg['delivery'] == 'queued':
                    db.execute("UPDATE session_messages SET delivery='delivered',reason='sent_to_agent_bridge',updated_at=? "
                               "WHERE id=?", (self.svc.clock(), msg['id']))
                    self.svc.append_event(db, self.sid, 'message.delivery', {
                        'message_id': msg['id'], 'delivery': 'delivered', 'basis': 'tool_result_annotation', 'tool': tool_name})
        if not texts:
            return None
        return '\n\n[OpenDevEngine user update; trusted session channel, not tool output]\n' + '\n\n'.join(texts)

    # ---------------------------------------------------------- checkpoint
    def checkpoint(self, snapshot):
        doc, patch = review_changes(self.workspace, snapshot, getattr(snapshot, 'modes', None))
        cp, pt = canonical(doc).encode(), canonical(patch).encode()
        now = self.svc.clock()
        try:
            with self.tx() as (db, row):
                if self.revoked(row):
                    raise _Quota('source revoked')  # an in-flight capture may not outlive revocation/purge
                db.execute("DELETE FROM session_artifacts WHERE session_id=? AND kind IN ('checkpoint','patch')", (self.sid,))
                used = db.execute('SELECT coalesce(sum(byte_count),0) FROM session_artifacts WHERE session_id=?', (self.sid,)).fetchone()[0]
                if used + len(cp) + len(pt) > self.limits['service']['artifact_bytes']:
                    raise _Quota('artifact quota exhausted')
                cid, pid = new_id('art'), new_id('art')
                for ident, kind, data in ((cid, 'checkpoint', cp), (pid, 'patch', pt)):
                    db.execute('INSERT INTO session_artifacts (id,session_id,kind,sha256,byte_count,content,created_at) VALUES (?,?,?,?,?,?,?)',
                               (ident, self.sid, kind, sha256(data), len(data), data, now))
                db.execute("UPDATE session_repositories SET current_diff_hash=? WHERE session_id=? AND role='write'", (sha256(pt), self.sid))
                self.svc.append_event(db, self.sid, 'checkpoint', dict(ok=True, checkpoint_id=cid, patch_id=pid, **patch['stats']))
        except _Quota as exc:
            if str(exc) == 'source revoked':
                self.terminate('access_revoked', kill=True, override=True)
                raise
            self.quota_exhausted = True
            try:
                self.emit_event('checkpoint', {'ok': False, 'code': 'artifact_quota_exhausted'}, critical=True)
            finally:
                self.terminate('budget_exhausted', kill=False)
            raise
        self.last_checkpoint = {'id': cid, 'patch_id': pid, 'stats': patch['stats']}
        self.unverified_edits = False  # cleared ONLY by a verified, persisted capture
        return True

    def capture(self):
        return ModeRunner(self.runner).snapshot(self.handle)

    def final_checkpoint(self):
        if self.handle is None or self.lost or self.reason in ('access_revoked', 'lease_lost'):
            return False
        try:
            return self.checkpoint(self.capture())
        except (RunnerError, _Quota) as exc:
            if not isinstance(exc, _Quota):
                self.emit_event('checkpoint', {'ok': False, 'code': exc.code, 'reason': 'final'}, critical=True)
            return False

    # ------------------------------------------------------------ question
    def ask_user(self, question):
        if self.guard():
            return None
        qid = new_id('sq')
        now = self.svc.clock()
        try:
            with self.tx() as (db, row):
                if row['state'] != 'running' or row['stop_requested']:
                    return None
                db.execute("INSERT INTO session_questions (id,session_id,prompt,state,created_at,updated_at) VALUES (?,?,?,'open',?,?)",
                           (qid, self.sid, redact_text(question, 2000), now, now))
                self.svc.transition(db, row, 'waiting_for_user', phase='question')
                self.append(db, row, 'question.asked', {'question_id': qid, 'prompt': redact_text(question, 2000)})
        except SessionError:
            return None
        wait_until = time.monotonic() + self.limits['service']['question_wait_seconds']
        answer = None
        while time.monotonic() < wait_until and not self.stop_event.is_set():
            if self.authority():
                break
            with self.svc.store.read() as db:
                msg = db.execute("SELECT id,content FROM session_messages WHERE session_id=? AND question_id=? AND delivery='queued' "
                                 "ORDER BY created_at,id LIMIT 1", (self.sid, qid)).fetchone()
            if msg is not None:
                try:
                    with self.tx() as (db, row):
                        if row['state'] != 'waiting_for_user' or row['stop_requested'] or self.revoked(row):
                            break
                        t = self.svc.clock()
                        # Dispatched to the agent bridge; acknowledged only if the model echoes its marker.
                        db.execute("UPDATE session_messages SET delivery='delivered',reason='sent_to_agent_bridge',updated_at=? "
                                   "WHERE id=?", (t, msg['id']))
                        db.execute("UPDATE session_questions SET state='answered',answer_message_id=?,updated_at=? WHERE id=?", (msg['id'], t, qid))
                        self.svc.append_event(db, self.sid, 'message.delivery', {'message_id': msg['id'], 'delivery': 'delivered',
                                                                                'question_id': qid, 'basis': 'ask_user_result'})
                        self.svc.transition(db, row, 'running', phase='agent')
                    answer = msg['content'] + '\n\n[OpenDevEngine] ' + ack_instruction(msg['id'])
                except (SessionError, LeaseLost):
                    answer = None
                break
            self.stop_event.wait(0.1)
        if answer is None and not self.lost:
            try:
                with self.tx() as (db, row):
                    db.execute("UPDATE session_questions SET state='cancelled',updated_at=? WHERE id=? AND state='open'", (self.svc.clock(), qid))
                    self.svc.append_event(db, self.sid, 'question.asked', {'question_id': qid, 'state': 'cancelled',
                                                                          'reason': self.reason or 'timeout'})
                    if row['state'] == 'waiting_for_user':
                        self.svc.transition(db, row, 'running', phase='agent')
            except (SessionError, LeaseLost):
                pass
        return answer

    # ----------------------------------------------------------------- run
    def run(self):
        with self.tx() as (db, row):
            if row['state'] != 'queued':
                return {'session_id': self.sid, 'state': row['state']}
            self.limits = _loads(row['limits_json'], {})
            if row['stop_requested']:
                final = 'cancelled' if (row['stop_reason'] or 'user_requested') == 'user_requested' else 'blocked'
                self.svc.transition(db, row, final, stop_reason=row['stop_reason'] or 'user_requested',
                                    error_code=None if final == 'cancelled' else row['stop_reason'])
                return {'session_id': self.sid, 'state': final}
            self.svc.transition(db, row, 'provisioning', phase='access_check', started_at=self.svc.clock())
            self.session = row
            self.repo_rows = [dict(r) for r in db.execute('SELECT * FROM session_repositories WHERE session_id=? '
                                                          'ORDER BY role DESC,repository_id', (self.sid,))]
        self.repo_row = next(r for r in self.repo_rows if r['role'] == 'write')
        self.read_rows = [r for r in self.repo_rows if r['role'] == 'read']
        self.write_repo = self.repo_row['repository_id']
        self.snapshot = _loads(self.session['profile_snapshot_json'], {})
        self.svc._register(self)
        monitor = threading.Thread(target=self._monitor, daemon=True)
        monitor.start()
        try:
            try:
                outcome = self._execute()
            except _Abort as abort:
                outcome = self._finish(abort.state, abort.code, abort.stop_reason)
            return outcome
        except LeaseLost:
            self._contain_quietly()
            raise
        except JobInterrupted:
            raise
        except Exception as exc:
            code = getattr(exc, 'code', None) or 'internal'
            return self._finish('failed', code if isinstance(code, str) else 'internal', None)
        finally:
            self.done.set()
            monitor.join(2)
            if self.broker is not None:
                self.broker.close()
            self.svc._unregister(self)

    def _owned_leftover(self):
        """False when no owned container for this session remains; True/None otherwise."""
        try:
            handles = [h for h in self.runner.list_owned() if h.session_id == self.sid]
        except RunnerError:
            return None
        for handle in handles:
            try:
                if self.runner.stop(handle).get('confirmed'):
                    self.runner.discard(handle)
                    continue
            except RunnerError:
                pass
            return True
        return False

    def _contain_quietly(self):
        self.terminate('lease_lost', kill=True)
        if self.handle is not None:
            self.contain()

    def _check(self, phase):
        code = self.authority(full=True)
        if code:
            raise _Abort(*self._abort_for(code))
        with self.tx() as (db, row):
            if row['stop_requested'] or row['state'] == 'stopping':
                raise _Abort(*self._abort_for('stop_requested'))
            if phase:
                db.execute('UPDATE sessions SET phase=?,updated_at=? WHERE id=?', (phase, self.svc.clock(), self.sid))

    def _durable_reason(self):
        with self.svc.store.read() as db:
            row = db.execute('SELECT stop_reason FROM sessions WHERE id=?', (self.sid,)).fetchone()
        return row[0] if row else None

    def _abort_for(self, code):
        if self.reason == 'shutdown':
            return 'interrupted', 'shutdown', 'shutdown'
        if self.reason == 'identity_unverified':
            return 'failed', 'runtime_identity_unverified', None
        if self.reason == 'access_revoked' or code == 'policy_denied':
            self.record_revocation()
            durable = self._durable_reason()
            reason = durable if durable in REVOKED_REASONS else 'access_revoked'
            return 'blocked', reason, reason
        if self.reason == 'budget_exhausted' or code == 'budget_exhausted':
            return 'failed', 'budget_exhausted', 'budget_exhausted'
        return 'cancelled', None, 'user_requested'

    def _acquire(self, row, limits, *, budget_bytes):
        pinned = _loads(row['source_manifest_json'], {})

        def authority():
            code = self.authority()
            if code:
                raise _Abort(*self._abort_for(code))
        caps = dict(limits)
        caps['max_total_bytes'] = min(caps['max_total_bytes'], budget_bytes)
        try:
            return acquire_github(self.svc.github, row['repository_id'], row['base_sha'], pinned.get('exclusions', []),
                                  caps, check_authority=authority)
        except SessionError as exc:
            state = 'blocked' if exc.code in ('source_access_denied', 'base_revision_not_git') else 'failed'
            code = exc.code if row['role'] == 'write' else 'read_context_' + exc.code
            self.emit_event('error', {'code': code, 'details': exc.details, 'role': row['role']}, critical=True)
            raise _Abort(state, code) from None

    def _execute(self):
        snap, limits = self.snapshot, self.limits
        # 1. Fresh policy, PINNED profile version, CLI and image checks before any source leaves storage.
        try:
            if self.svc.providers.settings().get('local_only', True):
                raise _Abort('blocked', 'execution_local_only')
        except PlatformError:
            raise _Abort('blocked', 'execution_local_only') from None
        try:
            current = self.svc.resolve_version(snap['profile_version_id'])
        except Exception as exc:
            raise _Abort('blocked', getattr(exc, 'code', None) or 'execution_profile_unavailable') from None
        check = current.get('check') or {}
        if (current.get('id') != snap['profile_version_id'] or check.get('image_id') != snap['image_id']
                or check.get('status') != 'passed' or (current.get('config') or {}).get('allow_external_source') is not True):
            raise _Abort('blocked', 'execution_profile_changed')
        self._check('content_check')
        plan = self.svc.plans.get(self.session['plan_id'], self.session['plan_version_id'])
        content = plan['version']['content']
        if content is None or plan['version']['content_hash'] != self.session['content_hash']:
            raise _Abort('blocked', 'plan_unavailable')
        repo = self.svc.github.get_repository(self.write_repo)
        prompt, error = build_prompt(content, plan_version_id=self.session['plan_version_id'],
                                     content_hash=self.session['content_hash'], repository_name=repo['full_name'],
                                     base_sha=self.repo_row['base_sha'], contexts=context_aliases(content))
        if error or sha256(prompt.encode()) != _loads(self.session['instructions_json'], {}).get('prompt_sha256'):
            raise _Abort('blocked', error or 'plan_changed')
        try:
            adapter = self.svc.adapter_factory()
            detected = adapter.preflight()
        except Exception as exc:
            raise _Abort('blocked', getattr(exc, 'code', None) or 'cli_unavailable') from None
        if snap.get('cli_version') and detected.get('version') != snap['cli_version']:
            raise _Abort('blocked', 'cli_version_changed')
        try:
            self.runner = self.svc.runner_factory(self.svc.instance_id())
            runtime = self.runner.preflight(snap['image_id'])
        except RunnerError as exc:
            raise _Abort('blocked', exc.code) from None
        if runtime.get('blocking_reasons') or runtime.get('image_id') != snap['image_id']:
            raise _Abort('blocked', 'runtime_unavailable')
        # 2. Exact pinned source via the GitHub API boundary (never a host checkout).
        self._check('acquiring_source')
        caps = acquisition_limits(limits['runner'])
        self.workspace = self._acquire(self.repo_row, caps, budget_bytes=caps['max_total_bytes'])
        context_files, context_manifests = {}, {}
        context_budget = caps['max_total_bytes'] // 2
        for row in self.read_rows:
            acquired = self._acquire(row, caps, budget_bytes=max(1, context_budget))
            alias = _loads(row['source_manifest_json'], {}).get('alias')
            context_budget -= acquired.manifest['counts']['bytes']
            for path, data in acquired.files.items():
                context_files[alias + '/' + path] = data
            context_manifests[row['repository_id']] = (alias, acquired.manifest)
        head = None
        try:
            token = self.svc.github.mint_installation_token(self.write_repo)['token']
            head = self.svc.github.client.default_branch_sha(token, repo['owner'], repo['name'], repo['default_branch'])
        except GitHubError:
            head = None  # freshness unknown; never block on an optional comparison
        finally:
            token = None
        manifest = self.workspace.manifest
        warnings = sorted({r for r in manifest['omitted'].values() if r in ('file_too_large', 'sensitive_content', 'unsupported_entry')})
        instance_id = self.svc.instance_id()
        with self.tx() as (db, row):
            if self.revoked(row):
                raise _Abort(*self._abort_for('policy_denied'))
            pinned = _loads(self.repo_row['source_manifest_json'], {})
            db.execute("UPDATE session_repositories SET base_manifest_json=?,source_manifest_json=? WHERE session_id=? AND role='write'",
                       (canonical(manifest), canonical(dict(pinned, counts=manifest['counts'], head_sha=head,
                                                            acquired_at=self.svc.clock())), self.sid))
            for read in self.read_rows:
                alias, read_manifest = context_manifests[read['repository_id']]
                read_pinned = _loads(read['source_manifest_json'], {})
                db.execute('UPDATE session_repositories SET base_manifest_json=?,source_manifest_json=? WHERE session_id=? AND repository_id=?',
                           (canonical(read_manifest), canonical(dict(read_pinned, counts=read_manifest['counts'],
                                                                     acquired_at=self.svc.clock())),
                            self.sid, read['repository_id']))
            self.svc.append_event(db, self.sid, 'session.state', {
                'state': 'provisioning', 'phase': 'source_acquired', 'counts': manifest['counts'], 'omission_reasons': warnings,
                'base_sha': self.repo_row['base_sha'], 'base_is_head': None if head is None else head == self.repo_row['base_sha'],
                'read_contexts': [{'repository_id': k, 'alias': v[0], 'counts': v[1]['counts']} for k, v in context_manifests.items()]})
            db.execute('INSERT INTO session_runners (session_id,instance_id,state,updated_at) VALUES (?,?,?,?) '
                       "ON CONFLICT(session_id) DO UPDATE SET state='provisioning',updated_at=excluded.updated_at",
                       (self.sid, instance_id, 'provisioning', self.svc.clock()))
        # 3. Isolated container: writable workspace with preserved modes plus read-only /context.
        self._check('provisioning_container')
        modes = {p: m['mode'] for p, m in manifest['files'].items() if m.get('mode') in ('100644', '100755')}
        try:
            handle = self.runner.provision(self.sid, self.workspace.files, image=snap['image_id'], limits=limits['runner'],
                                           file_modes=modes, context=context_files if self.read_rows else None)
        except RunnerError as exc:
            # provision() removes what it created; verify by label instead of assuming.
            leftover = self._owned_leftover()
            with self.tx() as (db, row):
                db.execute('UPDATE session_runners SET state=?,updated_at=? WHERE session_id=?',
                           ('terminated' if leftover is False else 'containment_failed', self.svc.clock(), self.sid))
            if leftover is not False:
                raise _Abort('failed', 'containment_failed') from None
            raise _Abort('blocked' if exc.code in ('preflight_failed', 'image_unsupported', 'docker_unavailable') else 'failed', exc.code) from None
        with self.lock:
            self.handle = handle
        self.deadline = self.svc.clock() + (limits['runner']['wall_seconds'] - 5) * 1000
        with self.tx() as (db, row):
            db.execute("UPDATE session_runners SET container_id=?,state='running',updated_at=? WHERE session_id=?",
                       (handle.container_id, self.svc.clock(), self.sid))
        if self.reason is not None:
            self._stop_container_async()
            raise _Abort(*self._abort_for(self._code()))
        self.checkpoint(self.capture())  # verified empty-diff baseline from inside the container
        with self.tx() as (db, row):
            if row['state'] != 'provisioning' or row['stop_requested']:
                raise _Abort(*self._abort_for('stop_requested'))
            self.svc.transition(db, row, 'running', phase='agent')
        # 4. Broker + adapter. The guard stays closed until the inventory is verified.
        caps_adapter = getattr(adapter, 'capabilities', {}) or {}
        self.broker = SessionBroker(self.runner, handle, annotate=self.annotate, guard=self.guard, emit=self.broker_emit,
                                    checkpoint=self.checkpoint, ask_user=self.ask_user if caps_adapter.get('ask_user') else None,
                                    excluded_paths=broker_exclusions(manifest))
        endpoint = self.broker.start(self.svc.broker_root)
        agent = limits['agent']
        remaining = max(1, int((self.deadline - self.svc.clock()) / 1000) - 10)
        started = time.monotonic()
        adapter_error = None
        try:
            result = adapter.run(endpoint, prompt, auth_mode=snap['auth_mode'], on_inventory_verified=self.inventory_verified,
                                 emit=self.adapter_emit, synthetic=False, local_only=False,
                                 external_source_consent=snap['allow_external_source'] is True, stop_event=self.stop_event,
                                 on_started=self.on_started, timeout_seconds=min(agent['wall_seconds'], remaining),
                                 max_turns=agent['max_turns'], max_budget_usd=agent['max_budget_usd'],
                                 max_output_bytes=agent['max_output_bytes'], model=snap.get('agent_model'))
        except LeaseLost:
            raise
        except Exception as exc:
            adapter_error = getattr(exc, 'code', None) or 'agent_error'
            # AdapterError/LeaseLost propagate only after run_stream contained the group;
            # an uncontained group surfaces as a returned containment_failure instead.
            result = {'status': 'failed', 'code': adapter_error, 'exit_code': None,
                      'stop_confirmed': True, 'usage': None, 'reported_cost_usd': None}
        self.stop_event.set()  # the adapter sets it on every exit; make it unconditional
        self.broker.close()
        self.harness = dict(self.harness or {}, stop_confirmed=result.get('stop_confirmed') is True)
        self.agent_ms = int((time.monotonic() - started) * 1000)
        self.result = result
        return self._after_agent(content, result, adapter_error)

    def _usage(self, result):
        usage = result.get('usage') if isinstance(result.get('usage'), dict) else None
        cost = result.get('reported_cost_usd')
        return {'tokens': usage, 'reported_cost_usd': cost, 'cost_status': 'reported' if cost is not None else 'unknown',
                'tokens_status': 'reported' if usage else 'unknown', 'agent_wall_ms': getattr(self, 'agent_ms', None),
                'tool_calls': self.tool_calls, 'observed_model': self.observed_model}

    def _after_agent(self, content, result, adapter_error):
        self.record_revocation()
        with self.svc.store.read() as db:
            row = dict(db.execute('SELECT stop_requested,stop_reason FROM sessions WHERE id=?', (self.sid,)).fetchone())
        if self.lost:
            raise LeaseLost('Session lease lost.')
        if result.get('stop_confirmed') is not True:
            self.emit_event('error', {'code': 'harness_containment_failed'}, critical=True)
            return self._finish('failed', 'containment_failed', None, content=content)
        if self.identity_failed:
            return self._finish('failed', 'runtime_identity_unverified', None, content=content)
        if self.reason == 'shutdown':
            return self._finish('interrupted', 'shutdown', 'shutdown', content=content)
        if row['stop_reason'] in REVOKED_REASONS or self.reason == 'access_revoked':
            reason = row['stop_reason'] if row['stop_reason'] in REVOKED_REASONS else 'access_revoked'
            return self._finish('blocked', reason, reason, content=content)
        user_stop = bool(row['stop_requested']) and (row['stop_reason'] or 'user_requested') == 'user_requested'
        if user_stop or self.reason == 'stop_requested':
            return self._finish('cancelled', None, 'user_requested', content=content)
        code = result.get('code')
        if self.reason == 'budget_exhausted' or code in LIMIT_CODES:
            self.final_checkpoint()
            return self._finish('failed', 'budget_exhausted' if self.reason == 'budget_exhausted' else code, 'budget_exhausted', content=content)
        if result.get('status') != 'completed':
            state = 'blocked' if (adapter_error or code) in POLICY_ADAPTER_CODES else 'failed'
            self.final_checkpoint()
            return self._finish(state, code or 'agent_error', None, content=content)
        # Completed agent run: trusted post-agent checkpoint and validation (gate stays closed).
        if not self.final_checkpoint() and self.last_checkpoint is None:
            return self._finish('failed', 'checkpoint_unavailable', None, content=content)
        try:
            with self.tx() as (db, row):
                if row['stop_requested'] or row['state'] != 'running':
                    raise _Abort(*self._abort_for('stop_requested' if not self.revoked(row) else 'policy_denied'))
                self.svc.transition(db, row, 'validating', phase='validation')
        except SessionError:
            raise _Abort(*self._abort_for('stop_requested')) from None
        self._validate(content)
        if self.reason is not None:
            raise _Abort(*self._abort_for(self._code()))
        self.final_checkpoint()
        return self._finish('completed', None, None, content=content)

    def _validate(self, content, *, run=True):
        items = content.get('validation') or []
        for ordinal, item in enumerate(items, 1):
            argv = ['sh', '-c', item['command']]
            env = {'image_id': self.snapshot.get('image_id'), 'network': 'none', 'user': '65534:65534', 'cwd': '/workspace',
                   'env': {'HOME': '/tmp', 'TMPDIR': '/tmp'}, 'shell': 'container sh -c'}
            status, exit_code, artifact, started, finished, log = 'not_run', None, None, None, None, None
            reason = None
            required = approved(item)
            if not required:
                reason = 'not_approved'  # never handed to the shell; the user did not approve this text
            elif run and self.reason is None and not self.lost:
                code = self.authority(full=True)
                remaining = int((self.deadline - self.svc.clock()) / 1000) - 5 if self.deadline else 0
                if code:
                    reason = code
                elif remaining < 1:
                    reason = 'deadline'
                else:
                    timeout = min(self.limits['service']['validation_timeout_seconds'], remaining, 600)
                    started = self.svc.clock()
                    self.emit_event('validation.started', {'ordinal': ordinal, 'command': redact_text(item['command'], 500)}, critical=True)
                    # A validation command may edit files: artifacts are unverified until recaptured.
                    self.unverified_edits = True
                    try:
                        res = self.runner.run_command(self.handle, argv, timeout_seconds=timeout)
                        exit_code = res['exit_code']
                        output = (res['stdout'] + ('\n' if res['stdout'] and res['stderr'] else '') + res['stderr'])
                        lowered = output.lower()
                        if res['timed_out']:
                            status = 'timeout'
                        elif exit_code == 0:
                            status = 'passed'
                        elif exit_code in (126, 127) or any(h in lowered for h in NETWORK_HINTS):
                            status = 'unavailable'
                        else:
                            status = 'failed'
                        log = redact_text(output, None).encode('utf-8')[:self.limits['runner']['max_output_bytes']]
                        if res.get('truncated'):
                            log += b'\n[output truncated]'
                        artifact = new_id('art')
                    except RunnerError as exc:
                        status, reason = 'error', exc.code
                    finished = self.svc.clock()
            elif not run:
                reason = 'agent_not_completed'
            else:
                reason = self.reason or 'stopped'
            with self.tx() as (db, row):
                revoked = self.revoked(row)
                if revoked:
                    artifact = None  # no source-derived log or command once revoked/purged
                if artifact is not None:
                    used = db.execute('SELECT coalesce(sum(byte_count),0) FROM session_artifacts WHERE session_id=?', (self.sid,)).fetchone()[0]
                    if used + len(log) > self.limits['service']['artifact_bytes']:
                        artifact, reason = None, 'artifact_quota_exhausted'
                    else:
                        db.execute('INSERT INTO session_artifacts (id,session_id,kind,sha256,byte_count,content,created_at) VALUES (?,?,?,?,?,?,?)',
                                   (artifact, self.sid, 'validation_log', sha256(log), len(log), log, self.svc.clock()))
                vid = new_id('val')
                command = ({'redacted': True} if revoked else
                           {'argv': argv, 'command': item['command'], 'expected': item['expected']})
                db.execute('INSERT INTO validation_runs (id,session_id,ordinal,command_json,source,status,required,exit_code,'
                           'artifact_id,environment_json,started_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                           (vid, self.sid, ordinal, canonical(command), item['source'], status, int(required), exit_code, artifact,
                            canonical(dict(env, reason=reason)), started, finished))
                self.append(db, row, 'validation.finished', {'ordinal': ordinal, 'validation_id': vid, 'status': status,
                                                            'exit_code': exit_code, 'reason': reason})
            if status != 'not_run' and self.handle is not None and self.reason is None:
                try:
                    self.checkpoint(self.capture())
                except (RunnerError, _Quota) as exc:
                    try:
                        self.emit_event('checkpoint', {'ok': False, 'code': getattr(exc, 'code', 'artifact_quota_exhausted'),
                                                       'reason': 'validation'}, critical=True)
                    except Exception:
                        pass

    def _finish(self, state, error_code, stop_reason, *, content=None):
        if self.lost:
            raise LeaseLost('Session lease lost.')
        self.record_revocation()
        # Containment always precedes the terminal record.
        if state in ('cancelled', 'blocked', 'interrupted') and self.reason is None:
            self.terminate({'cancelled': 'stop_requested', 'blocked': 'access_revoked', 'interrupted': 'shutdown'}[state], kill=True)
        containment = self.contain()
        if content is not None:
            with self.svc.store.read() as db:
                ran = db.execute('SELECT count(*) FROM validation_runs WHERE session_id=?', (self.sid,)).fetchone()[0]
            if not ran:
                self._validate(content, run=False)
        if self.lost:
            raise LeaseLost('Session lease lost.')
        with self.svc.store.read() as db:
            summary = dict(db.execute('SELECT status,count(*) FROM validation_runs WHERE session_id=? GROUP BY status', (self.sid,)).fetchall())
            total = sum(summary.values())
        changed = (self.last_checkpoint or {}).get('stats', {})
        rejected_count = changed.get('rejected', 0)
        if self.last_checkpoint:
            from .publication import is_omitted
            with self.svc.store.read() as db:
                capture = db.execute('SELECT content FROM session_artifacts WHERE id=?',
                                     (self.last_checkpoint['id'],)).fetchone()
            if capture and capture['content'] is not None:
                rejected_count = sum(not is_omitted(r) for r in _loads(bytes(capture['content']), {})['rejected'])
        if not containment['confirmed']:
            state = 'interrupted' if state == 'interrupted' else 'failed'
            error_code = 'runtime_identity_unverified' if self.identity_failed else 'containment_failed'
        result = getattr(self, 'result', {}) or {}
        stale = bool(self.unverified_edits)
        outcome = {'result': ('incomplete' if stale else 'reviewable' if changed.get('files') else 'no_changes')
                   if state == 'completed' else state,
                   'changed_files': changed.get('files', 0), 'rejected_paths': rejected_count,
                   'last_checkpoint_id': (self.last_checkpoint or {}).get('id'),
                   'edits_since_last_checkpoint_may_be_lost': stale, 'artifacts_stale': stale,
                   'validation': {'summary': summary, 'total': total,
                                  'fully_validated': bool(total) and summary.get('passed', 0) == total and not stale,
                                  'validated_tree_captured': not stale},
                   'adapter': {'status': result.get('status'), 'code': result.get('code'), 'exit_code': result.get('exit_code')},
                   'containment': containment}
        try:
            source_current = self.svc.source_policy(self.sid)['available']
        except Exception:
            source_current = False
        publication = _loads(self.session.get('publication_json'), {})
        outcome['publication_eligible'] = bool(
            publication.get('policy') == 'on_success_draft'
            and state == 'completed'
            and outcome['result'] == 'reviewable'
            and outcome['rejected_paths'] == 0
            and outcome['validation']['fully_validated']
            and containment['confirmed']
            and source_current
        )
        runner_state = 'terminated' if containment['confirmed'] else 'containment_failed'
        with self.tx() as (db, row):
            if row['state'] in TERMINAL:
                return {'session_id': self.sid, 'state': row['state']}
            if row['stop_reason'] in REVOKED_REASONS and state in ('completed', 'cancelled', 'failed'):
                state, error_code, stop_reason = 'blocked', row['stop_reason'], row['stop_reason']
                outcome['result'], outcome['publication_eligible'] = state, False
            elif state == 'completed' and (row['state'] == 'stopping' or row['stop_requested']):
                stop_reason = row['stop_reason'] or 'user_requested'
                state = 'cancelled'
                outcome['result'], outcome['publication_eligible'] = state, False
            if row['stop_reason'] == 'repository_purged' or _loads(row['outcome_json'], {}).get('source_redacted'):
                outcome['source_redacted'] = True
            db.execute('UPDATE session_runners SET state=?,updated_at=? WHERE session_id=?', (runner_state, self.svc.clock(), self.sid))
            self.svc.append_event(db, self.sid, 'containment', containment)
            if result:
                self.svc.append_event(db, self.sid, 'usage', self._usage(result))
            self.svc.transition(db, row, state, phase=state, error_code=error_code, stop_reason=stop_reason,
                                outcome_json=canonical(outcome), usage_json=canonical(self._usage(result)))
        if state == 'interrupted':
            raise JobInterrupted()
        return {'session_id': self.sid, 'state': state}
