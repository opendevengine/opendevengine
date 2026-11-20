"""HTTP adapter for draft publication; behavior and trust checks stay in PublicationService."""
import re
from urllib.parse import unquote
from .api import APIResponse, fields, query_fields
from .platform.errors import NotFound


PUBLISH_FIELDS = {'title', 'body', 'expected_diff_hash', 'intent_version', 'idempotency_key'}


class PublicationAPI:
    def __init__(self, platform, publications=None):
        self.platform, self.publications = platform, publications

    def _for_session(self, session_id):
        # A session has at most one publication intent; the browser finds it again after reload.
        with self.platform.store.read() as db:
            row = db.execute('SELECT id FROM publications WHERE session_id=?', (session_id,)).fetchone()
        if row is None:
            raise NotFound('Publication not found.')
        return self.publications.get(row['id'])

    def dispatch(self, method, path, raw_query='', body=None):
        if self.publications is None:
            return None
        if path == '/api/settings/publication':
            if method == 'GET':
                query_fields(raw_query, set())
                return APIResponse(200, self.publications.settings())
            if method == 'PATCH':
                data = fields(body, {'commit_name', 'commit_email'}, {'commit_name', 'commit_email'})
                return APIResponse(200, self.publications.update_settings(**data))
            return None
        match = re.fullmatch(r'/api/sessions/([^/]+)/(publication-preview|pull-requests|publication)', path)
        if match:
            session_id, operation = unquote(match[1]), match[2]
            if operation == 'publication-preview' and method == 'POST':
                fields(body, set())
                return APIResponse(200, self.publications.preview(session_id))
            if operation == 'pull-requests' and method == 'POST':
                return APIResponse(202, self.publications.publish(session_id, **fields(body, PUBLISH_FIELDS, PUBLISH_FIELDS)))
            if operation == 'publication' and method == 'GET':
                query_fields(raw_query, set())
                return APIResponse(200, self._for_session(session_id))
            return None
        match = re.fullmatch(r'/api/publications/([^/]+)(/(retry|refresh))?', path)
        if not match:
            return None
        ident, operation = unquote(match[1]), match[3]
        if operation is None and method == 'GET':
            query_fields(raw_query, set())
            return APIResponse(200, self.publications.get(ident))
        if operation == 'retry' and method == 'POST':
            return APIResponse(202, self.publications.retry(ident, **fields(body, {'idempotency_key'}, {'idempotency_key'})))
        if operation == 'refresh' and method == 'POST':
            fields(body, set())
            return APIResponse(200, self.publications.refresh(ident))
        return None
