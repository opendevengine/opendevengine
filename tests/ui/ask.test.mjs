import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {mkdtempSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createBrowser,install,input,settle,visibleText} from './dom.mjs';
import {createAskPanel} from '../../opendevengine/web/ask.js';
const html=readFileSync(new URL('../../opendevengine/web/index.html',import.meta.url),'utf8');
const bridge=fileURLToPath(new URL('./ask-service-bridge.py',import.meta.url));

test('real AskAPI plan edit, immutable history, exact source, export and conflicts survive process restarts',async()=>{
 const dir=mkdtempSync(join(tmpdir(),'ask-ui-'));
 const call=(path,method='GET',body)=>{const u=new URL(path,'http://localhost');return JSON.parse(execFileSync(process.env.OPENDEVENGINE_TEST_PYTHON || 'python3',[bridge,dir],{input:JSON.stringify({path:u.pathname,query:u.search.slice(1),method,body}),encoding:'utf8'}));};
 try {
  const seed=call('/test/seed','POST').body;
  const b=createBrowser(html,{hash:`#/plans/${seed.plan.id}/${seed.plan.version.id}`,respond:(m,u,o)=>call(u,m,o.body?JSON.parse(o.body):undefined)});install(b);
  b.window.confirm=()=>true;
  await import(new URL('../../opendevengine/web/app.js?ask-real-services',import.meta.url));await settle();
  const panel=b.document.querySelector('.persistent-ask');assert.ok(panel);assert.ok(b.document.getElementById('question'));assert.equal(b.document.getElementById('thread').parentNode.hidden,true);
  // The plan editor lives in the side panel; the Ask page keeps only a summary.
  const side=b.document.getElementById('side-panel');
  const button=text=>[...panel.querySelectorAll('button'),...side.querySelectorAll('button')].find(x=>x.textContent===text);
  const field=label=>[...panel.querySelectorAll('label'),...side.querySelectorAll('label')].find(x=>x.textContent.startsWith(label)).querySelector('textarea');
  const planTab=()=>side.querySelectorAll('[role="tab"]').find(x=>x.textContent===`Edit · ${seed.plan.title}`)?.click();
  // The main pane shows the saved plan as a document; editing opens on request.
  assert.match(visibleText(panel),/What you asked/);assert.doesNotMatch(visibleText(panel),/SHA256/);
  button('Edit plan').click();await settle();
  assert.equal(side.hidden,false);
  assert.ok(side.querySelectorAll('summary').find(x=>x.textContent==='Plan metadata'));
  assert.equal(side.querySelectorAll('summary').find(x=>x.textContent==='Version details'),undefined);
  assert.match(visibleText(side),/implementation_not_permitted/);
  assert.equal(button('Start session').disabled,true);
  side.querySelectorAll('button').find(x=>x.textContent.includes('worker.py:1')).click();await settle();
  assert.match(visibleText(side),/def deliver/);assert.equal(side.hidden,false);
  planTab();await settle();
  input(field('goal'),'Human edited goal');input(field('requirements'),'Preserve mail\nBound retries');
  button('Add step').click();await settle();
  input(field('2. title'),'Observe retries');input(field('2. description'),'Record attempt count');
  button('Save immutable version').click();await settle(30);
  const latest=call(`/api/plans/${seed.plan.id}`).body;
  assert.equal(latest.version.version,2);assert.equal(latest.version.content.goal,'Human edited goal');assert.equal(latest.version.content.steps.length,2);
  assert.deepEqual(latest.version.content.requirements,['Preserve mail','Bound retries']);
  assert.equal(call(`/api/plans/${seed.plan.id}?version_id=${seed.plan.version.id}`).body.version.content.goal,'Bound delivery retries');
  assert.equal(call(`/api/plans/${seed.plan.id}/versions`,'POST',{content:latest.version.content,parent_version_id:seed.plan.version.id}).status,409);
  assert.match(visibleText(panel),/Human edited goal/);button('Edit plan').click();await settle();
  button('Export this saved version').click();await settle();assert.match(visibleText(side),new RegExp(latest.version.content_hash));assert.match(visibleText(side),/Human edited goal/);
  const old=side.querySelectorAll('a').find(x=>x.textContent==='Previous version');old.click();await settle(30);button('Edit plan').click();await settle();assert.equal(field('goal').value,'Bound delivery retries');
  b.window.location.hash=`#/ask/${seed.conversation.id}`;await settle(30);assert.match(visibleText(panel),/Durable UI test/);assert.ok(panel.querySelector('.conversation-title').classList.contains('sr-only'));
  input(field('Question or planning request'),'How does mail delivery work?');button('Ask').click();await settle(30);
  const c=call(`/api/conversations/${seed.conversation.id}`).body;assert.equal(c.messages.filter(x=>x.role==='user').length,1);assert.equal(c.runs.length,1);
  button('Cancel').click();await settle(30);assert.equal(call(`/api/conversations/${seed.conversation.id}`).body.runs[0].state,'cancelled');
  button('Retry').click();await settle(30);assert.equal(call(`/api/conversations/${seed.conversation.id}`).body.messages.filter(x=>x.role==='user').length,1);
  button('Cancel').click();await settle(30);
 } finally {rmSync(dir,{recursive:true,force:true});}
});

test('missing persistent API preserves the legacy DOM and demo',async()=>{
 const b=createBrowser(html,{respond:()=>({status:404,body:{code:'not_found'}})});install(b);
 assert.equal(await createAskPanel({host:b.document.getElementById('view-ask')}),null);
 assert.ok(b.document.getElementById('question'));assert.equal(b.document.getElementById('ask-form').hidden,false);
});

test('uncertain message submission reuses client ID and survives navigation refusal',async()=>{
 const c={id:'c',title:'Saved',scope_revision:1,repository_ids:['r'],messages:[],runs:[],plans:[]};const submitted=[];
 const b=createBrowser(html,{hash:'#/ask/c',respond:(m,u,o)=>{
  if(u==='/api/conversations')return {items:[c],next_cursor:null};
  if(u==='/api/conversations/c')return c;
  if(u==='/api/conversations/c/messages'){submitted.push(JSON.parse(o.body));if(submitted.length===1)throw new Error('Connection lost');return {id:'r',state:'queued'};}
  return {status:404,body:{}};
 }});install(b);b.window.confirm=()=>false;
 const ui=await createAskPanel({host:b.document.getElementById('view-ask')});
 const button=t=>ui.panel.querySelectorAll('button').find(b=>b.textContent===t);
 const prompt=ui.panel.querySelectorAll('textarea').at(-1);input(prompt,'Preserve this request');
 b.window.location.hash='#/settings';await settle();assert.equal(b.window.location.hash,'#/ask/c');assert.equal(prompt.value,'Preserve this request');
 button('Ask').click();await settle();assert.match(visibleText(ui.panel),/Connection lost/);
 button('Ask').click();await settle();assert.equal(submitted.length,2);assert.equal(submitted[0].client_request_id,submitted[1].client_request_id);assert.equal(submitted[0].intent,'answer');
});

test('source and conversation reads fence late responses; citations render untrusted text literally',async()=>{
 const c={id:'c',title:'Saved',scope_revision:1,repository_ids:[],runs:[],plans:[],messages:[{role:'assistant',state:'complete',content:'<img src=x onerror=alert(1)>',citations:[{repository_id:'r',snapshot_id:'s',revision:'a',path:'one',start_line:1,end_line:1},{repository_id:'r',snapshot_id:'s',revision:'a',path:'two',start_line:1,end_line:1}]}]};let first;
 const b=createBrowser(html,{hash:'#/ask/c',respond:(m,u)=>{
  if(u==='/api/conversations')return {items:[c]};if(u==='/api/conversations/c')return c;
  if(u.includes('path=one'))return new Promise(resolve=>{first=resolve;});
  if(u.includes('path=two'))return {repository_id:'r',revision:'a',path:'two',first_line:1,lines:['second']};
  return {status:404,body:{}};
 }});install(b);
 const ui=await createAskPanel({host:b.document.getElementById('view-ask')});
 assert.equal(ui.panel.querySelector('img'),null);
 const cites=ui.panel.querySelectorAll('button').filter(x=>x.classList.contains('cite'));assert.equal(cites.length,2);cites[0].click();await settle();cites[1].click();await settle();
 first({repository_id:'r',revision:'a',path:'one',first_line:1,lines:['late first']});await settle();
 assert.match(visibleText(ui.panel),/second/);assert.doesNotMatch(visibleText(ui.panel),/late first/);
});

test('ready launch uses reserved version/hash/idempotency body and only server session ID',async()=>{
 const content={goal:'Goal',user_request:'Request',repositories:[],citations:[],findings:[],requirements:['R'],steps:[],validation:[],risks:[],assumptions:[],open_questions:[],execution_context:{execution_profile_id:'execution-1',base_choices:{}},publication:{policy:'manual_draft'}};
 const p={id:'p',title:'Plan',latest_version_id:'v',version:{id:'v',version:1,content_hash:'hash',content,readiness:{state:'ready',blocking_reasons:[],warnings:[],source_freshness:{}}}};
 const submitted=[]; const b=createBrowser(html,{hash:'#/plans/p/v',respond:(m,u,o)=>{
  if(u==='/api/conversations')return {items:[]};if(u.startsWith('/api/plans/p'))return p;
  if(u==='/api/sessions'){submitted.push(JSON.parse(o.body));if(submitted.length===1)throw new Error('Timeout after submit');return {session_id:'server-session',state:'queued'};}
  return {status:404,body:{}};
 }});install(b);const ui=await createAskPanel({host:b.document.getElementById('view-ask')});
 const start=ui.panel.querySelectorAll('button').find(x=>x.textContent==='Start session');start.click();await settle();start.click();await settle();
 assert.equal(b.window.location.hash,'#/sessions/server-session');assert.deepEqual(submitted[0],submitted[1]);assert.deepEqual(Object.keys(submitted[0]).sort(),['content_hash','idempotency_key','plan_version_id']);assert.equal(submitted[0].plan_version_id,'v');
});

test('any visible plan edit blocks Start until saved or explicitly discarded',async()=>{
 const content={goal:'Goal',user_request:'Request',repositories:[],citations:[],findings:[],requirements:['R'],steps:[{title:'Old step',description:'D',paths:[],citation_ids:[]}],validation:[],risks:[],assumptions:[],open_questions:[],execution_context:{execution_profile_id:null,base_choices:{}},publication:{policy:'manual_draft'}};
 const p={id:'p',title:'Plan',latest_version_id:'v',version:{id:'v',version:1,content_hash:'hash',content,readiness:{state:'ready',blocking_reasons:[],warnings:[],source_freshness:{}}}};
 const submitted=[];const b=createBrowser(html,{hash:'#/plans/p/v',respond:(m,u,o)=>{
  if(u==='/api/conversations')return {items:[]};if(u.startsWith('/api/plans/p'))return p;
  if(u==='/api/sessions'){submitted.push(JSON.parse(o.body));return {session_id:'s',state:'queued'};}
  return {status:404,body:{}};
 }});install(b);b.window.confirm=()=>true;const ui=await createAskPanel({host:b.document.getElementById('view-ask')});await settle();
 const start=()=>ui.panel.querySelectorAll('button').find(x=>x.textContent==='Start session');
 assert.equal(start().disabled,false);
 // A step row field previously mutated the plan without marking it dirty.
 input(ui.panel.querySelectorAll('label').find(x=>x.textContent.startsWith('1. title')).querySelector('textarea'),'Edited step');
 assert.equal(start().disabled,true);assert.match(visibleText(ui.panel),/Unsaved plan edits/);
 start().disabled=false;start().click();await settle();assert.equal(submitted.length,0);
 ui.panel.querySelectorAll('button').find(x=>x.textContent==='Discard plan edits').click();await settle(20);
 assert.equal(ui.panel.querySelectorAll('label').find(x=>x.textContent.startsWith('1. title')).querySelector('textarea').value,'Old step');
 assert.equal(start().disabled,false);start().click();await settle();assert.equal(submitted.length,1);
});
