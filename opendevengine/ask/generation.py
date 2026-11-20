"""Compact native model drafts bind to server-owned snapshot identities."""
from opendevengine.platform.clock import new_id


def _object(properties):
    return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}


def _array(items, *, empty=False):
    return {'type':'array','items':items,**({'maxItems':0} if empty else {})}


def plan_schema(repository_ids, evidence_ids):
    string={'type':'string'}
    refs=_array({'type':'string',**({'enum':list(evidence_ids)} if evidence_ids else {})},empty=not evidence_ids)
    strings=_array(string)
    repositories=_array(_object({'repository_id':{'type':'string',**({'enum':list(repository_ids)} if repository_ids else {})},'role':{'type':'string','enum':['write','read']}}),empty=not repository_ids)
    repositories.update(maxItems=len(repository_ids))
    refs.update(maxItems=len(evidence_ids))
    return _object({
        'goal':string,'repositories':repositories,'citation_ids':refs,
        'findings':_array(_object({'text':string,'citation_ids':refs})),
        'requirements':strings,
        'steps':_array(_object({'title':string,'description':string,'paths':strings,'citation_ids':refs})),
        'validation':_array(_object({'command':string,'expected':string,'citation_ids':refs})),
        'risks':strings,'assumptions':strings,
        'open_questions':_array(_object({'text':string,'blocking':{'type':'boolean'}})),
    })


def bind_plan(draft, prompt, bindings, evidence):
    """A model chooses roles and evidence IDs; it never supplies commit identities."""
    if not isinstance(draft,dict) or set(draft)!=set(plan_schema(bindings,evidence)['properties']):
        raise ValueError('Invalid plan draft fields.')
    repositories=[];seen=set()
    for item in draft['repositories']:
        if not isinstance(item,dict) or set(item)!={'repository_id','role'}:
            raise ValueError('Invalid repository role.')
        ident=item['repository_id']
        if ident not in bindings or ident in seen or item['role'] not in ('write','read'):
            raise ValueError('Invalid repository role.')
        seen.add(ident)
        repositories.append({**bindings[ident],'repository_id':ident,'role':item['role']})
    references=draft['citation_ids']
    if not isinstance(references,list) or any(not isinstance(key,str) or key not in evidence for key in references):
        raise ValueError('Invalid evidence references.')
    for collection in ('findings','steps','validation'):
        for item in draft[collection]:
            for key in item['citation_ids']:
                if not isinstance(key,str) or key not in evidence:
                    raise ValueError('Invalid evidence references.')
                if key not in references:
                    references=references+[key]
    citations=[]
    for key in dict.fromkeys(references):
        hit=evidence[key]
        if hit['repository_id'] not in seen:
            raise ValueError('Evidence outside selected repositories.')
        citations.append({'id':key,**{k:hit[k] for k in ('repository_id','snapshot_id','revision','path','start_line','end_line')}})
    questions=[]
    for question in draft['open_questions']:
        if not isinstance(question,dict) or set(question)!={'text','blocking'}:
            raise ValueError('Invalid open question.')
        questions.append({'id':new_id('q'),'text':question['text'],'blocking':question['blocking'],'answer':None})
    return {
        'goal':draft['goal'],'user_request':prompt,'repositories':repositories,'citations':citations,
        'findings':draft['findings'],'requirements':draft['requirements'],'steps':draft['steps'],
        'validation':[{**item,'source':'proposed'} for item in draft['validation']],
        'risks':draft['risks'],'assumptions':draft['assumptions'],'open_questions':questions,
        'execution_context':{'execution_profile_id':None,'skills':[],'plugins':[],'memory':[],'base_choices':{}},
        'publication':{'policy':'manual_draft'},
    }
