"""HTTP adapter for Settings → AI providers: providers by kind and one choice per job."""
import re
from urllib.parse import unquote
from .api import APIResponse, fields, query_fields

ADD_FIELDS = {'kind', 'endpoint', 'adapter', 'api_key', 'use_login', 'consent'}


class AIProvidersAPI:
    def __init__(self, catalog=None):
        self.catalog = catalog

    def dispatch(self, method, path, raw_query='', body=None):
        if self.catalog is None or not path.startswith(('/api/ai-providers', '/api/ai-jobs')):
            return None
        if method != 'GET' and raw_query:
            raise ValueError('Mutation queries are not supported.')
        if path == '/api/ai-providers':
            if method == 'GET':
                query_fields(raw_query, set())
                return APIResponse(200, {'providers': self.catalog.list()})
            if method == 'POST':
                data = dict(fields(body, ADD_FIELDS, {'kind'}))
                return APIResponse(201, self.catalog.add(data.pop('kind'), **data))
        match = re.fullmatch(r'/api/ai-providers/([^/]+)', path)
        if match:
            ident = unquote(match[1])
            if method == 'PATCH':
                return APIResponse(200, self.catalog.update(ident, **fields(body, {'api_key', 'endpoint'})))
            if method == 'DELETE':
                fields(body, set())
                self.catalog.remove(ident)
                return APIResponse(200, {'deleted': True})
        if path == '/api/ai-jobs' and method == 'GET':
            query_fields(raw_query, set())
            return APIResponse(200, self.catalog.jobs())
        match = re.fullmatch(r'/api/ai-jobs/([a-z]{1,20})(/test)?', path)
        if match and match[2] and method == 'POST':
            fields(body, set())
            return APIResponse(200, self.catalog.test_job(match[1]))
        if match and not match[2] and method == 'PUT':
            data = fields(body, {'provider_id', 'model'}, {'provider_id'})
            return APIResponse(200, self.catalog.choose(match[1], data['provider_id'], data.get('model')))
        return None
