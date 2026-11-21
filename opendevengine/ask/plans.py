"""Strict normalized plans, immutable versions and recomputed readiness."""
import hashlib
import json
from pathlib import PurePosixPath
from opendevengine.platform.clock import now_ms, new_id
from opendevengine.platform.errors import InvalidTransition, NotFound
from .common import encode, text, one, page, eligible, citation, export_text


def shape(value, required, optional=()):
    if not isinstance(value,dict) or set(required)-value.keys() or value.keys()-set(required)-set(optional):
        raise ValueError('Invalid plan object.')


def strings(value):
    if not isinstance(value,list) or len(value)>100:
        raise ValueError('Invalid plan list.')
    for item in value:
        text(item,4000)


def approved(item):
    return item.get('approved') is True

def safe_path(value):
    text(value,1000)
    if value.startswith('/'):
        raise ValueError('Unsafe relative path.')
    value=value.rstrip('/')  # a directory may be named with a trailing slash
    if any(ord(c)<32 for c in value) or '\\' in value or any(p in ('','.','..') for p in value.split('/')) or ':' in value:
        raise ValueError('Unsafe relative path.')
    return str(PurePosixPath(value))


class PlanService:
    def __init__(self,store,index,providers,*,execution_preflight=None,clock=now_ms):
        self.store,self.index,self.providers=store,index,providers
        self.execution_preflight,self.clock=execution_preflight,clock
        self.default_execution_profile=None

    def validate(self,content,conversation_id=None):
        shape(content,('goal','user_request'),('repositories','citations','findings','requirements','steps','validation','risks','assumptions','open_questions','execution_context','publication'))
        if len(encode(content).encode())>100000:
            raise ValueError('Plan too large.')
        content=json.loads(encode(content))
        for key in ('goal','user_request'):
            text(content[key])
        for key in ('repositories','citations','findings','requirements','steps','validation','risks','assumptions','open_questions'):
            content.setdefault(key,[])
            if not isinstance(content[key],list) or len(content[key])>100:
                raise ValueError('Invalid plan list.')
        scope=None
        if conversation_id:
            with self.store.read() as db:
                conv=one(db,'conversations',conversation_id)
                if conv['deleted_at'] is not None:
                    raise NotFound('Conversation deleted.')
                scope=json.loads(conv['scope_json'])
        repos={}
        for repo in content['repositories']:
            shape(repo,('repository_id','role','snapshot_id','revision','branch'))
            for key in ('repository_id','snapshot_id','revision'):
                text(repo[key],200)
            if repo['branch'] is not None:
                text(repo['branch'],200)
            if repo['role'] not in ('read','write') or repo['repository_id'] in repos or scope is not None and repo['repository_id'] not in scope:
                raise ValueError('Invalid repository scope.')
            state=eligible(self.index,repo['repository_id'])
            if not state:
                raise ValueError('Repository unavailable.')
            # Public search validates retained snapshot ownership, including empty snapshots.
            result=self.index.search('snapshot', [repo['repository_id']],{repo['repository_id']:repo['snapshot_id']},limit=1,semantic=False)
            snap=result['snapshots'].get(repo['repository_id'])
            if not snap or snap['revision']!=repo['revision']:
                raise ValueError('Snapshot mismatch.')
            repos[repo['repository_id']]=repo
        citations={}
        for c in content['citations']:
            shape(c,('id','repository_id','snapshot_id','revision','path','start_line','end_line'))
            text(c['id'],100)
            if c['id'] in citations:
                raise ValueError('Duplicate citation.')
            repo=repos.get(c['repository_id'])
            if not repo or any(c[k]!=repo[k] for k in ('snapshot_id','revision')):
                raise ValueError('Citation outside scope.')
            citation(self.index,c)
            citations[c['id']]=c
        for key in ('requirements','risks','assumptions'):
            strings(content[key])
        for key,required in (('findings',('text','citation_ids')),('steps',('title','description','paths','citation_ids')),('validation',('command','expected','source','citation_ids'))):
            for item in content[key]:
                shape(item,required,('approved',) if key=='validation' else ())
                strings(item['citation_ids'])
                if any(c not in citations for c in item['citation_ids']):
                    raise ValueError('Unknown evidence ID.')
                for k in set(required)-{'citation_ids','paths'}:
                    text(item[k],4000)
                if key=='findings' and not item['citation_ids'] and not any(w in item['text'].lower() for w in ('unknown','not verified','no evidence','gap:')):
                    raise ValueError('Finding requires evidence or explicit gap.')
                if key=='steps':
                    strings(item['paths'])
                    item['paths']=[safe_path(path) for path in item['paths']]
                if key=='validation' and (item['source'] not in ('observed','proposed') or item['source']=='observed' and not item['citation_ids']):
                    raise ValueError('Invalid validation evidence.')
                if key=='validation' and type(item.get('approved',False)) is not bool:
                    raise ValueError('Invalid validation approval.')
        question_ids=set()
        for q in content['open_questions']:
            shape(q,('id','text','blocking','answer'))
            text(q['id'],100); text(q['text'],4000)
            if q['id'] in question_ids or type(q['blocking']) is not bool:
                raise ValueError('Invalid question.')
            question_ids.add(q['id'])
            if q['answer'] is not None:
                text(q['answer'],4000)
        ctx=content.setdefault('execution_context',{})
        shape(ctx,(),('execution_profile_id','skills','plugins','memory','base_choices'))
        ctx.setdefault('execution_profile_id',None)
        if ctx['execution_profile_id'] is not None:
            text(ctx['execution_profile_id'],200)
        for key in ('skills','plugins','memory'):
            ctx.setdefault(key,[])
            if ctx[key]!=[]:
                raise ValueError('Customization unsupported in M1.')
        ctx.setdefault('base_choices',{})
        if not isinstance(ctx['base_choices'],dict) or any(k not in repos or v not in ('retain','regenerate') for k,v in ctx['base_choices'].items()):
            raise ValueError('Invalid base choice.')
        publication=content.setdefault('publication',{'policy':'manual_draft'})
        shape(publication,('policy',))
        if publication['policy'] not in ('manual_draft','on_success_draft'):
            raise ValueError('Invalid publication policy.')
        return content

    def _insert(self,db,plan_id,content,parent,source,profile,generation,conversation_id):
        if source not in ('manual','model','amendment') or source=='model' and not profile:
            raise ValueError('Invalid version provenance.')
        scope_revision=None
        if conversation_id:
            conv=one(db,'conversations',conversation_id)
            if conv['deleted_at'] is not None or set(r['repository_id'] for r in content['repositories'])-set(json.loads(conv['scope_json'])):
                raise InvalidTransition('Scope changed.')
            scope_revision=conv['scope_revision']
        for repo in content['repositories']:
            if not eligible(self.index,repo['repository_id']):
                raise InvalidTransition('Repository unavailable.')
        for c in content['citations']:
            citation(self.index,c)
        if source!='manual':
            # Only the user's own edits can approve a validation command to run as shell.
            content={**content,'validation':[{**item,'approved':False} for item in content['validation']]}
        number=1 if parent is None else one(db,'plan_versions',parent)['version']+1
        ident=new_id('pv'); now=self.clock(); canonical=encode(content)
        db.execute('INSERT INTO plan_versions (id,plan_id,version,parent_version_id,scope_revision,content_json,content_hash,source,profile_version_id,generation_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',(ident,plan_id,number,parent,scope_revision,canonical,hashlib.sha256(canonical.encode()).hexdigest(),source,profile,encode(generation or {}),now))
        db.execute('UPDATE plans SET latest_version_id=?,title=?,updated_at=? WHERE id=?',(ident,content['goal'][:200],now,plan_id))
        return ident

    def create(self,conversation_id,content,source='manual',profile_version_id=None,generation=None):
        content=self.validate(content,conversation_id)
        ident=new_id('plan'); now=self.clock()
        with self.store.transaction() as db:
            db.execute('INSERT INTO plans (id,conversation_id,title,latest_version_id,created_at,updated_at) VALUES (?,?,?,?,?,?)',(ident,conversation_id,content['goal'][:200],'pending',now,now))
            self._insert(db,ident,content,None,source,profile_version_id,generation,conversation_id)
        return self.get(ident)

    def save_version(self,plan_id,content,parent_version_id,source='manual',profile_version_id=None,generation=None):
        with self.store.read() as db:
            plan=one(db,'plans',plan_id)
        content=self.validate(content,plan['conversation_id'])
        with self.store.transaction() as db:
            plan=one(db,'plans',plan_id)
            if plan['deleted_at'] is not None or plan['latest_version_id']!=parent_version_id:
                raise InvalidTransition('Plan version changed.')
            self._insert(db,plan_id,content,parent_version_id,source,profile_version_id,generation,plan['conversation_id'])
        return self.get(plan_id)

    def _readiness(self,plan,version):
        c=version['content']; reasons=[]; warnings=[]; freshness={}; execution=None
        if c is None:
            return dict(state='draft',blocking_reasons=['deleted'],warnings=[],source_freshness={})
        if plan['conversation_id']:
            with self.store.read() as db:
                conv=one(db,'conversations',plan['conversation_id'])
            if conv['deleted_at'] is not None or conv['scope_revision']!=version['scope_revision']:
                reasons.append('scope_changed')
        writes=[r for r in c['repositories'] if r['role']=='write']
        if len(writes)!=1:
            reasons.append('exactly_one_write_target_required')
        for repo in c['repositories']:
            state=eligible(self.index,repo['repository_id'])
            if not state:
                reasons.append('repository_unavailable'); continue
            if repo['role']=='write' and not state['implementation_enabled']:
                reasons.append('implementation_not_permitted')
            snap=state['active_snapshot']
            changed=not snap or snap['id']!=repo['snapshot_id'] or state['status']=='stale'
            freshness[repo['repository_id']]='retained' if changed else 'current_index'
            if changed and c['execution_context']['base_choices'].get(repo['repository_id'])!='retain':
                reasons.append('base_choice_required')
            try:
                result=self.index.search('snapshot',[repo['repository_id']],{repo['repository_id']:repo['snapshot_id']},limit=1,semantic=False)
                if result['snapshots'][repo['repository_id']]['revision']!=repo['revision']:
                    raise ValueError()
            except Exception:
                reasons.append('snapshot_unavailable')
            if state['status'] in ('partial','stale','metadata_only','failed','indexing'):
                warnings.append('source_'+state['status'])
        for ctn in c['citations']:
            try:
                citation(self.index,ctn)
            except Exception:
                reasons.append('citation_unavailable')
        if not all(approved(item) for item in c['validation']):
            warnings.append('validation_unapproved')
        if any(q['blocking'] and not q['answer'] for q in c['open_questions']):
            reasons.append('blocking_questions')
        if not c['execution_context']['execution_profile_id'] or self.execution_preflight is None:
            reasons.append('execution_not_configured')
        elif self.execution_preflight:
            try:
                check=self.execution_preflight(c)
                reasons.extend(check['blocking_reasons']); warnings.extend(check['warnings'])
                execution=check.get('execution') if isinstance(check.get('execution'),dict) else None
            except Exception:
                reasons.append('execution_preflight_failed')
        return dict(state='superseded' if version['id']!=plan['latest_version_id'] else 'needs_input' if reasons else 'ready',blocking_reasons=sorted(set(reasons)),warnings=sorted(set(warnings)),source_freshness=freshness,execution=execution)

    def get(self,plan_id,version_id=None):
        with self.store.read() as db:
            plan=one(db,'plans',plan_id)
            version=one(db,'plan_versions',version_id or plan['latest_version_id'])
        if version['plan_id']!=plan_id:
            raise NotFound('Version unavailable.')
        raw=version.pop('content_json')
        version['content']=json.loads(raw) if raw else None
        version['generation']=json.loads(version.pop('generation_json'))
        version['readiness']=self._readiness(plan,version)
        # Fail closed for duplicated source when any citation/scope has become inaccessible.
        if any(x in version['readiness']['blocking_reasons'] for x in ('repository_unavailable','citation_unavailable','snapshot_unavailable','deleted')):
            version['content']=None
            plan['title']='[Unavailable plan]'
        return dict(**plan,version=version)

    def list_plans(self,conversation_id=None,limit=50,cursor=None):
        with self.store.read() as db:
            rows,nxt=page(db,'plans',limit,cursor,'deleted_at IS NULL'+(' AND conversation_id=?' if conversation_id else ''),(conversation_id,) if conversation_id else ())
        return dict(items=[self.get(r['id']) for r in rows],next_cursor=nxt)

    def delete(self,plan_id):
        with self.store.transaction() as db:
            one(db,'plans',plan_id)
            self._redact(db,plan_id)
        return {'id':plan_id,'deleted':True}

    def _redact(self,db,plan_id):
        now=self.clock()
        db.execute('UPDATE plans SET title=?,deleted_at=coalesce(deleted_at,?) WHERE id=?',('[deleted]',now,plan_id))
        db.execute("UPDATE plan_versions SET content_json=NULL,generation_json='{}',deleted_at=coalesce(deleted_at,?) WHERE plan_id=?",(now,plan_id))

    def export(self,plan_id,version_id=None):
        version=self.get(plan_id,version_id)['version']; c=version['content']
        markdown='[Deleted or unavailable plan]'
        if c is not None:
            from urllib.parse import urlencode
            markdown='# '+export_text(c['goal'])+'\n\nVersion: '+version['id']+'\nHash: '+version['content_hash']+'\n\n'
            markdown+='## User request\n\n'+export_text(c['user_request'])+'\n\n## Repository scope\n\n'
            for repo in c['repositories']:
                markdown+='- '+export_text(repo['repository_id'])+' ('+repo['role']+'), snapshot '+export_text(repo['snapshot_id'])+', revision '+export_text(repo['revision'])+', branch '+export_text(repo['branch'])+'\n'
            markdown+='\n## Evidence\n\n'
            for cit in c['citations']:
                query=urlencode({k:cit[k] for k in ('repository_id','snapshot_id','path','start_line','end_line')})
                markdown+='- ['+export_text(cit['id'])+'](/api/context/source?'+query+') '+export_text(cit['path'])+':'+str(cit['start_line'])+'–'+str(cit['end_line'])+' @ '+export_text(cit['revision'])+'\n'
            for key in ('findings','requirements','steps','validation','risks','assumptions','open_questions'):
                markdown+='\n## '+key.replace('_',' ').title()+'\n\n'
                for i,item in enumerate(c[key],1):
                    if isinstance(item,str):
                        rendered=export_text(item)
                    else:
                        rendered='; '.join(export_text(k)+': '+export_text(encode(v) if isinstance(v,(list,dict)) else v) for k,v in item.items())
                    markdown+=str(i)+'. '+rendered+'\n'
            markdown+='\n## Execution context\n\n'+export_text(encode(c['execution_context']))+'\n\n## Publication\n\n'+export_text(c['publication']['policy'])+'\n'
        return dict(plan_version_id=version['id'],content_hash=version['content_hash'],markdown=markdown)
