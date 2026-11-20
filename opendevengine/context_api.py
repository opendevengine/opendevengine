"""HTTP adapters for bounded source context; never accepts arbitrary host paths."""
import json
import re
from urllib.parse import unquote
from .api import APIResponse, fields, page_limit, query_fields


class ContextAPI:
    def __init__(self, platform, index, *, purge_context=None, select_context=None):
        self.platform, self.index, self.purge_context = platform, index, purge_context
        self.select_context = select_context

    def dispatch(self, method, path, raw_query='', body=None):
        if method != 'GET' and raw_query:
            raise ValueError('Mutation queries are not supported.')
        if method == 'GET' and path == '/api/context/repositories':
            q = query_fields(raw_query, {'limit','cursor','query'})
            limit = page_limit(q.get('limit', 50))
            query, cursor = q.get('query',''), q.get('cursor','')
            if len(query)>200 or len(cursor)>256:
                raise ValueError('Invalid catalog filters.')
            with self.platform.store.read() as conn:
                rows = conn.execute("""SELECT id FROM (
                  SELECT id,full_name AS name,description FROM repositories
                  UNION ALL SELECT id,name,'' AS description FROM index_sources WHERE kind!='github'
                    AND NOT (kind='demo' AND EXISTS(SELECT 1 FROM github_connections WHERE status='connected'))
                ) WHERE id>? AND instr(lower(name||' '||description),lower(?))>0 ORDER BY id LIMIT ?""",
                                    (cursor,query,limit+1)).fetchall()
            items = [self.index.get_state(row[0]) for row in rows[:limit]]
            return APIResponse(200, {'items':items,'next_cursor':rows[limit-1][0] if len(rows)>limit else None})
        match = re.fullmatch(r'/api/repositories/([^/]+)/(exclusions|purge)', path)
        if match:
            repository_id, operation = unquote(match[1]), match[2]
            if operation == 'exclusions':
                if method == 'GET':
                    query_fields(raw_query,set())
                    self.index.get_state(repository_id)
                    with self.platform.store.read() as conn:
                        row = conn.execute('SELECT exclusions_json FROM index_sources WHERE id=?',(repository_id,)).fetchone()
                    if row is None:
                        from .platform.errors import NotFound
                        raise NotFound('Context settings unavailable.')
                    return APIResponse(200,{'exclusions':json.loads(row[0])})
                if method == 'PATCH':
                    data = fields(body,{'exclusions'},{'exclusions'})
                    return APIResponse(200,self.index.set_exclusions(repository_id,**data))
            if operation == 'purge' and method == 'POST' and not repository_id.startswith('gh:'):
                fields(body,set())
                self.index.get_state(repository_id)
                if self.purge_context is None:
                    from .platform.errors import InvalidTransition
                    raise InvalidTransition('Local context purge is not configured.')
                return APIResponse(200,self.purge_context(repository_id))
        match = re.fullmatch(r'/api/repositories/([^/]+)/(index|context)', path)
        if match:
            repository_id, operation = unquote(match[1]), match[2]
            if method == 'GET' and operation == 'context':
                query_fields(raw_query,set())
                return APIResponse(200,self.index.get_state(repository_id))
            if method == 'POST' and operation == 'index':
                data = fields(body,{'revision','priority','embedding_version_id','limits'})
                return APIResponse(202,self.index.request_index(repository_id,**data))
            if method == 'PATCH' and operation == 'context':
                data = fields(body,{'index_enabled','exclusions'},{'index_enabled'})
                return APIResponse(200,(self.select_context or self.index.set_local_selection)(repository_id,**data))
        match = re.fullmatch(r'/api/index-jobs/([^/]+)(/(pause|resume|retry))?', path)
        if match:
            run_id = unquote(match[1])
            if method == 'GET' and not match[2]:
                query_fields(raw_query,set())
                return APIResponse(200,self.index.get_run(run_id))
            if method == 'POST' and match[3]:
                fields(body,set())
                return APIResponse(202,getattr(self.index,match[3])(run_id))
        if method == 'POST' and path == '/api/search':
            data = fields(body,{'query','repository_ids','snapshot_ids','limit','context_token_budget','semantic'},
                          {'query','repository_ids'})
            return APIResponse(200,self.index.search(**data))
        if method == 'GET' and path == '/api/context/source':
            q = query_fields(raw_query,{'repository_id','snapshot_id','path','start_line','end_line','context_lines'})
            fields(q,{'repository_id','snapshot_id','path','start_line','end_line','context_lines'},
                   {'repository_id','snapshot_id','path','start_line','end_line'})
            for key in ('start_line','end_line','context_lines'):
                if key in q:
                    q[key] = int(q[key])
            return APIResponse(200,self.index.source(**q))
        return None
