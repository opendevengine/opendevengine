"""Application-owned services and a bounded, observable background job lifecycle."""
from threading import Event, Thread, RLock
import time

from .api import FeatureAPI
from .github import GitHubService
from .providers import ProviderService
from .platform import Worker
from .platform.worker import JobInterrupted
from .platform.clock import new_id
from .platform.errors import LeaseLost, PlatformError


class ShutdownContext:
    def __init__(self, context, stop_event):
        self.context, self.stop_event = context, stop_event
        self.queue, self.lease, self.lease_ms = context.queue, context.lease, context.lease_ms

    def checkpoint(self, data):
        self.context.checkpoint(data)
        if self.stop_event.is_set():
            raise JobInterrupted()

    @property
    def shutdown_requested(self):
        return self.stop_event.is_set()

    @property
    def cancel_requested(self):
        if self.stop_event.is_set():
            raise JobInterrupted()
        return self.context.cancel_requested


class ApplicationServices:
    def __init__(self, platform, *, github_transport=None, provider_transport=None, public_base_url='http://127.0.0.1:8765'):
        self.platform = platform
        self.public_base_url = public_base_url
        self.github = GitHubService(platform.store, platform.jobs, platform.secrets, transport=github_transport)
        self.providers = ProviderService(platform.store, platform.secrets, transport=provider_transport)
        from .providers.catalog import AIProviders
        self.ai_providers = AIProviders(platform.store, platform.secrets, self.providers)
        self.indexing = self.routing = self.plans = self.conversations = None
        self.execution_profiles = self.sessions = self.publications = None
        self.scheduler = None
        self.legacy_index = None
        self.legacy_sources = {}
        self.context_sources = {}
        self.context_lock = RLock()
        self.demo_loader = self._bundled_demos
        self.api = FeatureAPI(platform, self.github, self.providers, ai_providers=self.ai_providers,
                              public_base_url=public_base_url)
        self.stop_event = Event()
        self.thread = None
        self.error_code = None
        self.recovery_error = None
        self.recovering = False
        self.handlers = {'github.refresh': self.github.run_refresh, 'github.purge': self.github.run_purge}

    def _before_start(self):
        if self.thread is not None:
            raise RuntimeError('Service composition must finish before workers start.')

    def _rebuild_api(self):
        self.api = FeatureAPI(self.platform, self.github, self.providers,
                              indexing=self.indexing, routing=self.routing, plans=self.plans,
                              conversations=self.conversations, purge_context=self.purge_context,
                              select_context=self.select_context,
                              execution_profiles=self.execution_profiles, sessions=self.sessions,
                              publications=self.publications, ai_providers=self.ai_providers,
                              public_base_url=self.public_base_url)

    def enable_context(self, indexing):
        self._before_start()
        if self.indexing is not None:
            raise RuntimeError('Context service already configured.')
        self.indexing = indexing
        self.handlers['index.run'] = indexing.run_index
        self._rebuild_api()

    def enable_ask(self, routing, plans, conversations):
        self._before_start()
        if self.indexing is None or self.conversations is not None:
            raise RuntimeError('Configure context once before Ask.')
        self.routing, self.plans, self.conversations = routing, plans, conversations
        self.handlers['ask.answer'] = conversations.run_answer
        self.github.register_purge_hook('ask', conversations.purge_repository)
        self._rebuild_api()

    def enable_execution(self, execution_profiles=None, sessions=None, *, data_dir=None, publications=None):
        """Compose the dedicated execution runtime before application workers start.

        Tests may inject the two services as a pair. Production constructs the
        reviewed implementations over the already configured Context and Ask
        services; importing or configuring them starts no Docker/model work.
        """
        self._before_start()
        if self.indexing is None or self.plans is None:
            raise RuntimeError('Configure Context and Ask before execution.')
        if self.execution_profiles is not None or self.sessions is not None:
            raise RuntimeError('Execution services already configured.')
        if (execution_profiles is None) != (sessions is None):
            raise ValueError('Inject execution profiles and sessions together.')
        if execution_profiles is None:
            from .execution.profiles import ExecutionProfileService
            from .sessions import SessionService
            execution_profiles = ExecutionProfileService(self.platform, self.providers)
            sessions = SessionService(self.platform, self.indexing, self.github, self.providers,
                                      self.plans, execution_profiles, data_dir=data_dir)
        self.execution_profiles, self.sessions = execution_profiles, sessions
        self.ai_providers.execution_profiles = execution_profiles
        # Plan readiness asks the session service whether a saved version could launch now.
        if getattr(self.plans, 'execution_preflight', False) is None and callable(getattr(sessions, 'preflight', None)):
            self.plans.execution_preflight = sessions.preflight
        if getattr(self.plans, 'default_execution_profile', False) is None and callable(getattr(execution_profiles, 'default_profile_id', None)):
            self.plans.default_execution_profile = execution_profiles.default_profile_id
        # Draft publication reads the trusted session snapshot; it registers its own purge hook.
        if publications is None:
            from .publication import PublicationService
            publications = PublicationService(self.platform, self.github, sessions)
        self.publications = publications
        self.handlers['publication.run'] = publications.run
        self._rebuild_api()
        return self.execution_status()

    def execution_status(self):
        if self.sessions is None:
            return {'status': 'not_available',
                    'reason': 'Implementation sessions are not configured.'}
        if getattr(self, 'recovering', False):
            return {'status': 'available', 'runtime': {'status': 'starting'}}
        runtime = self.sessions.runtime_status() if callable(getattr(self.sessions, 'runtime_status', None)) else {'status': 'stopped'}
        return {'status': 'available', 'runtime': runtime}

    @staticmethod
    def _bundled_demos():
        from pathlib import Path
        from .index import load_demos
        return load_demos(Path(__file__).resolve().parent / 'examples')

    def bootstrap_context(self, snapshots, local_roots, *, demo_loader=None):
        self._before_start()
        if self.indexing is None:
            raise RuntimeError('Configure context before bootstrap.')
        from .scheduling import IndexScheduler
        if demo_loader is not None:
            self.demo_loader = demo_loader  # Trusted application factory, never an HTTP input.
        self.scheduler = IndexScheduler(self.platform, self.indexing, self.github, self.providers)
        with self.context_lock:
            for snapshot in snapshots:
                try:
                    if snapshot.demo:
                        state = self.indexing.register_demo(snapshot)
                    else:
                        state = self.indexing.register_local(snapshot.name, local_roots[snapshot.name])
                except PlatformError as error:
                    if error.code != 'purge_pending' or not snapshot.demo:
                        raise
                    state = self.indexing.get_state('demo:' + snapshot.name)
                repository_id = state['repository_id']
                self.legacy_sources[snapshot.name] = repository_id
                # Retain identities/configuration only. Purged source is reread only
                # after an explicit Enable; no snapshot is retained as a restore cache.
                self.context_sources[repository_id] = {
                    'name': snapshot.name, 'kind': state['kind'],
                    'path': None if snapshot.demo else local_roots[snapshot.name],
                }
                if state.get('purged') or state.get('purging'):
                    if self.legacy_index is not None:
                        self.legacy_index.purge([snapshot.name])
                    continue
                self.scheduler.register(repository_id, snapshot.revision)
                if self.legacy_index is not None and state['eligible']:
                    self._restore_mirror(repository_id, snapshot)

    def _restore_mirror(self, repository_id, snapshot):
        with self.platform.store.read() as conn:
            row = conn.execute('SELECT exclusions_json FROM index_sources WHERE id=?', (repository_id,)).fetchone()
        import json
        self.legacy_index.restore(snapshot, exclusions=json.loads(row[0]))

    def select_context(self, repository_id, index_enabled, exclusions=None):
        if type(index_enabled) is not bool:
            raise ValueError('Invalid selection.')
        if self.indexing is None:
            raise RuntimeError('Context service unavailable.')
        from .indexing.service import IndexError
        from .indexing.safety import exclusions as validate_exclusions
        if exclusions is not None:
            exclusions = validate_exclusions(exclusions)
        with self.context_lock:
            state = self.indexing.get_state(repository_id)
            if state.get('purging'):
                raise IndexError('purge_pending')
            snapshot = None
            configured = self.context_sources.get(repository_id)
            missing_mirror = (configured is not None and self.legacy_index is not None
                              and configured['name'] not in self.legacy_index.snapshots)
            if index_enabled and (state.get('purged') or missing_mirror):
                if configured is None:
                    raise IndexError('source_not_configured')
                if configured['kind'] == 'demo':
                    snapshot = next((item for item in self.demo_loader()
                                     if item.demo and item.name == configured['name']), None)
                    if snapshot is None:
                        raise IndexError('source_not_configured')
                    self.indexing.register_demo(snapshot, restore=True)
                else:
                    from .index import load_git
                    from pathlib import Path
                    snapshot = load_git(configured['name'], Path(configured['path']))
            result = self.indexing.set_local_selection(repository_id, index_enabled, exclusions)
            if snapshot is not None:
                if self.legacy_index is not None:
                    self._restore_mirror(repository_id, snapshot)
                if self.scheduler is not None:
                    self.scheduler.register(repository_id, snapshot.revision)
                    with self.platform.store.transaction() as conn:
                        conn.execute('DELETE FROM index_schedule_state WHERE repository_id=?', (repository_id,))
            return result

    def legacy_source_allowed(self, name, path=None):
        if self.indexing is None:
            return True
        repository_id = self.legacy_sources.get(name)
        if repository_id is None:
            return False
        try:
            if not self.indexing.get_state(repository_id)['eligible']:
                return False
            if path is not None:
                from .indexing.safety import excluded, decode
                with self.platform.store.read() as conn:
                    row = conn.execute('SELECT exclusions_json FROM index_sources WHERE id=?',(repository_id,)).fetchone()
                import json
                if row is None or excluded(path,json.loads(row[0])):
                    return False
                snapshot = self.legacy_index.snapshots.get(name) if self.legacy_index is not None else None
                if snapshot is None or path not in snapshot.files or decode('\n'.join(snapshot.files[path]).encode()) is None:
                    return False
            return True
        except PlatformError:
            return False

    def purge_context(self, repository_id):
        with self.context_lock:
            return self._purge_context(repository_id)

    def _purge_context(self, repository_id):
        if self.indexing is None:
            raise RuntimeError('Context service unavailable.')
        state = self.indexing.get_state(repository_id)
        if state['kind'] == 'github':
            raise ValueError('Use the GitHub purge operation.')
        # Hide retrieval before removing duplicated answers and physical context.
        if not state.get('purging'):
            self.indexing.set_local_selection(repository_id, False)
        if self.scheduler is not None:
            self.scheduler.local_markers.pop(repository_id, None)
        if self.legacy_index is not None:
            self.legacy_index.purge([name for name, ident in self.legacy_sources.items() if ident == repository_id])
        with self.platform.store.transaction() as conn:
            conn.execute('DELETE FROM index_schedule_state WHERE repository_id=?',(repository_id,))
        if self.conversations is not None:
            self.conversations.purge_repository(repository_id)
        return self.indexing.purge_repository(repository_id)

    def _maintained(self, handler):
        def execute(job, context):
            finished = Event()
            def maintain():
                while not finished.wait(5):
                    try:
                        context.queue.heartbeat(context.lease, lease_ms=context.lease_ms)
                    except (LeaseLost, PlatformError):
                        return
            heartbeat = Thread(target=maintain, name='opendevengine-job-heartbeat')
            heartbeat.start()
            try:
                if self.stop_event.is_set():
                    raise JobInterrupted()
                return handler(job, ShutdownContext(context, self.stop_event))
            finally:
                finished.set()
                heartbeat.join()
        return execute

    def start(self):
        if self.thread is not None:
            raise RuntimeError('Services already started.')
        self.worker = Worker(self.platform.jobs, {kind: self._maintained(fn) for kind, fn in self.handlers.items()},
                             worker_id=new_id('worker'), lease_ms=120000)
        # Recovery can wait on Docker or GitHub (up to their control timeouts); it runs on the
        # background thread so the web app serves immediately. Jobs are claimed only afterwards.
        self.recovering = self.sessions is not None or self.publications is not None
        self.thread = Thread(target=self._run, name='opendevengine-background-jobs')
        self.thread.start()

    def _recover(self):
        try:
            if self.sessions is not None and not self.stop_event.is_set():
                try:
                    self.sessions.start()
                except Exception:
                    self.recovery_error = 'session_runtime_unavailable'
                if self.stop_event.is_set():
                    self.sessions.close()  # close() ran before the runtime started
            # Lost or expired publication runs are reconciled against GitHub before new work is claimed.
            if self.publications is not None and callable(getattr(self.publications, 'recover', None)) and not self.stop_event.is_set():
                try:
                    self.publications.recover()
                except Exception:
                    self.recovery_error = 'publication_recovery_unavailable'
        finally:
            self.error_code = self.recovery_error
            self.recovering = False

    def _run(self):
        self._recover()
        next_poll = 0
        while not self.stop_event.is_set():
            try:
                if time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + 5
                    self.github.schedule_due()
                    if self.scheduler is not None:
                        self.scheduler.run_once()
                worked = self.worker.run_once()
                self.error_code = self.recovery_error
            except Exception:
                # Network/source/credential messages must never enter diagnostics.
                self.error_code = 'background_unavailable'
                worked = False
            if not worked:
                self.stop_event.wait(0.25)

    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive() and not self.stop_event.is_set()

    def close(self):
        self.stop_event.set()
        if self.sessions is not None:
            self.sessions.close()
        if self.thread is not None:
            # Current bounded handler settles before its Store is closed. Interrupted
            # processes recover their durable lease on the next application start.
            self.thread.join()
        if self.indexing is not None:
            self.indexing.vectors.close()
