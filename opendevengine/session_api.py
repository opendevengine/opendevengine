"""HTTP adapters for versioned execution settings and durable session controls."""
import re
from urllib.parse import unquote
from .api import APIResponse, fields, page_limit, query_fields


PROFILE_FIELDS={'name','adapter','auth_mode','image','allow_external_source','runner_limits','agent_limits','agent_model'}


def bounded_int(value, minimum, maximum):
    if isinstance(value,bool) or not isinstance(value,(str,int)):
        raise ValueError('Invalid integer.')
    if isinstance(value,str) and not value.isdecimal():
        raise ValueError('Invalid integer.')
    result=int(value)
    if not minimum<=result<=maximum:
        raise ValueError('Integer outside permitted range.')
    return result


class ExecutionAPI:
    def __init__(self, profiles=None, sessions=None):
        self.profiles,self.sessions=profiles,sessions

    def dispatch(self, method, path, raw_query='', body=None):
        from .sessions.errors import SessionError
        try:
            return self._dispatch(method,path,raw_query,body)
        except SessionError as exc:
            if exc.code != 'purged':
                raise
            return APIResponse(409,{'code':'purged','error':{'code':'purged'}})

    def _dispatch(self, method, path, raw_query='', body=None):
        if method!='GET' and raw_query:
            raise ValueError('Mutation queries are not supported.')
        if self.profiles is not None:
            if path=='/api/execution-profiles':
                if method=='GET':
                    q=query_fields(raw_query,{'limit','cursor'})
                    return APIResponse(200,self.profiles.list_profiles(limit=page_limit(q.get('limit',50)),cursor=q.get('cursor')))
                if method=='POST':
                    return APIResponse(201,self.profiles.create(fields(body,PROFILE_FIELDS,{'name','adapter','auth_mode','image'})))
            match=re.fullmatch(r'/api/execution-profiles/([^/]+)(/test)?',path)
            if match:
                ident=unquote(match[1])
                if match[2] and method=='POST':
                    fields(body,set())
                    return APIResponse(202,self.profiles.request_test(ident))
                if not match[2]:
                    if method=='GET':
                        query_fields(raw_query,set())
                        return APIResponse(200,self.profiles.get(ident))
                    if method=='PATCH':
                        return APIResponse(200,self.profiles.update(ident,fields(body,PROFILE_FIELDS)))
                    if method=='DELETE':
                        fields(body,set())
                        return APIResponse(200,self.profiles.delete(ident))
            match=re.fullmatch(r'/api/execution-profile-versions/([^/]+)',path)
            if match and method=='GET':
                query_fields(raw_query,set())
                return APIResponse(200,self.profiles.get_version(unquote(match[1])))
            if path=='/api/settings/execution':
                if method=='GET':
                    query_fields(raw_query,set())
                    return APIResponse(200,self.profiles.settings())
                if method=='PATCH':
                    return APIResponse(200,self.profiles.update_settings(**fields(body,{'max_active_sessions'},{'max_active_sessions'})))
        if self.sessions is None:
            return None
        if path=='/api/sessions':
            if method=='GET':
                q=query_fields(raw_query,{'limit','cursor'})
                return APIResponse(200,self.sessions.list_sessions(limit=page_limit(q.get('limit',50)),cursor=q.get('cursor')))
            if method=='POST':
                data=fields(body,{'plan_version_id','content_hash','idempotency_key','acknowledged_base'},{'plan_version_id','content_hash','idempotency_key'})
                from .sessions.errors import SessionError
                try:
                    return APIResponse(202,self.sessions.launch(**data))
                except SessionError as exc:
                    if exc.code != 'base_advanced':
                        raise
                    return APIResponse(409, {'error': {'code': exc.code, 'details': exc.details},
                                             'code': exc.code, 'details': exc.details})
        match=re.fullmatch(r'/api/sessions/([^/]+)(/(events|diff|validations|artifacts|messages|stop|retry|purge))?',path)
        if not match:
            return None
        ident,operation=unquote(match[1]),match[3]
        if operation is None and method=='GET':
            query_fields(raw_query,set())
            return APIResponse(200,self.sessions.get(ident))
        if method=='GET' and operation=='events':
            q=query_fields(raw_query,{'after','limit'})
            after=bounded_int(q.get('after',0),0,2**53-1)
            limit=bounded_int(q.get('limit',200),1,200)
            return APIResponse(200,self.sessions.events(ident,after=after,limit=limit))
        if method=='GET' and operation in ('diff','validations','artifacts'):
            query_fields(raw_query,set())
            return APIResponse(200,getattr(self.sessions,operation)(ident))
        if method=='GET' and operation=='messages':
            q=query_fields(raw_query,{'limit','cursor'})
            return APIResponse(200,self.sessions.list_messages(ident,limit=page_limit(q.get('limit',50)),cursor=q.get('cursor')))
        if method=='POST' and operation=='messages':
            data=fields(body,{'content','client_request_id','question_id'},{'content','client_request_id'})
            return APIResponse(202,self.sessions.send_message(ident,**data))
        if method=='POST' and operation=='purge':
            fields(body,set())
            from .sessions.errors import SessionError
            try:
                return APIResponse(200,self.sessions.purge_session(ident))
            except SessionError as exc:
                if exc.code not in ('session_active','publication_in_progress'):
                    raise
                return APIResponse(409,{'code':exc.code,'error':{'code':exc.code}})
        if method=='POST' and operation=='stop':
            fields(body,set())
            return APIResponse(202,self.sessions.stop(ident))
        if method=='POST' and operation=='retry':
            data=fields(body,{'idempotency_key','acknowledged_base'},{'idempotency_key'})
            from .sessions.errors import SessionError
            try:
                return APIResponse(202,self.sessions.retry(ident,**data))
            except SessionError as exc:
                if exc.code != 'base_advanced':
                    raise
                return APIResponse(409, {'error': {'code': exc.code, 'details': exc.details},
                                         'code': exc.code, 'details': exc.details})
        return None
