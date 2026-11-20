import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createBrowser,install,input,keydown,settle,visibleText} from './dom.mjs';
import {createAskPanel} from '../../opendevengine/web/ask.js';
const html=readFileSync(new URL('../../opendevengine/web/index.html',import.meta.url),'utf8');
const conversation=(id='a',extra={})=>({id,title:`Conversation ${id}`,scope_revision:1,repository_ids:['r1'],messages:[],runs:[],plans:[],...extra});
const plan=()=>({id:'p',title:'Plan',latest_version_id:'v',version:{id:'v',version:1,content_hash:'hash',content:{goal:'Goal',user_request:'Request',repositories:[],citations:[],findings:[],requirements:['R'],steps:[],validation:[],risks:[],assumptions:[],open_questions:[],execution_context:{execution_profile_id:'execution-1',base_choices:{}},publication:{policy:'manual_draft'}},readiness:{state:'ready',blocking_reasons:[],warnings:[],source_freshness:{}}}});
const missing={status:404,body:{code:'not_found'}};
const storage=()=>{const map=new Map();return {getItem:k=>map.get(k)??null,setItem:(k,v)=>map.set(k,v),removeItem:k=>map.delete(k)};};
let imports=0;
async function boot(respond,{hash='#/ask/a',sessionStorage=storage(),app=false}={}){
 const b=createBrowser(html,{hash,respond});b.window.sessionStorage=sessionStorage;b.window.confirm=()=>true;install(b);
 let panel,ui;
 if(app){await import(new URL(`../../opendevengine/web/app.js?recovery=${++imports}`,import.meta.url));await settle();panel=b.document.querySelector('.persistent-ask');}
 else {ui=await createAskPanel({host:b.document.getElementById('view-ask')});panel=ui?.panel;}
 const button=t=>panel.querySelectorAll('button').find(n=>n.textContent===t);
 const choice=t=>panel.querySelectorAll('.route-choice').find(n=>n.querySelector('.route-choice-name')?.textContent===t);
 const field=t=>panel.querySelectorAll('label').find(n=>n.textContent.startsWith(t))?.querySelector('textarea');
 return {...b,panel,ui,button,choice,field,text:()=>visibleText(panel)};
}

test('definite 4xx clears pending identity, keeps the draft and permits corrected/new-conversation sends',async()=>{
 for(const rejection of [400,401,403,404,409,422]){
  const attempts=[];
  const b=await boot((m,u,o)=>{
   if(u==='/api/conversations')return {items:[conversation()]};
   if(u==='/api/conversations/a')return conversation();if(u==='/api/conversations/b')return conversation('b');
   if(m==='POST' && u.endsWith('/messages')){attempts.push(JSON.parse(o.body));return attempts.length===1?{status:rejection,body:{message:'Definite rejection'}}:{id:'run',state:'queued'};}
   return missing;
  });
  input(b.field('Question'),'Original draft');b.button('Ask').click();await settle();
  assert.match(b.text(),/Definite rejection/);assert.equal(b.field('Question').value,'Original draft');assert.equal(b.button('Discard pending request'),undefined);assert.equal(b.window.sessionStorage.getItem('ask-pending-send'),null);
  input(b.field('Question'),'Corrected draft');b.button('Ask').click();await settle();assert.notEqual(attempts[0].client_request_id,attempts[1].client_request_id);
  b.window.location.hash='#/ask/b';await settle();input(b.field('Question'),'Other conversation');b.button('Ask').click();await settle();assert.equal(attempts.length,3);
 }
});

test('network/5xx ambiguity preserves client ID across reload; explicit discard preserves draft and permits a new attempt',async()=>{
 for(const kind of ['network','5xx']){
  const sessionStorage=storage(), attempts=[];
  const respond=(m,u,o)=>{
   if(u==='/api/conversations')return {items:[conversation()]};if(u==='/api/conversations/a')return conversation();
   if(u.endsWith('/messages')){attempts.push(JSON.parse(o.body));if(kind==='network')throw new Error('Network lost');return {status:503,body:{message:'Service unavailable'}};}
   return missing;
  };
  let b=await boot(respond,{sessionStorage});input(b.field('Question'),'Keep ambiguous draft');b.button('Ask').click();await settle();
  assert.ok(b.button('Discard pending request'));assert.ok(sessionStorage.getItem('ask-pending-send'));
  b=await boot(respond,{sessionStorage});assert.equal(b.field('Question').value,'Keep ambiguous draft');b.button('Ask').click();await settle();assert.equal(attempts[0].client_request_id,attempts[1].client_request_id);
  b.button('Discard pending request').click();await settle();assert.equal(b.field('Question').value,'Keep ambiguous draft');assert.equal(sessionStorage.getItem('ask-pending-send'),null);
  input(b.field('Question'),'New deliberate request');b.button('Ask').click();await settle();assert.notEqual(attempts[1].client_request_id,attempts[2].client_request_id);
 }
});

test('first history failure keeps one working persistent workspace and recovers history and saved URL',async()=>{
 let historyFails=true,pageFails=true;
 const b=await boot((m,u)=>{
  if(u==='/api/repos')return {repos:[],model:null};
  if(u==='/api/conversations'){if(historyFails)return {status:503,body:{message:'History offline'}};return {items:[conversation()]};}
  if(u==='/api/conversations/a'){if(pageFails)throw new Error('Saved URL offline');return conversation();}
  return missing;
 },{app:true});
 assert.ok(b.panel);assert.equal(b.document.querySelectorAll('.persistent-ask').length,1);assert.equal(b.document.getElementById('thread').parentNode.hidden,true);
 assert.match(b.text(),/Conversation history unavailable/);assert.match(b.text(),/Conversation unavailable/);assert.equal(b.button('Ask'),undefined);
 historyFails=false;b.button('Retry conversation history').click();await settle();assert.equal(b.document.getElementById('recent-list').querySelectorAll('a').filter(n=>n.getAttribute('href')==='#/ask/a').length,1);assert.equal(b.button('Retry conversation history'),undefined);
 pageFails=false;b.button('Retry saved page').click();await settle();assert.ok(b.button('Ask'));assert.match(b.text(),/Conversation a/);
});

test('missing saved conversation/plan URL and malformed/non-404 history keep recoverable panels',async()=>{
 for(const hash of ['#/ask/missing','#/plans/p/v']){
  for(const status of [404,500]){
   let recover=false;
   const b=await boot((m,u)=>{
    if(u==='/api/conversations')return {items:[]};
    if(u.startsWith('/api/plans/') || u==='/api/conversations/missing')return recover?(hash.includes('plans')?plan():conversation('missing')):{status,body:{message:'Saved page missing'}};
    return missing;
   },{hash});
   assert.ok(b.panel);assert.match(b.text(),/unavailable/);assert.equal(b.button('Ask'),undefined);assert.equal(b.button('Start session'),undefined);
   recover=true;b.button('Retry saved page').click();await settle();assert.ok(b.button(hash.includes('plans')?'Save immutable version':'Ask'));
  }
 }
 for(const value of [{}, {status:500,body:{message:'Offline'}}]){
  const b=await boot((m,u)=>u==='/api/conversations'?value:missing,{hash:'#/ask'});assert.ok(b.panel);assert.ok(b.button('Retry conversation history'));
 }
});

test('navigation clears stale Ask and plan actions during delayed/error GETs and rejects detached buttons',async()=>{
 for(const hash of ['#/ask/a','#/plans/p/v']){
  let reject,lateResolve;const writes=[];
  const b=await boot((m,u,o)=>{
   if(m!=='GET'){writes.push({m,u});return {};}
   if(u==='/api/conversations')return {items:[conversation()]};if(u==='/api/conversations/a')return conversation();if(u.startsWith('/api/plans/'))return plan();
   if(u==='/api/conversations/b')return new Promise((resolve,rejectFn)=>{reject=rejectFn;lateResolve=resolve;});
   return missing;
  },{hash});
  const old=hash.includes('plans')?[b.button('Save immutable version'),b.button('Start session')]:[b.button('Ask'),b.button('Generate plan')];
  b.window.location.hash='#/ask/b';await settle();assert.match(b.text(),/Loading saved Ask/);for(const button of old)button.click();await settle();assert.deepEqual(writes,[]);
  reject(new Error('B unavailable'));await settle();assert.match(b.text(),/Conversation unavailable/);for(const button of old)button.click();await settle();assert.deepEqual(writes,[]);
  b.button('Retry saved page').click();await settle();b.window.location.hash='#/ask/a';await settle();lateResolve(conversation('b'));await settle();assert.match(b.text(),/Conversation a/);assert.doesNotMatch(b.text(),/Conversation b/);
 }
});

test('history pagination in the rail deduplicates links and consumes the previous More control',async()=>{
 const b=await boot((m,u)=>{
  if(u==='/api/conversations')return {items:[conversation()],next_cursor:'next'};
  if(u==='/api/conversations?cursor=next')return {items:[conversation(),conversation('b')],next_cursor:null};
  if(u==='/api/conversations/a')return conversation();
  return missing;
 },{app:true});
 const rail=()=>b.document.getElementById('recent-list');
 const more=rail().querySelectorAll('button').find(n=>n.textContent==='More conversations');more.click();await settle();more.click();await settle();
 assert.equal(rail().querySelectorAll('button').find(n=>n.textContent==='More conversations'),undefined);
 assert.deepEqual(rail().querySelectorAll('a').map(n=>n.getAttribute('href')),['#/ask/a','#/ask/b']);
 assert.doesNotMatch(visibleText(rail()),/conv_|#\/ask/);
});

test('typing a question is a draft, confirmed discard clears it, missing scoped repos are visible',async()=>{
 const c=conversation('a',{repository_ids:['unavailable']});let confirms=0;
 const b=await boot((m,u)=>u==='/api/conversations'?{items:[c]}:u==='/api/conversations/a'?c:missing);
 b.window.confirm=()=>{confirms++;return true;};await settle();assert.match(b.text(),/unavailable · repository context unavailable/);
 b.window.location.hash='#/ask';await settle();assert.equal(confirms,0,'leaving without typing needs no confirmation');
 b.window.location.hash='#/ask/a';await settle();input(b.field('Question'),'discard me');b.window.location.hash='#/ask';await settle();assert.equal(confirms,1);
 b.window.location.hash='#/ask/a';await settle();assert.equal(b.field('Question').value,'');
 assert.deepEqual(b.panel.querySelectorAll('.chip-repo .chip-label').map(n=>n.textContent),['unavailable']);
});

test('an unscoped question routes first, asks which repository, then scopes and sends it',async()=>{
 let c=conversation('a',{repository_ids:[]});const mutations=[],sent=[];
 const b=await boot((m,u,o)=>{
  if(u==='/api/conversations')return {items:[c]};
  if(u==='/api/conversations/a'){if(m==='PATCH'){mutations.push(JSON.parse(o.body));c={...c,repository_ids:mutations.at(-1).repository_ids,scope_revision:c.scope_revision+1};}return c;}
  if(u==='/api/route'){assert.deepEqual(JSON.parse(o.body).pinned_repository_ids,[]);return {status:'needs_selection',selected_repository_ids:[],candidates:[{name:'owner/candidate',repository_id:'suggested',reason:'Observed match'}]};}
  if(u==='/api/repositories/suggested/context')return {repository_id:'suggested',name:'owner/candidate'};
  if(m==='POST' && u.endsWith('/messages')){sent.push(JSON.parse(o.body));return {id:'run',state:'queued'};}
  return missing;
 });
 input(b.field('Question'),'request');b.button('Ask').click();await settle();
 assert.match(b.text(),/Which repository\?/);assert.equal(sent.length,0,'nothing is sent before a repository is chosen');assert.equal(b.field('Question').value,'request');
 b.choice('candidate').click();await settle(20);
 assert.deepEqual(mutations,[{repository_ids:['suggested'],expected_revision:1}]);
 assert.deepEqual(sent.map(x=>x.content),['request']);
 assert.equal(b.field('Question').value,'','the sent question does not come back as a draft');assert.equal(b.window.sessionStorage.getItem('ask-draft:a'),null);
 assert.doesNotMatch(b.text(),/Choose a repository to continue/);
});

test('a question waiting for a repository is restored after reload and sent with its original intent',async()=>{
 let c=conversation('a',{repository_ids:[],pending_route:{id:'route_1',status:'needs_selection',prompt:'Plan retry handling',candidates:[{name:'owner/candidate',repository_id:'suggested',reason:'Catalog evidence: name: worker; profile: retry, order',freshness:'ready'}]}});
 const mutations=[],sent=[];const store=storage();store.setItem('ask-route-intent:a','plan');
 const b=await boot((m,u,o)=>{
  if(u==='/api/conversations')return {items:[c]};
  if(u==='/api/conversations/a'){if(m==='PATCH'){mutations.push(JSON.parse(o.body));c={...c,pending_route:null,repository_ids:mutations.at(-1).repository_ids,scope_revision:c.scope_revision+1};}return c;}
  if(u==='/api/repositories/suggested/context')return {repository_id:'suggested',name:'owner/candidate'};
  if(m==='POST' && u.endsWith('/messages')){sent.push(JSON.parse(o.body));return {id:'run',state:'queued'};}
  return missing;
 },{sessionStorage:store});
 assert.match(b.text(),/Which repository\?/,'the pending choice is shown again');
 assert.match(b.text(),/Matches worker, retry, order/,'the routing evidence is visible, not only on hover');
 assert.equal(b.field('Question').value,'Plan retry handling','the waiting question is back in the composer');
 b.choice('candidate').click();await settle(20);
 assert.deepEqual(mutations,[{repository_ids:['suggested'],expected_revision:1}]);
 assert.deepEqual(sent.map(x=>[x.content,x.intent]),[['Plan retry handling','plan']]);
 assert.equal(store.getItem('ask-route-intent:a'),null);
});

test('scope conflict re-applies the chip edit once, then explains a second conflict',async()=>{
 for(const conflicts of [1,2]){
  let c=conversation('a',{repository_ids:['r1']});let patches=0;
  const b=await boot((m,u,o)=>{
   if(u==='/api/conversations')return {items:[c]};
   if(u==='/api/conversations/a'){if(m==='PATCH'){patches++;if(patches<=conflicts)return {status:409,body:{message:'Scope revision changed.'}};c={...c,repository_ids:JSON.parse(o.body).repository_ids,scope_revision:9};return c;}return {...c,scope_revision:patches?3:1};}
   return missing;
  });
  b.panel.querySelectorAll('button').find(n=>n.getAttribute('aria-label')==='Remove r1').click();await settle(20);
  assert.equal(patches,2,'one retry after the first conflict');
  if(conflicts===1)assert.deepEqual(b.panel.querySelectorAll('.chip-repo').length,0);
  else{assert.match(b.text(),/Someone changed this conversation's repositories\. Reload to see the latest\./);assert.equal(b.panel.querySelectorAll('.chip-repo').length,1);}
 }
});

test('stored generation attribution overrides neither unknown labels nor configured current role; repeated export replaces display',async()=>{
 const c=conversation('a',{messages:[{role:'assistant',state:'complete',content:'Answer',model:'stored-model',destination:{kind:'local',host:'stored-host'},usage:{input_tokens:2,output_tokens:3,estimated_cost:0.01,cost_status:'estimated'}},{role:'assistant',state:'failed',error_code:'failed'}]});
 const b=await boot((m,u)=>u==='/api/conversations'?{items:[c]}:u==='/api/conversations/a'?c:missing);
 for(const info of b.panel.querySelectorAll('button').filter(n=>n.getAttribute('aria-label')==='Answer details'))info.click();
 assert.match(b.text(),/stored-model · local stored-host · 2 in \/ 3 out tokens · estimated 0\.01 USD/);assert.match(b.text(),/Couldn't answer: failed\./);
 const p=plan();
 const other=await boot((m,u)=>u==='/api/conversations'?{items:[]}:u.includes('/export')?{plan_version_id:'v',content_hash:'hash',markdown:'One export only'}:u.startsWith('/api/plans/')?p:missing,{hash:'#/plans/p/v'});
 other.button('Export this saved version').click();await settle();other.button('Export this saved version').click();await settle();assert.equal(other.panel.querySelectorAll('pre').filter(n=>n.textContent==='One export only').length,1);
});

test('delayed first saved-page read retires legacy workspace before completing and recovers after rejection',async()=>{
 let rejectPage;
 const b=createBrowser(html,{hash:'#/ask/a',respond:(m,u)=>{
  if(u==='/api/repos')return {repos:[],model:null};if(u==='/api/conversations')return {items:[conversation()]};
  if(u==='/api/conversations/a')return new Promise((resolve,reject)=>{rejectPage=reject;});
  return missing;
 }});install(b);b.window.confirm=()=>true;
 const imported=import(new URL(`../../opendevengine/web/app.js?recovery=${++imports}`,import.meta.url));
 await settle(40);assert.ok(rejectPage);assert.equal(b.document.querySelectorAll('.persistent-ask').length,1);assert.equal(b.document.getElementById('thread').parentNode.hidden,true);assert.match(visibleText(b.document.querySelector('.persistent-ask')),/Loading saved Ask/);
 rejectPage(new Error('Delayed first GET failed'));await imported;await settle();assert.match(visibleText(b.document.querySelector('.persistent-ask')),/Conversation unavailable/);assert.equal(b.document.getElementById('thread').parentNode.hidden,true);
});

test('cancel/retry refresh preserves usable keyboard focus and stale retired controls cannot submit',async()=>{
 let run={id:'run',state:'queued',assistant_message_id:'m2'},posts=0;
 const messages=()=>[{id:'m1',role:'user',state:'complete',content:'Q',created_at:1},{id:'m2',role:'assistant',state:run.state==='queued'?'pending':run.state,content:null,created_at:2}];
 const b=await boot((m,u)=>{
  if(u==='/api/conversations')return {items:[conversation()]};if(u==='/api/conversations/a')return conversation('a',{runs:[run],messages:messages()});
  if(m==='POST' && u.endsWith('/cancel')){posts++;run={...run,state:'cancelled'};return run;}
  if(m==='POST' && u.endsWith('/retry')){posts++;run={...run,state:'queued'};return run;}
  return missing;
 });
 const cancel=b.button('Cancel');cancel.focus();cancel.click();await settle();assert.notEqual(b.document.activeElement,b.document.body);assert.equal(b.document.activeElement.isConnected,true);cancel.click();await settle();assert.equal(posts,1,'a retired Cancel cannot post again');
 const retry=b.button('Retry');retry.focus();retry.click();await settle();assert.notEqual(b.document.activeElement,b.document.body);assert.equal(b.document.activeElement.isConnected,true);assert.equal(posts,2);
 b.window.location.hash='#/ask';await settle();
});

test('browser-like NodeLists without Array helpers support the picker, modal focus trap and chips',async()=>{
 const repo=id=>({repository_id:id,name:`owner/${id}`,status:'ready',eligible:true,active_snapshot:{mode:'lexical_only'},active_run:null});let c=conversation();
 const b=createBrowser(html,{hash:'#/ask/a',respond:(m,u,o)=>{
  if(u==='/api/conversations')return {items:[c]};
  if(u==='/api/conversations/a'){if(m==='PATCH')c={...c,repository_ids:JSON.parse(o.body).repository_ids,scope_revision:2};return c;}
  if(u.startsWith('/api/context/repositories'))return {items:[repo('r1'),repo('r2')],next_cursor:null};
  if(u==='/api/repositories/r1/context')return repo('r1');if(u==='/api/repositories/r2/context')return repo('r2');
  return missing;
 }});b.window.sessionStorage=storage();b.window.localStorage=storage();install(b);
 function nodeList(items){const list={length:items.length,item:i=>items[i]??null,forEach:fn=>items.forEach(fn),[Symbol.iterator]:()=>items[Symbol.iterator]()};items.forEach((item,i)=>{list[i]=item;});return Object.freeze(list);}
 function wrap(element){const query=element.querySelectorAll;element.querySelectorAll=selector=>nodeList(query.call(element,selector));return element;}
 for(const element of b.document.descendants())wrap(element);wrap(b.document);
 const create=b.document.createElement.bind(b.document);b.document.createElement=tag=>wrap(create(tag));
 b.window.confirm=()=>true;
 const ui=await createAskPanel({host:b.document.getElementById('view-ask')});await settle();
 const find=(root,fn)=>[...root.querySelectorAll('button')].find(fn);
 assert.equal(ui.panel.querySelectorAll('button').find,undefined,'NodeList has no Array helpers');
 find(ui.panel,n=>n.getAttribute('aria-label')==='Add repositories').click();await settle(40);assert.equal(b.document.querySelectorAll('.picker-row').length,2,'picker rows render');
 const dialog=b.document.querySelector('dialog.modal');const search=dialog.querySelector('input');
 keydown(search,'ArrowDown');assert.equal(b.document.activeElement.type,'checkbox');keydown(b.document.activeElement,'ArrowDown');
 b.document.activeElement.click();const add=find(dialog,n=>n.textContent.startsWith('Add'));add.focus();keydown(add,'Tab');
 assert.equal(b.document.activeElement,find(dialog,n=>n.getAttribute('aria-label')==='Close'),'Tab wraps inside the modal');
 add.click();await settle(20);
 assert.deepEqual(c.repository_ids,['r1','r2']);
 assert.equal(ui.panel.tagName,'MAIN');ui.focus();assert.equal(b.document.activeElement,ui.panel.querySelector('h1'));
});

test('persistent Ask route focuses visible H1 instead of retired legacy title',async()=>{
 const b=await boot((m,u)=>u==='/api/repos'?{repos:[],model:null}:u==='/api/conversations'?{items:[]}:missing,{app:true,hash:'#/ask'});
 b.window.location.hash='#/settings';await settle();b.window.location.hash='#/ask';await settle();
 const title=b.panel.querySelector('h1');assert.equal(b.panel.tagName,'MAIN');assert.equal(b.document.activeElement,title);assert.equal(title.inHiddenSubtree,false);
});


test('completed generation loads automatically for heading/button focus and preserves editable focus',async()=>{
 for(const editing of [false,true]){
  let completed=false,reads=0;
  const b=await boot((m,u)=>{
   if(u==='/api/conversations')return {items:[conversation()]};
   if(u==='/api/conversations/a'){reads++;return conversation('a',{runs:completed?[]:[{id:'r',state:'running'}],messages:completed?[{role:'assistant',state:'complete',content:'Automatically loaded answer',citations:[]}]:[]});}
   if(u==='/api/answer-runs/r'){completed=true;return {id:'r',state:'completed'};}
   return missing;
  });
  if(editing)b.field('Question').focus();else b.button('Cancel').focus();
  await new Promise(resolve=>setTimeout(resolve,1600));await settle();
  if(editing){assert.equal(reads,2);assert.match(b.text(),/Automatically loaded answer/);assert.doesNotMatch(b.text(),/Reload saved conversation/);assert.equal(b.document.activeElement,b.field('Question'),'typing focus is kept');}
  else {assert.equal(reads,2);assert.match(b.text(),/Automatically loaded answer/);assert.equal(b.document.activeElement.tagName,'H3');}
 }
});

test('coverage and destination expose useful summaries without internal JSON',async()=>{
 const c=conversation('a',{repository_ids:['r']});
 const b=await boot((m,u)=>{
  if(u==='/api/conversations')return {items:[c]};if(u==='/api/conversations/a')return c;
  if(u==='/api/repositories/r/context')return {name:'App',repository_id:'r',status:'ready',active_snapshot:{revision:'base',coverage:{files:4,chunks:12,selection_hash:'private-internal-hash',partial:true}},implementation_enabled:false};
  if(u==='/api/settings/model-roles')return {settings:{local_only:true},roles:{ask:{model:'local-model',status:'ready',destination:{kind:'local',host:'localhost'}}}};
  return missing;
 });
 await settle();assert.match(b.text(),/4 files · 12 chunks · partial coverage/);assert.match(b.text(),/local · localhost/);assert.doesNotMatch(b.text(),/selection_hash|private-internal-hash/);
});

test('a conflicting scope edit keeps the other tab change and re-applies only this edit',async()=>{
 let server={...conversation('a',{repository_ids:['x','y']}),scope_revision:2};const patches=[];
 const b=await boot((m,u,o)=>{
  if(u==='/api/conversations')return {items:[server]};
  if(u==='/api/conversations/a'){
   if(m==='PATCH'){const body=JSON.parse(o.body);patches.push(body);if(body.expected_revision!==server.scope_revision)return {status:409,body:{message:'Scope revision changed.'}};server={...server,repository_ids:body.repository_ids,scope_revision:server.scope_revision+1};return server;}
   return patches.length?server:{...server,repository_ids:['x'],scope_revision:1};
  }
  if(u==='/api/repositories/x/context')return {repository_id:'x',name:'owner/x'};
  if(u==='/api/repositories/y/context')return {repository_id:'y',name:'owner/y'};
  return missing;
 });
 b.panel.querySelectorAll('button').find(n=>n.getAttribute('aria-label')==='Remove x').click();await settle(30);
 assert.deepEqual(patches.map(p=>[p.repository_ids,p.expected_revision]),[[[],1],[['y'],2]]);
 assert.deepEqual(b.panel.querySelectorAll('.chip-repo .chip-label').map(n=>n.textContent),['y']);
 assert.match(b.text(),/Removed x/);assert.doesNotMatch(b.text(),/Removed y|Added y/);
});

test('a failed repository choice or retry is reported and keeps the pending question',async()=>{
 const c=conversation('a',{repository_ids:[],messages:[{id:'m1',role:'user',state:'complete',content:'Q',created_at:1},{id:'m2',role:'assistant',state:'failed',error_code:'provider_timeout',created_at:2}],runs:[{id:'run1',assistant_message_id:'m2',state:'failed'}]});
 const b=await boot((m,u)=>{
  if(u==='/api/conversations')return {items:[c]};if(u==='/api/conversations/a'){if(m==='PATCH')return {status:500,body:{message:'Scope store offline'}};return c;}
  if(u==='/api/route')return {status:'needs_selection',selected_repository_ids:[],candidates:[{name:'owner/candidate',repository_id:'suggested',reason:'match'}]};
  if(u==='/api/answer-runs/run1/retry')return {status:503,body:{message:'Retry queue offline'}};
  return missing;
 });
 b.button('Retry').click();await settle();assert.match(b.text(),/Retry queue offline/);
 input(b.field('Question'),'request');b.button('Ask').click();await settle();
 b.choice('candidate').click();await settle(20);
 assert.match(b.text(),/Scope store offline/);assert.match(b.text(),/Which repository\?/,'the choice stays available');assert.equal(b.field('Question').value,'request');
});
