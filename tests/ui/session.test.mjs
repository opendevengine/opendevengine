import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createBrowser, install, settle, input, keydown, visibleText} from './dom.mjs';
import {createSessionPanel, createExecutionSettings} from '../../opendevengine/web/sessions.js';
import {createAskPanel} from '../../opendevengine/web/ask.js';
const html = readFileSync(new URL('../../opendevengine/web/index.html', import.meta.url), 'utf8');
const css = readFileSync(new URL('../../opendevengine/web/styles.css', import.meta.url), 'utf8');
const session = (id='s', changes={}) => ({id,plan_id:'p',plan_version_id:'v',content_hash:'a'.repeat(64),attempt:1,state:'running',phase:'agent',source_available:true,execution:{profile_id:'e',profile_version_id:'ev',version:2,adapter:'claude_code',cli_version:'2.1',image_id:'sha256:image',destination:{kind:'remote',disclosure:'source leaves machine'}},limits:{runner:{memory_mib:512,wall_seconds:600},agent:{max_turns:12,max_budget_usd:1}},repositories:[{repository_id:'gh:1',role:'write',base_sha:'b'.repeat(40),base_branch:'main',session_branch:'session/s'}],runner:{containment:'pending'},capabilities:{ask_user:true,follow_up:false},usage:{},outcome:{},publication:{policy:'manual'},created_at:Date.now()-5000,latest_seq:0,pending_question:null,...changes});
const profile = (id='e', changes={}) => ({id,name:'Coding',active_version_id:'ev',version:{id:'ev',version:1,adapter:'claude_code',auth_mode:'cli_login',config:{image:'python:3.12-slim',allow_external_source:true,runner_limits:{memory_mib:512},agent_limits:{max_turns:12}}},check:null,...changes});
const button = (root, text) => [...root.querySelectorAll('button')].find(n=>n.textContent===text);
const field = (root, text) => [...root.querySelectorAll('label')].find(n=>n.textContent.startsWith(text))?.querySelector('input, textarea, select');
// The composer is found through its visible label, which names what a message will do.
const composer = root => field(root,'Send guidance') || field(root,"Answer the agent's question");
function requestFor(b) {return async (path, method='GET',body)=>{const r=await b.window.fetch(path,{method,...(body===undefined?{}:{body:JSON.stringify(body)})});const v=await r.json();if(!r.ok){const e=new Error(v.code||'request failed');e.status=r.status;e.code=v.code;throw e;}return v;};}
function basic(m,u){if(u.includes('/events?'))return {items:[],next_seq:0,has_more:false};if(u.includes('/messages?'))return {items:[],next_cursor:null};if(u.endsWith('/diff'))return {available:false,reason:'no_checkpoint'};if(u.endsWith('/validations'))return {items:[],fully_validated:false};return session();}
function setup(respond, hash='#/sessions/s') {const b=createBrowser(html,{hash,respond});install(b);b.document.getElementById('view-sessions').hidden=false;b.document.getElementById('view-settings').hidden=false;const data=new Map();b.window.sessionStorage={getItem:k=>data.get(k),setItem:(k,v)=>data.set(k,v),removeItem:k=>data.delete(k)};b.window.confirm=()=>true;return b;}

test('session replay advances server cursor, deduplicates, preserves focused composer and unknown usage',async()=>{
 let reads=0;
 const b=setup((m,u)=>{
  if(u.includes('/events?')){reads++;return reads===1?{items:[{session_id:'s',seq:1,type:'agent.summary',payload:{kind:'ready',model:'actual-model'},created_at:1}],next_seq:1,has_more:true}:{items:[{session_id:'s',seq:1,type:'agent.summary',payload:{kind:'ready',model:'actual-model'},created_at:1},{session_id:'s',seq:2,type:'tool.finished',payload:{tool:'write_file',path:'<unsafe>.py',ok:true},created_at:2}],next_seq:2,has_more:false};}
  return basic(m,u);
 });
 const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:requestFor(b),pollMs:1e6});ui.open();await settle();
 assert.match(visibleText(ui.panel),/UsageunknownCostunknown/);assert.match(visibleText(ui.panel),/Follow-ups no proven delivery channel/);
 const compose=composer(ui.panel);input(compose,'Keep this draft');compose.focus();
 button(ui.panel,'Load more activity').click();await settle();
 assert.equal(ui.panel.querySelectorAll('[data-seq="1"]').length,1);assert.equal(ui.panel.querySelectorAll('[data-seq="2"]').length,1);
 assert.equal(b.document.activeElement,compose);assert.equal(compose.value,'Keep this draft');assert.match(visibleText(ui.panel),/<unsafe>.py/);assert.equal(ui.panel.querySelector('unsafe'),null);
 assert.ok(b.requests.some(r=>r.url.includes('after=1&limit=200')));ui.close();
});

test('source loss clears old diff, questions, logs, messages and replays redacted history',async()=>{
 let allowed=true;
 const b=setup((m,u)=>{
  if(u.includes('/events?'))return {items:[{session_id:'s',seq:1,type:'agent.summary',payload:allowed?{kind:'assistant',text:'PRIVATE_SUMMARY'}:{redacted:true},created_at:1}],next_seq:1,has_more:false};
  if(u.endsWith('/diff'))return {available:true,files:[{path:'secret.py',status:'modified',diff:'PRIVATE_DIFF'}]};
  if(u.endsWith('/validations'))return {items:[{id:'v',ordinal:1,status:'failed',command:['test'],output:'PRIVATE_LOG'}],fully_validated:false};
  if(u.includes('/messages?'))return {items:[{id:'m',created_at:1,delivery:'queued',content:allowed?'PRIVATE_MESSAGE':null}],next_cursor:null};
  return session('s',{source_available:allowed,pending_question:allowed?{id:'q',prompt:'PRIVATE_QUESTION'}:{id:'q',prompt:null,redacted:true}});
 });const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:requestFor(b),pollMs:1e6});ui.open();await settle();
 const tab=name=>ui.panel.querySelectorAll('[role="tab"]').find(t=>t.textContent===name).click();
 tab('Diff');assert.match(visibleText(ui.panel),/PRIVATE_DIFF/);tab('Checks');assert.match(visibleText(ui.panel),/PRIVATE_LOG/);allowed=false;await ui.refresh();await settle();
 // Hidden tabs count too: revoked source must be gone from the DOM, not just out of view.
 assert.doesNotMatch(ui.panel.textContent,/PRIVATE_/);assert.match(visibleText(ui.panel),/redacted/);assert.equal(composer(ui.panel).disabled,true);assert.ok(b.requests.filter(r=>r.url.includes('events?after=0')).length>=2);ui.close();
});

test('late responses and detached actions cannot repaint or mutate another session',async()=>{
 let finish;let sends=0;
 const b=setup((m,u)=>{if(u==='/api/sessions/old')return new Promise(r=>finish=r);if(m==='POST')sends++;return basic(m,u);},'#/sessions/old');
 const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:requestFor(b),pollMs:1e6});ui.open();await settle();
 const oldStop=button(ui.panel,'Stop session');b.window.location.hash='#/sessions/new';await settle();finish(session('old'));await settle();
 // The late answer for the old session never repaints the new page; its detached Stop sends nothing.
 assert.doesNotMatch(ui.panel.textContent,/1 · old/);assert.equal(ui.panel.querySelector('.session-title').textContent,'Session');oldStop.click();assert.equal(sends,0);ui.close();
});

test('unknown message outcome preserves key and typed question target across refresh and terminal reconciliation',async()=>{
 let state='waiting_for_user', question={id:'q',prompt:'Choose an option'};const sends=[];
 const b=setup((m,u,o)=>{if(m==='POST'&&u.endsWith('/messages')){const body=JSON.parse(o.body);sends.push(body);if(sends.length===1){state='completed';question=null;throw new Error('lost');}return {id:'m',session_id:'s',...body,delivery:'acknowledged',created_at:1};}if(u==='/api/sessions/s')return session('s',{state,pending_question:question});return basic(m,u);});
 const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:requestFor(b),pollMs:1e6});ui.open();await settle();const compose=composer(ui.panel);input(compose,'Use the bounded option');
 button(ui.panel,'Send').click();await settle();assert.match(visibleText(ui.panel),/lost/);await ui.refresh();await settle();
 assert.equal(compose.value,'Use the bounded option');assert.equal(field(ui.panel,'Message target').value,'q');assert.equal(button(ui.panel,'Send').disabled,false);
 button(ui.panel,'Send').click();await settle();assert.equal(sends.length,2);assert.deepEqual(sends[0],sends[1]);assert.equal(sends[1].question_id,'q');assert.match(visibleText(ui.panel),/acknowledged/);ui.close();
});

test('definite message rejection permits corrected draft; delivery state never inferred from tool result',async()=>{
 let attempt=0;const sends=[];
 const b=setup((m,u,o)=>{if(m==='POST'){sends.push(JSON.parse(o.body));if(++attempt===1)return {status:409,body:{code:'plan_amendment_required'}};return {id:'m',...sends.at(-1),delivery:'queued',reason:'follow_up_unsupported',created_at:1};}return basic(m,u);});
 const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:requestFor(b),pollMs:1e6});ui.open();await settle();const compose=composer(ui.panel);input(compose,'Grant access');button(ui.panel,'Send').click();await settle();input(compose,'Please clarify the result');button(ui.panel,'Send').click();await settle();
 assert.notEqual(sends[0].client_request_id,sends[1].client_request_id);assert.match(visibleText(ui.panel),/queued/);assert.doesNotMatch(visibleText(ui.panel),/Clarification · acknowledged/);ui.close();
});

test('stop remains stopping until server containment and retry unknown outcomes reuse one key',async()=>{
 let s=session();let retries=[];const b=setup((m,u,o)=>{if(m==='POST'&&u.endsWith('/stop')){s=session('s',{state:'stopping',stop_requested:true,runner:{containment:'pending'}});return {state:'stopping'};}if(m==='POST'&&u.endsWith('/retry')){retries.push(JSON.parse(o.body));if(retries.length===1)throw new Error('lost retry');return {session_id:'successor',state:'queued'};}if(u==='/api/sessions/s')return s;return basic(m,u);});
 const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:requestFor(b),pollMs:1e6});ui.open();await settle();button(ui.panel,'Stop session').click();await settle();assert.match(visibleText(ui.panel),/not confirmed stopped/);assert.equal(button(ui.panel,'Retry as a new attempt').disabled,true);
 s=session('s',{state:'cancelled',runner:{containment:'confirmed'}});await ui.refresh();button(ui.panel,'Retry as a new attempt').click();await settle();button(ui.panel,'Retry as a new attempt').click();await settle();assert.equal(retries.length,2);assert.equal(retries[0].idempotency_key,retries[1].idempotency_key);assert.equal(b.window.location.hash,'#/sessions/successor');ui.close();
});

test('session list URLs paginate and deduplicate; unavailable checks stay distinct from failure',async()=>{
 const b=setup((m,u)=>u.includes('/api/sessions?')?{items:[session('s'),session('s')],next_cursor:u.includes('cursor=')?null:'next'}:basic(m,u),'#/sessions');const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:requestFor(b),pollMs:1e6});ui.open();await settle();button(ui.panel,'More sessions').click();await settle();assert.equal(ui.panel.querySelectorAll('a[href="#/sessions/s"]').length,1);assert.equal(button(ui.panel,'More sessions').hidden,true);ui.close();
});

test('execution profiles create human limits, test polls immutable check, update and delete',async()=>{
 let profiles=[],versionCheck=null;const mutations=[];
 const b=setup((m,u,o)=>{
  if(m==='POST'&&u==='/api/execution-profiles'){const body=JSON.parse(o.body);mutations.push(body);const p=profile();p.version.config={...body};profiles=[p];return p;}
  if(m==='POST'&&u.endsWith('/test')){versionCheck={status:'running'};return {job_id:'job',version_id:'ev',status:'queued'};}
  if(u==='/api/execution-profile-versions/ev')return {id:'ev',check:versionCheck};
  if(m==='PATCH'&&u==='/api/execution-profiles/e'){mutations.push(JSON.parse(o.body));return profile('e',{version:{...profile().version,version:2}});}
  if(m==='DELETE')return {deleted_at:1};
  if(u.startsWith('/api/execution-profiles?'))return {items:profiles,next_cursor:null};
  if(u==='/api/settings/execution')return {max_active_sessions:1};if(u==='/api/settings/model-roles')return {settings:{local_only:true}};
  return {status:404,body:{code:'not_found'}};
 },'#/settings');const ui=createExecutionSettings({request:requestFor(b),pollMs:20});b.document.body.append(ui.element);await ui.refresh();
 assert.match(visibleText(ui.element),/local-only: enabled/);assert.match(visibleText(ui.element),/Never paste CLI tokens/);
 input(field(ui.element,'Execution profile name'),'My coding profile');input(field(ui.element,'Locally provisioned'),'python:3.12-slim');button(ui.element,'Create execution profile').click();await settle();
 assert.equal(mutations[0].adapter,'claude_code');assert.equal(mutations[0].agent_limits.max_turns,12);assert.equal(mutations[0].runner_limits.memory_mib,512);assert.equal(mutations[0].allow_external_source,false);
 button(ui.element,'Test execution profile Coding').click();await settle();assert.match(visibleText(ui.element),/queued|running/);assert.equal(button(ui.element,'Test execution profile Coding').disabled,true);
 versionCheck={status:'failed',error_code:'authentication_required'};await new Promise(r=>setTimeout(r,60));await settle();assert.match(visibleText(ui.element),/authentication required/);assert.doesNotMatch(visibleText(ui.element),/test: passed/);
 button(ui.element,'Edit execution profile Coding').click();input(field(ui.element,'Execution profile name'),'Renamed');button(ui.element,'Save execution profile version').click();await settle();assert.equal(mutations[1].name,'Renamed');assert.match(visibleText(ui.element),/Version 2/);
 button(ui.element,'Delete execution profile Coding').click();button(ui.element,'Confirm delete execution profile Coding').click();await settle();assert.match(visibleText(ui.element),/No execution profiles/);ui.close();
});

test('execution API failure offers no create editor before discovery and test read failure never reports success',async()=>{
 const b=setup(()=>({status:404,body:{code:'not_found'}}),'#/settings');const ui=createExecutionSettings({request:requestFor(b),pollMs:1e6});b.document.body.append(ui.element);await ui.refresh();assert.equal(button(ui.element,'Create execution profile'),undefined);assert.match(visibleText(ui.element),/unavailable in this build/);ui.close();
});

test('profile and publication choices save only a new plan version and preserve all other intent',async()=>{
 const content={goal:'Preserve intent',user_request:'Fix a bug',repositories:[],citations:[],findings:[],requirements:['Keep safety'],steps:[{title:'Fix',description:'small change',paths:['x'],citation_ids:[]}],validation:[],risks:['risk'],assumptions:['assumption'],open_questions:[],execution_context:{execution_profile_id:null,base_choices:{r:'retain'},skills:[{id:'skill',version:'one'}],plugins:[],memory:[]},publication:{policy:'manual_draft'}};
 const version={id:'v',version:1,content_hash:'a'.repeat(64),content,readiness:{state:'blocked',blocking_reasons:['execution_test_required'],warnings:[],source_freshness:{},execution:{profile_id:'e',profile_version_id:'ev',image_id:'sha256:image',cli_version:'2.1',limits:{agent:{max_turns:12}},destination:{kind:'remote'}}}};
 let saved;const b=setup((m,u,o)=>{
  if(u==='/api/conversations')return {items:[]};if(u.startsWith('/api/plans/p?'))return {id:'p',title:'Plan',latest_version_id:'v',version};
  if(u.startsWith('/api/execution-profiles?'))return {items:[profile()],next_cursor:null};
  if(m==='POST'&&u==='/api/plans/p/versions'){saved=JSON.parse(o.body);return {version:{id:'v2'}};}return {status:404,body:{code:'not_found'}};
 },'#/plans/p/v');b.document.getElementById('view-ask').hidden=false;
 const ui=await createAskPanel({host:b.document.getElementById('view-ask')});await settle();
 assert.match(visibleText(ui.panel),/Immutable version ev/);assert.match(visibleText(ui.panel),/Turns 12/);assert.equal(button(ui.panel,'Start session').disabled,true);
 const execution=field(ui.panel,'Execution profile for');execution.value='e';execution.dispatchEvent({type:'change',bubbles:true});
 const publication=field(ui.panel,'Publication policy for');publication.value='on_success_draft';publication.dispatchEvent({type:'change',bubbles:true});
 button(ui.panel,'Save immutable version').click();await settle();
 assert.equal(saved.parent_version_id,'v');assert.equal(saved.content.execution_context.execution_profile_id,'e');assert.equal(saved.content.publication.policy,'on_success_draft');
 const expected=structuredClone(content);expected.execution_context.execution_profile_id='e';expected.publication.policy='on_success_draft';assert.deepEqual(saved.content,expected);assert.equal(content.execution_context.execution_profile_id,null);assert.equal(content.publication.policy,'manual_draft');
});

test('message history pagination deduplicates and source revocation in a feed fences stale successful siblings',async()=>{
 let deny=false;const b=setup((m,u)=>{
  if(u.includes('/messages?'))return {items:[{id:'m',created_at:1,content:'OLD_MESSAGE',delivery:'delivered'}],next_cursor:u.includes('cursor=')?null:'next',source_available:!deny};
  if(u.endsWith('/diff'))return {available:true,files:[{path:'x',diff:'OLD_SOURCE'}]};
  if(u.endsWith('/validations'))return {items:[{ordinal:1,status:'failed',command:['test'],output:'OLD_OUTPUT'}],fully_validated:false};
  return basic(m,u);
 });const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:requestFor(b),pollMs:1e6});ui.open();await settle();button(ui.panel,'More messages').click();await settle();assert.equal(ui.panel.querySelectorAll('.session-message').length,1);
 deny=true;await ui.refresh();await settle();assert.doesNotMatch(ui.panel.textContent,/OLD_/);assert.equal(composer(ui.panel).disabled,true);ui.close();
});

test('terminal refresh preserves focused message draft as read-only and no duplicate send starts while pending',async()=>{
 let state='running',resolveSend;const sent=[];const b=setup((m,u,o)=>{
  if(m==='POST'&&u.endsWith('/messages')){sent.push(JSON.parse(o.body));return new Promise(resolve=>resolveSend=resolve);}
  if(u==='/api/sessions/s')return session('s',{state});return basic(m,u);
 });const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:requestFor(b),pollMs:1e6});ui.open();await settle();
 const compose=composer(ui.panel);input(compose,'Keep my draft');compose.focus();state='completed';await ui.refresh();assert.equal(b.document.activeElement,compose);assert.equal(compose.readOnly,true);assert.equal(compose.value,'Keep my draft');assert.equal(button(ui.panel,'Send').disabled,true);
 state='running';await ui.refresh();button(ui.panel,'Send').click();await settle();await ui.refresh();button(ui.panel,'Send').click();await settle();assert.equal(sent.length,1);
 resolveSend({id:'m',delivery:'queued',created_at:1});await settle();ui.close();
});

test('unknown profile mutation blocks repeat writes until saved versions are reloaded',async()=>{
 let writes=0;const b=setup((m,u)=>{if(m==='POST'){writes++;throw new Error('lost');}if(u.startsWith('/api/execution-profiles?'))return {items:[],next_cursor:null};if(u==='/api/settings/execution')return {max_active_sessions:1};return {settings:{local_only:false}};},'#/settings');
 const ui=createExecutionSettings({request:requestFor(b),pollMs:1e6});b.document.body.append(ui.element);await ui.refresh();input(field(ui.element,'Execution profile name'),'Pending');input(field(ui.element,'Locally provisioned'),'python:3.12-slim');button(ui.element,'Create execution profile').click();await settle();button(ui.element,'Create execution profile').click();await settle();assert.equal(writes,1);assert.match(visibleText(ui.element),/unknown outcome/);
 await ui.refresh();button(ui.element,'Create execution profile').click();await settle();assert.equal(writes,2);ui.close();
});

test('missing sessions API shows one honest unavailable state with no dead controls',async()=>{
 const b=createBrowser('<!doctype html><html><body><main id=host></main></body></html>',{hash:'#/sessions',respond(){}});install(b);
 const host=b.document.getElementById('host');
 const ui=createSessionPanel({host,request:async()=>{const e=new Error('not_found');e.status=404;throw e;},pollMs:5});
 ui.open();await settle();
 assert.match(visibleText(host),/Sessions aren't available in this build\./);
 assert.equal(host.querySelectorAll('input').length,0);assert.equal(host.querySelectorAll('select').length,0);
 assert.equal(host.querySelectorAll('button').filter(x=>!x.hidden).length,0);
 assert.ok(host.querySelectorAll('a').find(a=>a.getAttribute('href')==='#/ask'));
});

test('session detail has a center thread and a page-owned pane with Progress, Diff, Checks and PR tabs',async()=>{
 const plan={id:'p',title:'Retry plan',latest_version_id:'v',version:{id:'v',version:3,content:{steps:[{title:'Bound retries'},{title:'Add test'}]}}};
 const b=setup((m,u)=>{
  if(u.startsWith('/api/plans/p'))return plan;
  if(u.endsWith('/diff'))return {available:true,base_sha:'b'.repeat(40),files:[{path:'a.py',status:'modified',additions:4,deletions:1,diff:'+x'},{path:'b.py',status:'added',additions:2,deletions:0,diff:'+y'}]};
  return basic(m,u);
 });
 const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:requestFor(b),pollMs:1e6});ui.open();await settle();
 const main=ui.panel.querySelector('.session-main'),pane=ui.panel.querySelector('.session-pane');
 assert.ok(main&&pane);assert.equal(main.querySelector('.session-pane'),null);
 const header=ui.panel.querySelector('.session-header');assert.equal(header.querySelector('h2').textContent,'Retry plan');
 const stages=header.querySelectorAll('.session-stage');assert.deepEqual(stages.map(x=>x.querySelector('.session-stage-label').textContent),['Prepare','Implement','Verify','Review']);
 assert.equal(stages[1].classList.contains('is-current'),true);
 const planLink=header.querySelector('a[href="#/plans/p/v"]');assert.equal(planLink.textContent,'Plan v3');assert.match(planLink.getAttribute('aria-label'),/Retry plan/);
 assert.equal(composer(main).getAttribute('placeholder'),'Guide the agent while it works…');
 const tabs=pane.querySelectorAll('[role="tab"]');assert.deepEqual(tabs.map(t=>t.textContent),['Progress','Diff','Checks','PR']);
 assert.equal(pane.querySelector('.session-workspace-head'),null,'the tablist is the pane heading; no duplicate Workspace banner');
 assert.match(css,/\.session-tabs\s*\{[^}]*top:\s*0;/,'desktop tabs stick at the top after the workspace banner was removed');
 assert.doesNotMatch(css,/\.session-workspace-(?:head|mark)\s*\{/,'removed workspace banner has no stale CSS');
 assert.ok(pane.querySelector('.session-run'), 'run details belong to the workspace inspector');
 assert.match(visibleText(pane),/Bound retries/);assert.doesNotMatch(visibleText(pane),/a\.py/);
 tabs[0].focus();keydown(tabs[0],'ArrowRight');await settle();
 assert.equal(b.document.activeElement.textContent,'Diff');assert.match(visibleText(pane),/2 files · \+6 −1 · base main@bbbbbbb/);assert.match(visibleText(pane),/a\.py/);
 keydown(b.document.activeElement,'End');await settle();
 assert.match(visibleText(pane),/Publishing isn't available yet/);assert.match(visibleText(pane),/Saved publication policy: manual/);
 // The pane is part of the page: Escape and navigation within the session never dismiss it.
 keydown(b.document.activeElement,'Escape');assert.equal(pane.hidden,false);assert.ok(pane.isConnected);ui.close();
});

test('the phase rail never claims unverified work for failed terminal sessions',async t=>{
 const cases=[
  ['failed',[],0],
  ['cancelled',[{session_id:'s',seq:1,type:'session.state',payload:{state:'running'},created_at:1}],1],
  ['interrupted',[{session_id:'s',seq:1,type:'validation.started',payload:{ordinal:1},created_at:1}],2],
  ['completed',[],3],
 ];
 for(const [state,eventItems,current] of cases)await t.test(state,async()=>{
  const b=setup((m,u)=>{
   if(u.includes('/events?'))return {items:eventItems,next_seq:eventItems.length,has_more:false};
   if(u==='/api/sessions/s')return session('s',{state,phase:state,runner:{containment:'confirmed'}});
   return basic(m,u);
  });
  const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:requestFor(b),pollMs:1e6});ui.open();await settle();
  const stages=ui.panel.querySelectorAll('.session-stage');
  assert.equal(stages[current].classList.contains('is-current'),true,state);
  if(state==='completed')assert.equal(stages.every(stage=>stage.classList.contains('is-done')),true);
  else {
   assert.equal(stages[current].classList.contains('is-stopped'),true,state);
   assert.equal(stages.filter(stage=>stage.classList.contains('is-done')).length,current,state);
  }
  ui.close();
 });
});

test('Stop sits in the session header while active and is absent once terminal and contained',async()=>{
 let s=session('s',{state:'running'});const b=setup((m,u)=>u==='/api/sessions/s'?s:basic(m,u));
 const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:requestFor(b),pollMs:1e6});ui.open();await settle();
 const stop=()=>button(ui.panel,'Stop session');
 assert.equal(stop().hidden,false);assert.ok(stop().closest('.session-header'));
 s=session('s',{state:'waiting_for_user'});await ui.refresh();assert.equal(stop().hidden,false);
 s=session('s',{state:'completed',runner:{containment:'confirmed'}});await ui.refresh();assert.equal(stop().hidden,true);
 // A failed containment keeps the stop control, worded as a repeat request, matching the result card's advice.
 s=session('s',{state:'failed',runner:{containment:'failed'}});await ui.refresh();const again=ui.panel.querySelector('.session-header .session-stop');assert.equal(again.hidden,false);assert.equal(again.textContent,'Request stop again');assert.match(visibleText(ui.panel.querySelector('.session-result')),/use Request stop again/);ui.close();
});

test('a question card sends an offered choice to that question and Reply… targets the composer',async()=>{
 const sends=[];const b=setup((m,u,o)=>{
  if(m==='POST'&&u.endsWith('/messages')){sends.push(JSON.parse(o.body));return {id:'m',...sends.at(-1),delivery:'delivered',created_at:1};}
  if(u==='/api/sessions/s')return session('s',{state:'waiting_for_user',pending_question:{id:'q',prompt:'Which retry limit?',options:['3 attempts','5 attempts']}});
  return basic(m,u);
 });
 const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:requestFor(b),pollMs:1e6});ui.open();await settle();
 const card=ui.panel.querySelector('.question-card');assert.match(visibleText(card),/Which retry limit\?/);
 button(card,'5 attempts').click();await settle();
 assert.equal(sends.length,1);assert.equal(sends[0].content,'5 attempts');assert.equal(sends[0].question_id,'q');
 button(ui.panel.querySelector('.question-card'),'Reply…').click();assert.equal(b.document.activeElement,composer(ui.panel));assert.equal(field(ui.panel,'Message target').value,'q');ui.close();
});

test('lost connectivity keeps the last confirmed state and says Reconnecting',async()=>{
 let down=false;const b=setup((m,u)=>{if(down)throw new Error('offline');return basic(m,u);});
 const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:requestFor(b),pollMs:1e6});ui.open();await settle();
 down=true;await ui.refresh();await settle();
 const indicator=ui.panel.querySelector('.session-live');assert.equal(indicator.getAttribute('role'),'status');assert.match(visibleText(indicator),/Reconnecting… · last update now/);
 assert.equal(button(ui.panel,'Reconnect').hidden,false);assert.equal(ui.panel.querySelector('.session-phase').textContent,'running');ui.close();
});

test('the rail Recent list shows sessions with repository, attempt and phase while in Sessions',async()=>{
 const recent=[];const shell={setHeader(){},setRecent(items,opts){recent.push({items,opts});}};
 const b=setup((m,u)=>u.includes('/api/sessions?')?{items:[session('s1',{state:'waiting_for_user'}),session('s2',{state:'completed'})],next_cursor:null}:basic(m,u),'#/sessions');
 const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:requestFor(b),shell,pollMs:1e6});ui.open();await settle();
 const last=recent.at(-1);assert.equal(last.opts.heading,'Recent sessions');
 assert.deepEqual(last.items.map(i=>i.href),['#/sessions/s1','#/sessions/s2']);
 // Without catalog names the repository id is the honest fallback; the plan title falls back to a neutral label.
 assert.equal(last.items[0].meta,'gh:1 · attempt 1 · needs your input');assert.equal(last.items[0].attention,true);assert.equal(last.items[1].meta,'gh:1 · attempt 1 · completed');assert.equal(last.items[0].title,'Saved plan');ui.close();
});

test('finished session content deletion confirms scope and renders durable deletion date', async()=>{
 let purged=false, confirmations=[];
 const b=setup((m,u)=>{
  if(m==='POST' && u.endsWith('/purge')) {purged=true;return {session_id:'s',purged_at:1700000000000};}
  if(u==='/api/sessions/s')return session('s',{state:'completed',purged,purged_at:purged?1700000000000:null,source_available:!purged});
  return basic(m,u);
 });
 b.window.confirm=text=>{confirmations.push(text);return true;};
 const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:requestFor(b),pollMs:1e6});ui.open();await settle();
 const action=button(ui.panel,'Delete session content'); assert.ok(action);action.focus();assert.equal(b.document.activeElement,action);
 action.click();await settle();
 assert.equal(confirmations.length,1);assert.match(confirmations[0],/PR on GitHub is not affected/);
 assert.match(confirmations[0],/validation output/);assert.match(visibleText(ui.panel),/Content deleted on/);
 assert.equal(button(ui.panel,'Delete session content'),undefined);
 assertPurgedView(ui.panel);
 assert.deepEqual(JSON.parse(b.requests.find(r=>r.url.endsWith('/purge')).body),{});
 ui.close();
});

function assertPurgedView(panel) {
 const text=visibleText(panel);
 assert.equal((text.match(/Content deleted on/g)||[]).length,1);
 assert.match(visibleText(panel.querySelector('.session-header')),/Content deleted on/);
 assert.doesNotMatch(text,/Retry|Create draft PR|Stop session|Delete session content/);
 assert.match(text,/Start a new session from the saved plan/);
 assert.equal(panel.querySelector('a[href="#/plans/p/v"]').textContent,'saved plan');
}

test('opening a purged session hides content actions and links its retained saved version',async()=>{
 const b=setup((m,u)=>{
  if(u==='/api/sessions/s')return {id:'s',state:'failed',plan_id:'p',plan_version_id:'v',purged:true,purged_at:1700000000000,repositories:[]};
  return basic(m,u);
 });
 const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:requestFor(b),pollMs:1e6});ui.open();await settle();
 assertPurgedView(ui.panel);await ui.refresh();assertPurgedView(ui.panel);
 assert.equal(b.requests.some(r=>r.url.startsWith('/api/plans')),false);
 assert.equal(b.requests.some(r=>/\/(retry|publications)$/.test(r.url)&&r.method==='POST'),false);
 ui.close();
});

test('purged session without a plan id links to Ask without scanning plans',async()=>{
 const b=setup((m,u)=>u==='/api/sessions/s'?{id:'s',state:'failed',plan_version_id:'v',purged:true,purged_at:1700000000000,repositories:[]}:basic(m,u));
 const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:requestFor(b),pollMs:1e6});ui.open();await settle();
 assert.equal(ui.panel.querySelector('a[href="#/ask"]').textContent,'saved plans in Ask');
 assert.equal(b.requests.some(r=>r.url.startsWith('/api/plans')),false);
 ui.close();
});

test('session list displays retained deletion date',async()=>{
 const b=setup((m,u)=>u.startsWith('/api/sessions?')?{items:[session('s',{state:'completed',purged:true,purged_at:1700000000000})],next_cursor:null}:basic(m,u),'#/sessions');
 const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:requestFor(b),pollMs:1e6});ui.open();await settle();
 assert.match(visibleText(ui.panel),/Content deleted on/);ui.close();
});
