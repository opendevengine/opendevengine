"""Small HTTP boundary for feature services; domain behavior stays in each service."""
from dataclasses import dataclass, field
import base64
import json
import re
from urllib.parse import parse_qs, unquote

from .platform.errors import PlatformError, NotFound
from .providers.discovery import discover_ollama, probe_embedding_dimensions


@dataclass
class APIResponse:
    status: int
    body: object = None
    headers: dict = field(default_factory=dict)


def fields(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError('Invalid request fields.')
    return value


def query_fields(raw, allowed):
    values = parse_qs(raw, keep_blank_values=True, max_num_fields=10)
    if set(values) - set(allowed) or any(len(v) != 1 for v in values.values()):
        raise ValueError('Invalid query fields.')
    return {k: v[0] for k, v in values.items()}


def page_limit(value):
    if isinstance(value, bool):
        raise ValueError('Invalid page limit.')
    try:
        result = int(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError('Invalid page limit.') from None
    if not 1 <= result <= 100:
        raise ValueError('Invalid page limit.')
    return result


def error_status(code):
    if code in {'internal','provider_error'}:
        return 500
    if code in {'bad_request','validation','validation_failed','context_overflow'}:
        return 400
    if code in {'not_found'}:
        return 404
    if code in {'rate_limited', 'provider_rate_limited'}:
        return 429
    if code in {'timeout', 'upstream_timeout', 'provider_timeout'}:
        return 504
    if code in {'malformed_output', 'invalid_output', 'embedding_mismatch','provider_rejected','upstream_rejected','upstream_redirect'}:
        return 502
    if code in {'storage_busy', 'secret_store_unavailable', 'upstream_unavailable','provider_unavailable'}:
        return 503
    return 409


class FeatureAPI:
    def __init__(self, platform, github, providers, *, indexing=None, conversations=None, plans=None, routing=None, purge_context=None, select_context=None, execution_profiles=None, sessions=None, publications=None, public_base_url='http://127.0.0.1:8765'):
        self.platform, self.github, self.providers = platform, github, providers
        self.public_base_url = public_base_url
        from .session_api import ExecutionAPI
        self.execution_api = ExecutionAPI(execution_profiles,sessions)
        from .publication_api import PublicationAPI
        self.publication_api = PublicationAPI(platform, publications)
        self.ask = None
        if all(service is not None for service in (conversations, plans, routing)):
            from .ask_api import AskAPI
            self.ask = AskAPI(conversations, plans, routing)
        self.context = None
        if indexing is not None:
            from .context_api import ContextAPI
            self.context = ContextAPI(platform, indexing, purge_context=purge_context, select_context=select_context)

    def _active_version(self, profile_id):
        for profile in self.providers.list_profiles():
            if profile['id'] == profile_id:
                return profile['active_version_id']
        raise NotFound('Profile not found.')

    def dispatch(self, method, path, raw_query='', body=None):
        body = {} if body is None else body
        if method != 'GET' and raw_query:
            raise ValueError('Mutation query parameters are not supported.')
        response = self.execution_api.dispatch(method,path,raw_query,body)
        if response is not None:
            return response
        response = self.publication_api.dispatch(method, path, raw_query, body)
        if response is not None:
            return response
        if self.context is not None:
            response = self.context.dispatch(method, path, raw_query, body)
            if response is not None:
                return response
        if self.ask is not None:
            response = self.ask.dispatch(method, path, raw_query, body)
            if response is not None:
                return response
        if path == '/api/github/manifest/callback' and method == 'GET':
            q = query_fields(raw_query, {'state', 'code'})
            try:
                self.github.finish_manifest(q.get('state', ''), q.get('code', ''))
                code = 'manifest_created'
            except PlatformError as error:
                code = error.code if re.fullmatch(r'[a-z_]{1,64}', error.code) else 'setup_failed'
            return APIResponse(303, headers={'Location': '/#/settings/github?github=' + code})
        if path == '/api/github/manifest/start' and method == 'POST':
            data = fields(body, {'organization'})
            return APIResponse(200, self.github.begin_manifest(public_base_url=self.public_base_url,
                                                               organization=data.get('organization') or None))
        if path == '/api/github/callback' and method == 'GET':
            q = query_fields(raw_query, {'state', 'installation_id', 'setup_action'})
            try:
                self.github.finish_connect(q.get('state', ''), q.get('installation_id'), setup_action=q.get('setup_action'))
                code = 'pending_approval' if q.get('setup_action') == 'request' else 'connected'
            except PlatformError as error:
                code = error.code if re.fullmatch(r'[a-z_]{1,64}', error.code) else 'setup_failed'
            return APIResponse(303, headers={'Location': '/#/settings?github=' + code})
        if method == 'GET':
            if path == '/api/github/connections':
                query_fields(raw_query, set())
                return APIResponse(200, {'connections': self.github.connections(), 'summary': self.github.status_summary(),
                                         'can_configure': self.github.can_configure()})
            if path == '/api/repositories':
                q = query_fields(raw_query, {'cursor', 'limit', 'query', 'owner', 'index_enabled'})
                selected = q.get('index_enabled')
                if selected is not None and selected not in {'true', 'false'}:
                    raise ValueError('Invalid selection filter.')
                return APIResponse(200, self.github.list_repositories(
                    cursor=q.get('cursor'), limit=page_limit(q.get('limit', 50)), query=q.get('query', ''),
                    owner=q.get('owner'), index_enabled=None if selected is None else selected == 'true'))
            if path == '/api/provider-profiles/discover':
                q = query_fields(raw_query, {'base_url'})
                return APIResponse(200, discover_ollama(q.get('base_url'), transport=self.providers.transport))
            if path == '/api/provider-profiles':
                query_fields(raw_query, set())
                return APIResponse(200, {'profiles': self.providers.list_profiles()})
            if path == '/api/settings/model-roles':
                query_fields(raw_query, set())
                return APIResponse(200, {'roles': self.providers.roles(), 'settings': self.providers.settings()})
            if path == '/api/jobs':
                return APIResponse(200, self._jobs(raw_query))
            match = re.fullmatch(r'/api/jobs/([^/]+)', path)
            if match:
                query_fields(raw_query, set())
                job = self.platform.jobs.get(unquote(match[1]))
                return APIResponse(200, self._public_job(job))
            return None
        if path == '/api/github/configure' and method == 'POST':
            data = fields(body, {'app_id', 'slug', 'private_key', 'index_new'}, {'app_id', 'slug', 'private_key'})
            return APIResponse(201, self.github.configure(**data))
        github_operations = {'/api/github/connect': ('begin_connect', 200),
                             '/api/github/refresh': ('refresh', 202), '/api/github/disconnect': ('disconnect', 200)}
        if path in github_operations and method == 'POST':
            data = fields(body, {'connection_id'}, {'connection_id'})
            function, status = github_operations[path]
            return APIResponse(status, getattr(self.github, function)(data['connection_id']))
        match = re.fullmatch(r'/api/repositories/([^/]+)(/purge)?', path)
        if match:
            repository_id = unquote(match[1])
            if method == 'PATCH' and not match[2]:
                data = fields(body, {'index_enabled', 'implementation_enabled'}, {'index_enabled', 'implementation_enabled'})
                return APIResponse(200, self.github.update_selection(repository_id, **data))
            if method == 'POST' and match[2]:
                fields(body, set())
                return APIResponse(202, self.github.purge_repository(repository_id))
        if path == '/api/provider-profiles/discover/dimensions' and method == 'POST':
            data = fields(body, {'base_url', 'model'}, {'model'})
            return APIResponse(200, {'dimensions': probe_embedding_dimensions(
                data.get('base_url'), data['model'], transport=self.providers.transport)})
        if path == '/api/provider-profiles' and method == 'POST':
            data = dict(fields(body, {'name', 'adapter', 'endpoint', 'model', 'config', 'secret_value'},
                               {'name', 'adapter', 'endpoint', 'model'}))
            secret = data.pop('secret_value', None)
            return APIResponse(201, self.providers.create_profile(data, secret_value=secret))
        match = re.fullmatch(r'/api/provider-profiles/([^/]+)(/test)?', path)
        if match:
            profile_id = unquote(match[1])
            if match[2] and method == 'POST':
                data = fields(body, {'capabilities'})
                return APIResponse(200, self.providers.test_profile(self._active_version(profile_id), **data))
            if not match[2] and method == 'PATCH':
                data = dict(fields(body, {'name', 'adapter', 'endpoint', 'model', 'config', 'secret_value'}))
                secret = data.pop('secret_value', None)
                return APIResponse(200, self.providers.update_profile(profile_id, data, secret_value=secret))
            if not match[2] and method == 'DELETE':
                fields(body, set())
                self.providers.delete_profile(profile_id)
                return APIResponse(200, {'deleted': True})
        if path == '/api/settings/model-roles' and method == 'PATCH':
            data = fields(body, {'local_only', 'assignments'})
            if len(data) != 1:
                raise ValueError('Update one setting or role at a time.')
            assignments = data.get('assignments', {})
            if not isinstance(assignments, dict) or set(assignments) - {'ask', 'planning', 'summary', 'embedding'}:
                raise ValueError('Invalid role assignments.')
            if 'local_only' in data:
                self.providers.update_settings(local_only=data['local_only'])
            if assignments:
                self.providers.assign_roles(assignments)
            return APIResponse(200, {'roles': self.providers.roles(), 'settings': self.providers.settings()})
        return None

    @staticmethod
    def _public_job(job):
        keys = ('id', 'kind', 'state', 'attempts', 'max_attempts', 'run_after', 'created_at',
                'updated_at', 'finished_at', 'last_error_code')
        return {key: getattr(job, key) for key in keys}

    def _jobs(self, raw_query):
        q = query_fields(raw_query, {'limit', 'cursor', 'state'})
        limit = page_limit(q.get('limit', 50))
        clauses, args = [], []
        if 'state' in q:
            from .platform.jobs import STATES
            if q['state'] not in STATES:
                raise ValueError('Invalid job state.')
            clauses.append('state=?'); args.append(q['state'])
        count_where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        count_args = list(args)
        if q.get('cursor'):
            if len(q['cursor']) > 256:
                raise ValueError('Invalid cursor.')
            try:
                created, job_id = json.loads(base64.urlsafe_b64decode(q['cursor']).decode())
                if type(created) is not int or not isinstance(job_id, str) or len(job_id) > 64:
                    raise ValueError()
            except Exception:
                raise ValueError('Invalid cursor.') from None
            clauses.append('(created_at<? OR (created_at=? AND id<?))');args.extend([created, created, job_id])
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        public_columns = ('id','kind','state','attempts','max_attempts','run_after','created_at','updated_at','finished_at','last_error_code')
        with self.platform.store.read() as conn:
            total = conn.execute('SELECT count(*) FROM jobs' + count_where, count_args).fetchone()[0]
            rows = conn.execute('SELECT ' + ','.join(public_columns) + ' FROM jobs' + where + ' ORDER BY created_at DESC,id DESC LIMIT ?',
                                (*args, limit + 1)).fetchall()
        items = [dict(row) for row in rows[:limit]]
        cursor = None
        if len(rows) > limit:
            row = rows[limit - 1]
            cursor = base64.urlsafe_b64encode(json.dumps([row['created_at'], row['id']]).encode()).decode()
        return {'items': items, 'next_cursor': cursor, 'total': total}
