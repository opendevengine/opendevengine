"""Evidence based catalog routing; model output never determines eligibility."""
import hashlib
import json
import re
from opendevengine.platform.clock import now_ms, new_id
from .common import encode, text, ids, one, eligible, citation


class RoutingService:
    def __init__(self,store,index,*,clock=now_ms):
        self.store,self.index,self.clock=store,index,clock

    def route(self,prompt,conversation_id=None,pinned_repository_ids=()):
        text(prompt,4000)
        pins=ids(pinned_repository_ids)
        if conversation_id:
            with self.store.read() as db:
                conv=one(db,'conversations',conversation_id)
                if conv['deleted_at'] is not None:
                    raise ValueError('Conversation deleted.')
                if not pins:
                    pins=json.loads(conv['scope_json'])
        # Morphological variants improve ordinary plural/tense paraphrases without a model.
        slash_pattern=r'(?<![\w/-])([\w.-]+(?:/[\w.-]+)+)'
        slash_tokens=set(re.findall(slash_pattern,prompt.lower()))
        catalog_prompt=re.sub(slash_pattern,' ',prompt)
        tokens=re.findall(r'\w+',catalog_prompt.lower())
        variants=[]
        for token in tokens[:64]:
            if len(token)>4 and token.endswith('s'):
                variants.append(token[:-1])
            if len(token)>5 and token.endswith('ed'):
                variants.append(token[:-2])
        query=(catalog_prompt+' '+' '.join(variants))[:4000]
        raw=self.index.catalog_candidates(query,pinned_repository_ids=pins,limit=max(30,len(pins)))
        generic=set('a an the to for of in on at by and or with without our their from when after before should never add update fix change improve generate design build show keep allow require explain trace track investigate reject prevent preserve during through into that this is are be as it first small'.split())
        path_prefixes={'src','lib','app','apps','test','tests','docs','config','scripts','read','client','i'}
        file_suffixes=('.py','.js','.ts','.tsx','.jsx','.md','.json','.yaml','.yml','.css','.html')
        def owner_qualified(token):
            parts=token.split('/')
            return len(parts)==2 and parts[0] not in path_prefixes and not parts[1].endswith(file_suffixes)
        for mention in sorted(slash_tokens)[:10]:
            for item in self.index.catalog_candidates(mention,limit=10):
                if (item['name'].lower()==mention or owner_qualified(mention) and item['name'].lower().split('/')[-1]==mention.split('/')[-1]) and not any(c['repository_id']==item['repository_id'] for c in raw):
                    raw.append(item)
        # A slash token is explicit only when it names a current catalog entry.
        qualified={item['name'].lower() for item in raw if item['name'].lower() in slash_tokens}
        without_paths=re.sub(slash_pattern,' ',prompt.lower())
        words=set(re.findall(r'[\w.-]+',without_paths))
        candidates=[]
        degradation=None
        for item in raw:
            state=eligible(self.index,item['repository_id'])
            if not state:
                continue
            name=item['name'].lower()
            exact=name in qualified or name.split('/')[-1] in words
            meaningful=[r for r in item['reasons'] if set(r['terms'])-generic]
            if not meaningful and not exact and item['repository_id'] not in pins:
                continue
            score=sum(len(set(r['terms'])-generic) for r in meaningful)+(10000 if item['repository_id'] in pins else 1000 if name in qualified else 100 if exact else 0)
            candidates.append(dict(repository_id=item['repository_id'],name=item['name'],rank=score,reason='Pinned repository' if item['repository_id'] in pins else 'Explicit repository name' if exact else 'Catalog evidence: '+'; '.join(r['field']+': '+', '.join(t for t in r['terms'] if t not in generic) for r in meaningful),citations=[],coverage=(state['active_snapshot'] or {}).get('coverage',{}),freshness=state['status'],implementation_enabled=state['implementation_enabled']))
        candidates.sort(key=lambda c:(-c['rank'],c['repository_id']))
        candidates=candidates[:max(8,len(pins))]
        for rank,c in enumerate(candidates,1):
            c['rank']=rank
        if candidates:
            result=self.index.search(prompt,[c['repository_id'] for c in candidates[:5]],limit=10,context_token_budget=3000)
            degradation=result['degradation_code']
            for hit in result['hits']:
                for c in candidates:
                    if c['repository_id']==hit['repository_id']:
                        c['citations'].append({k:hit[k] for k in ('repository_id','snapshot_id','revision','path','start_line','end_line')})
        candidates=[c for c in candidates if eligible(self.index,c['repository_id'])]
        missing=set(pins)-{c['repository_id'] for c in candidates}
        explicit=[c['repository_id'] for c in candidates if c['name'].lower() in qualified]
        short=[c for c in candidates if c['name'].split('/')[-1].lower() in words]
        selected=pins if pins and not missing else explicit
        if not selected and len(short)==1:
            selected=[short[0]['repository_id']]
        status='resolved' if selected else 'needs_selection' if candidates or missing else 'no_match'
        unavailable={token for token in slash_tokens if owner_qualified(token) and token not in qualified and any(c['name'].lower().split('/')[-1]==token.split('/')[-1] for c in candidates)}
        if unavailable and not pins:
            selected=[]
            status='needs_selection'
            degradation='explicit_repository_unavailable'
            for c in candidates:
                if any(c['name'].lower().split('/')[-1]==token.split('/')[-1] for token in unavailable):
                    c['reason']='Same repository short name; requested owner/repository is unavailable'
        if missing:
            selected=[]
            status='needs_selection'
            degradation='pinned_unavailable'
        ident=new_id('route')
        generation=hashlib.sha256(encode([(c['repository_id'],c['freshness']) for c in candidates]).encode()).hexdigest()
        with self.store.transaction() as db:
            db.execute('INSERT INTO routing_decisions (id,conversation_id,prompt,pinned_json,selected_json,candidates_json,catalog_generation,status,degradation_code,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',(ident,conversation_id,prompt,encode(pins),encode(selected),encode(candidates),generation,status,degradation,self.clock()))
        return self.get(ident)

    def get(self,decision_id):
        with self.store.read() as db:
            row=one(db,'routing_decisions',decision_id)
        for key in ('pinned','selected','candidates'):
            row[{'pinned':'pinned_repository_ids','selected':'selected_repository_ids','candidates':'candidates'}[key]]=json.loads(row.pop(key+'_json'))
        row['candidates']=[c for c in row['candidates'] if eligible(self.index,c['repository_id'])]
        for c in row['candidates']:
            state=eligible(self.index,c['repository_id'])
            if state:
                c.update(coverage=(state['active_snapshot'] or {}).get('coverage',{}),freshness=state['status'],implementation_enabled=state['implementation_enabled'])
            try:
                for cit in c['citations']:
                    citation(self.index,cit)
            except Exception:
                c.update(citations=[],reason='Catalog match; source evidence unavailable')
        allowed={c['repository_id'] for c in row['candidates']}
        if set(row['selected_repository_ids'])-allowed or set(row['pinned_repository_ids'])-allowed:
            row.update(status='needs_selection',degradation_code='scope_unavailable',selected_repository_ids=[],prompt='[Scope unavailable]')
        row['question']='Which repositories should provide context?' if row['status']=='needs_selection' else None
        if row['degradation_code']=='explicit_repository_unavailable':
            row['question']='The requested owner/repository is unavailable. Which accessible repository should provide context?'
        row['next_action']='ask' if row['selected_repository_ids'] else 'select_repositories'
        return row
