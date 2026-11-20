"""HTTP mapping for saved conversations and immutable, server-validated plans."""
import re
from urllib.parse import unquote
from .api import APIResponse,fields,page_limit,query_fields


class AskAPI:
    def __init__(self, conversations, plans, routing):
        self.conversations,self.plans,self.routing=conversations,plans,routing

    def dispatch(self,method,path,raw_query='',body=None):
        body={} if body is None else body
        if method!='GET' and raw_query:
            raise ValueError('Mutation query parameters are not supported.')
        if path=='/api/conversations':
            if method=='GET':
                q=query_fields(raw_query,{'limit','cursor'})
                return APIResponse(200,self.conversations.list_conversations(limit=page_limit(q.get('limit',50)),cursor=q.get('cursor')))
            if method=='POST':
                data=fields(body,{'title','repository_ids'})
                return APIResponse(201,self.conversations.create(**data))
        match=re.fullmatch(r'/api/conversations/([^/]+)(/(messages|runs|export))?',path)
        if match:
            conversation_id=unquote(match[1]);operation=match[3]
            if method=='GET' and operation is None:
                query_fields(raw_query,set())
                return APIResponse(200,self.conversations.get(conversation_id))
            if method=='GET' and operation=='export':
                q=query_fields(raw_query,{'limit','cursor'})
                return APIResponse(200,self.conversations.export(conversation_id,limit=page_limit(q.get('limit',100)),cursor=q.get('cursor')))
            if method=='GET' and operation=='runs':
                q=query_fields(raw_query,{'limit','cursor'})
                return APIResponse(200,self.conversations.list_runs(conversation_id,limit=page_limit(q.get('limit',50)),cursor=q.get('cursor')))
            if method=='GET' and operation=='messages':
                q=query_fields(raw_query,{'limit','cursor'})
                return APIResponse(200,self.conversations.list_messages(conversation_id,limit=page_limit(q.get('limit',50)),cursor=q.get('cursor')))
            if method=='POST' and operation=='messages':
                data=fields(body,{'content','client_request_id','intent'},{'content','client_request_id'})
                return APIResponse(202,self.conversations.send(conversation_id,**data))
            if method=='PATCH' and operation is None:
                data=fields(body,{'repository_ids','expected_revision'},{'repository_ids','expected_revision'})
                return APIResponse(200,self.conversations.update_scope(conversation_id,**data))
            if method=='DELETE' and operation is None:
                fields(body,set());self.conversations.delete(conversation_id)
                return APIResponse(200,{'deleted':True})
        match=re.fullmatch(r'/api/answer-runs/([^/]+)(/(cancel|retry))?',path)
        if match:
            run_id=unquote(match[1])
            if method=='GET' and not match[2]:
                query_fields(raw_query,set())
                return APIResponse(200,self.conversations.get_run(run_id))
            if method=='POST' and match[3]:
                fields(body,set())
                return APIResponse(202,getattr(self.conversations,match[3])(run_id))
        if path=='/api/route' and method=='POST':
            data=fields(body,{'prompt','conversation_id','pinned_repository_ids'},{'prompt'})
            return APIResponse(200,self.routing.route(**data))
        match=re.fullmatch(r'/api/route/([^/]+)',path)
        if match and method=='GET':
            query_fields(raw_query,set())
            return APIResponse(200,self.routing.get(unquote(match[1])))
        if path=='/api/plans':
            if method=='GET':
                q=query_fields(raw_query,{'limit','cursor','conversation_id'})
                return APIResponse(200,self.plans.list_plans(conversation_id=q.get('conversation_id'),limit=page_limit(q.get('limit',50)),cursor=q.get('cursor')))
            if method=='POST':
                data=fields(body,{'conversation_id','content'},{'content'})
                return APIResponse(201,self.plans.create(data.get('conversation_id'),data['content']))
        match=re.fullmatch(r'/api/plans/([^/]+)(/(versions|export))?',path)
        if match:
            plan_id=unquote(match[1]);operation=match[3]
            if method=='GET' and operation in (None,'export'):
                q=query_fields(raw_query,{'version_id'})
                function=self.plans.export if operation=='export' else self.plans.get
                return APIResponse(200,function(plan_id,version_id=q.get('version_id')))
            if method=='POST' and operation=='versions':
                data=fields(body,{'content','parent_version_id'},{'content','parent_version_id'})
                return APIResponse(201,self.plans.save_version(plan_id,**data))
            if method=='DELETE' and operation is None:
                fields(body,set());self.plans.delete(plan_id)
                return APIResponse(200,{'deleted':True})
        return None
