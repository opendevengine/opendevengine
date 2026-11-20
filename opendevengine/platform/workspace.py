"""Read-only status builders. No model probes, workers or recovery side effects."""
from dataclasses import asdict
from .clock import format_ts
from .jobs import STATES
from .migrations import LATEST_VERSION
from .secrets import UnavailableSecretStore


def build_workspace_summary(index, model, platform, *, now):
    snapshots = list(index.snapshots.values())
    demo = all(snapshot.demo for snapshot in snapshots)
    status = platform.secrets.status() if platform else UnavailableSecretStore().status()
    return {
        'contract': 'opendevengine.workspace/m0.1', 'generated_at': format_ts(now), 'product': 'OpenDevEngine',
        'mode': 'demo' if demo else 'local',
        'catalog': {'source': 'demo_fixtures' if demo else 'local_checkouts', 'repository_count': len(snapshots),
                    'limited_count': sum(bool(s.limited) for s in snapshots), 'repos_endpoint': '/api/repos',
                    'github': {'status': 'not_available', 'reason': 'GitHub connection is not available in this version. Only bundled samples or explicit local checkouts are indexed.'}},
        'providers': {'answer': {'status': 'configured' if model else 'not_configured', 'adapter': 'ollama' if model else None,
                                 'model': model.model if model else None, 'destination': getattr(model, 'destination', 'loopback') if model else None},
                      'profiles': {'status': 'not_available', 'reason': 'Provider profiles are not available in this version. Start with --model NAME to use a local Ollama model.'}},
        'execution': {'status': 'not_available', 'reason': 'Implementation sessions are not available in this version. OpenDevEngine is read-only.'},
        'storage': {'persistent': platform.store.persistent if platform else False, 'schema_version': platform.store.schema_version() if platform else None, 'status': 'ok' if platform else 'disabled'},
        'secrets': asdict(status),
        'navigation': [dict(id=name, label=label, href='#/' + name, status=state) for name, label, state in
                       [('ask', 'Ask', 'available'), ('repositories', 'Repositories', 'read_only'), ('settings', 'Settings', 'read_only')]]}


def apply_github_status(result, github):
    """Attach the GitHub catalog summary; a connected catalog is the workspace source."""
    result['catalog']['github'] = github
    if github.get('status') == 'connected':
        result['mode'] = 'connected'
        result['catalog']['source'] = 'github'
    return result


def build_health(index, model, platform, *, now):
    counts, expired, oldest, failures = dict.fromkeys(STATES, 0), 0, None, []
    if platform:
        with platform.store.read() as conn:
            counts.update(dict(conn.execute('SELECT state,count(*) FROM jobs GROUP BY state')))
            expired = conn.execute("SELECT count(*) FROM jobs WHERE state='leased' AND lease_expires_at<?", (now,)).fetchone()[0]
            first = conn.execute("SELECT min(created_at) FROM jobs WHERE state='queued'").fetchone()[0]
            oldest = max(0, now - first) if first is not None else None
            failures = platform.jobs.recent_failures()
    degraded = bool(expired or counts['needs_reconciliation'])
    secret_status = platform.secrets.status() if platform else UnavailableSecretStore().status()
    return {'contract': 'opendevengine.health/m0.1', 'status': 'degraded' if degraded else 'ok', 'checked_at': format_ts(now),
            'components': {
                'api': {'status': 'ok'},
                'index': {'status': 'ok', 'repository_count': len(index.snapshots), 'persistent': False},
                'storage': {'status': 'ok' if platform else 'disabled', 'persistent': platform.store.persistent if platform else False,
                            'schema_version': platform.store.schema_version() if platform else None,
                            'expected_schema_version': LATEST_VERSION if platform else None, 'size_bytes': platform.store.size_bytes() if platform else None},
                'jobs': {'status': ('degraded' if degraded else 'ok') if platform else 'disabled', 'counts': counts,
                         'expired_leases': expired, 'oldest_queued_age_ms': oldest, 'workers_running': 0},
                'secrets': {'status': 'available' if secret_status.available else 'unavailable', 'backend': secret_status.backend,
                            'code': None if secret_status.available else 'secret_store_unavailable'},
                'model': {'status': 'configured' if model else 'not_configured', 'adapter': 'ollama' if model else None,
                          'destination': getattr(model, 'destination', 'loopback') if model else None, 'probed': False}}, 'recent_failures': failures}


def apply_service_health(result, services):
    """Add content-free M1/M2 projections to the compatible m0.1 envelope.

    Index counts describe the latest run (active before failed before ready), not
    retained snapshots. Docker/agent readiness is never inferred from a worker.
    No provider destinations, recovery records or exception text are copied.
    """
    components = result['components']
    indexing = services.indexing
    if indexing is not None:
        counts = dict.fromkeys(('ready', 'indexing', 'failed', 'not_indexed'), 0)
        with indexing.store.read() as db:
            rows = db.execute("""SELECT s.active_snapshot_id, r.state, s.index_enabled
                FROM index_sources s LEFT JOIN index_runs r ON r.id=(
                  SELECT id FROM index_runs WHERE repository_id=s.id
                  ORDER BY created_at DESC,rowid DESC LIMIT 1)""").fetchall()
            for snapshot, state, enabled in rows:
                key = ('not_indexed' if not enabled else 'indexing' if state in ('queued', 'running', 'waiting', 'paused') else
                       'failed' if state == 'failed' else 'ready' if snapshot else 'not_indexed')
                counts[key] += 1
            # Discovery metadata need not have an index_sources row yet.
            counts['not_indexed'] += db.execute('''SELECT count(*) FROM repositories r
                WHERE NOT EXISTS(SELECT 1 FROM index_sources s WHERE s.github_repository_id=r.id)''').fetchone()[0]
            failure = db.execute("""SELECT error_code FROM index_runs WHERE state='failed'
                ORDER BY updated_at DESC,rowid DESC LIMIT 1""").fetchone()
        components['index'] = {'status': 'degraded' if counts['failed'] else 'ok',
                               'persistent': True, 'repository_count': sum(counts.values()),
                               'counts': counts, 'latest_failure_code': _health_code(failure[0]) if failure else None}
    roles = services.providers.roles()
    projected = {}
    for role in ('ask', 'planning'):
        entry = roles.get(role, {})
        version = services.providers.get_version(entry['version_id']) if entry.get('version_id') else {}
        projected[role] = {'status': 'not_configured' if entry.get('status') in (None, 'unassigned') else _health_code(entry['status']),
                           'adapter': version.get('adapter'),
                           'destination': (entry.get('destination') or {}).get('kind')}
    components['model'] = {**projected['ask'],
                           'status': 'not_configured' if projected['ask']['status'] == 'not_configured' else 'configured',
                           'roles': projected, 'probed': False}
    execution = services.execution_status()
    runtime = execution.get('runtime') or {}
    active = 0
    if services.sessions is not None:
        from ..sessions.service import ACTIVE
        with services.sessions.store.read() as db:
            active = db.execute('SELECT count(*) FROM sessions WHERE state IN (%s)' %
                                ','.join('?' for _ in ACTIVE), ACTIVE).fetchone()[0]
    components['execution'] = {'status': _health_code(execution.get('status')),
                               'runtime_status': _health_code(runtime.get('status')),
                               'docker_code': _health_code(runtime.get('docker_code'), 'not_checked'),
                               'agent_code': _health_code(runtime.get('agent_code'), 'not_checked'),
                               'active_session_count': active}
    if components['index']['status'] == 'degraded':
        result['status'] = 'degraded'
    return result


def _health_code(value, fallback='unknown'):
    """Codes only: reject free-form diagnostics even if an upstream caller errs."""
    import re
    return value if isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', value) else fallback
