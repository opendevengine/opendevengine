import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createInterface} from 'node:readline';
import {readFileSync} from 'node:fs';
import {createBrowser,install,input,settle,visibleText} from './dom.mjs';
import {createAskPanel} from '../../opendevengine/web/ask.js';
import {createSessionPanel,createExecutionSettings} from '../../opendevengine/web/sessions.js';
const html=readFileSync(new URL('../../opendevengine/web/index.html',import.meta.url),'utf8');
function bridge(){
 const child=spawn(process.env.OPENDEVENGINE_TEST_PYTHON||'python3',[new URL('./session-service-bridge.py',import.meta.url).pathname],{stdio:['pipe','pipe','pipe']});const pending=[];let error='';
 child.stderr.on('data',x=>{error=(error+x).slice(-4000);});
 createInterface({input:child.stdout}).on('line',line=>{const item=pending.shift();if(item){try{item.resolve(JSON.parse(line));}catch(e){item.reject(e);}}});
 child.on('exit',code=>{for(const item of pending.splice(0))item.reject(new Error(`Bridge exited ${code}: ${error}`));});
 return {call(path,method='GET',body){return new Promise((resolve,reject)=>{const u=new URL(path,'http://localhost');pending.push({resolve,reject});child.stdin.write(JSON.stringify({path:u.pathname,query:u.search.slice(1),method,body})+'\n');});},async close(){if(child.exitCode!==null){assert.equal(child.exitCode,0, error);return;}const finished=new Promise(resolve=>child.once('exit',resolve));child.stdin.end();await finished;assert.equal(error,'');}};
}
const button=(root,text)=>[...root.querySelectorAll('button')].find(n=>n.textContent===text);
const field=(root,text)=>[...root.querySelectorAll('label')].find(n=>n.textContent.startsWith(text))?.querySelector('input, textarea, select');
const composer=root=>field(root,'Send guidance');
const wait=async predicate=>{for(let i=0;i<300;i++){if(predicate())return;await settle(2);}assert.ok(predicate(),'UI did not settle');};

test('real FeatureAPI/profile/plan/session services: immutable launch, durable message replay, diff/checks, retry and access redaction',async()=>{
 const api=bridge();let sessionsUI,executionUI;
 try{
  const seeded=await api.call('/test/seed','POST');assert.equal(seeded.status,200);const {plan,profile}=seeded.body;
  assert.equal(plan.version.readiness.state,'ready');assert.equal(profile.check.status,'passed');
  const b=createBrowser(html,{hash:`#/plans/${plan.id}/${plan.version.id}`,respond:(m,u,o)=>api.call(u,m,o.body?JSON.parse(o.body):undefined)});install(b);b.window.confirm=()=>true;
  b.document.getElementById('view-ask').hidden=false;b.document.getElementById('view-sessions').hidden=false;
  const ask=await createAskPanel({host:b.document.getElementById('view-ask')});await settle();
  assert.match(visibleText(ask.panel),new RegExp(profile.active_version_id));assert.equal(button(ask.panel,'Start session').disabled,false);
  // Saving a publication choice is a manual immutable edit, never model authority.
  const policy=field(ask.panel,'Publication policy for');policy.value='on_success_draft';policy.dispatchEvent({type:'change',bubbles:true});button(ask.panel,'Save immutable version').click();
  await wait(()=>b.window.location.hash!==`#/plans/${plan.id}/${plan.version.id}`);await wait(()=>!!button(ask.panel,'Start session'));await settle();
  const saved=(await api.call(`/api/plans/${plan.id}`)).body;assert.equal(saved.version.version,2);assert.equal(saved.version.content.publication.policy,'on_success_draft');assert.equal((await api.call(`/api/plans/${plan.id}?version_id=${plan.version.id}`)).body.version.content.publication.policy,'manual_draft');
  button(ask.panel,'Start session').click();await wait(()=>b.window.location.hash.startsWith('#/sessions/'));const sid=b.window.location.hash.slice(11);
  const request=async(path,method='GET',body)=>{const result=await api.call(path,method,body);if(result.status>=400){const e=new Error(result.body.code);e.status=result.status;e.code=result.body.code;throw e;}return result.body;};
  sessionsUI=createSessionPanel({host:b.document.getElementById('sessions-content'),request,pollMs:1e6});sessionsUI.open();await wait(()=>!button(sessionsUI.panel,'Send').disabled);
  input(composer(sessionsUI.panel),'Please preserve the public function signature');button(sessionsUI.panel,'Send').click();await wait(()=>visibleText(sessionsUI.panel).includes('Guidance · queued'));
  const messages=await api.call(`/api/sessions/${sid}/messages?limit=50`);assert.equal(messages.body.items.length,1);assert.equal(messages.body.items[0].content,'Please preserve the public function signature');
  await api.call('/test/run','POST');await sessionsUI.refresh();await settle();
  assert.match(visibleText(sessionsUI.panel),/completed/);assert.match(visibleText(sessionsUI.panel),/0.01 USD \(reported\)/);assert.match(visibleText(sessionsUI.panel),/scripted-fixture/);
  button(sessionsUI.panel,'Diff').click();assert.match(visibleText(sessionsUI.panel),/return a \+ b/);
  button(sessionsUI.panel,'Checks').click();assert.match(visibleText(sessionsUI.panel),/All recorded checks passed/);
  const firstEvents=[...sessionsUI.panel.querySelectorAll('[data-seq]')].map(n=>n.dataset.seq);assert.equal(firstEvents.length,new Set(firstEvents).size);await sessionsUI.refresh();assert.equal(sessionsUI.panel.querySelectorAll('[data-seq]').length,firstEvents.length);
  // A fresh UI instance replays from the durable API without launching work.
  sessionsUI.close();sessionsUI.panel.remove();sessionsUI=createSessionPanel({host:b.document.getElementById('sessions-content'),request,pollMs:1e6});sessionsUI.open();await wait(()=>!!button(sessionsUI.panel,'Checks'));button(sessionsUI.panel,'Checks').click();await wait(()=>visibleText(sessionsUI.panel).includes('All recorded checks passed'));assert.equal(sessionsUI.panel.querySelectorAll('[data-seq]').length,firstEvents.length);
  const first=await api.call(`/api/sessions/${sid}/retry`,'POST',{idempotency_key:'real-ui-successor'});const again=await api.call(`/api/sessions/${sid}/retry`,'POST',{idempotency_key:'real-ui-successor'});assert.equal(first.body.session_id,again.body.session_id);assert.notEqual(first.body.session_id,sid);
  await api.call('/test/revoke','POST');await sessionsUI.refresh();await settle();button(sessionsUI.panel,'Diff').click();assert.doesNotMatch(visibleText(sessionsUI.panel),/return a \+ b|synthetic validation passed|Synthetic integration fixture edited/);assert.match(visibleText(sessionsUI.panel),/redacted|Source unavailable|source unavailable/);
  assert.equal((await api.call(`/api/sessions/${sid}`)).body.source_available,false);
  await api.call('/test/restore','POST');
  executionUI=createExecutionSettings({request,pollMs:1e6});b.document.body.append(executionUI.element);await executionUI.refresh();assert.match(visibleText(executionUI.element),/Synthetic coding test: passed/);
 }finally{sessionsUI?.close();executionUI?.close();await api.close();}
});

test('real execution-profile API creates immutable limits, enforces local-only tests and preserves deletion history',async()=>{
 const api=bridge();let ui;
 try{
  await api.call('/test/seed','POST');
  const b=createBrowser(html,{hash:'#/settings',respond:(m,u,o)=>api.call(u,m,o.body?JSON.parse(o.body):undefined)});install(b);b.document.getElementById('view-settings').hidden=false;
  const request=async(path,method='GET',body)=>{const result=await api.call(path,method,body);if(result.status>=400){const e=new Error(result.body.code);e.status=result.status;e.code=result.body.code;throw e;}return result.body;};
  ui=createExecutionSettings({request,pollMs:1e6});b.document.body.append(ui.element);await ui.refresh();
  input(field(ui.element,'Execution profile name'),'Limited coding');input(field(ui.element,'Locally provisioned'),'python:3.12-slim');input(field(ui.element,'Maximum agent turns'),'7');input(field(ui.element,'Claude model'),'sonnet');button(ui.element,'Create execution profile').click();await wait(()=>visibleText(ui.element).includes('Execution profile version saved'));
  const created=(await api.call('/api/execution-profiles?limit=100')).body.items.find(p=>p.name==='Limited coding');assert.equal(created.version.config.agent_limits.max_turns,7);assert.equal(created.version.config.agent_model,'sonnet');assert.equal(created.version.config.allow_external_source,false);assert.equal(created.check,null);
  await api.call('/api/settings/model-roles','PATCH',{local_only:true});await ui.refresh();button(ui.element,'Test execution profile Limited coding').click();await wait(()=>visibleText(ui.element).includes('execution_local_only'));
  assert.equal((await api.call(`/api/execution-profiles/${created.id}`)).body.check,null);
  await api.call('/api/settings/model-roles','PATCH',{local_only:false});await ui.refresh();button(ui.element,'Test execution profile Limited coding').click();await wait(()=>visibleText(ui.element).includes('Synthetic coding test queued'));
  assert.equal((await api.call(`/api/execution-profile-versions/${created.active_version_id}`)).body.check.status,'queued');
  button(ui.element,'Edit execution profile Limited coding').click();input(field(ui.element,'Maximum agent turns'),'6');button(ui.element,'Save execution profile version').click();await wait(()=>visibleText(ui.element).includes('Version 2'));
  const latest=(await api.call(`/api/execution-profiles/${created.id}`)).body;assert.equal(latest.version.config.agent_limits.max_turns,6);assert.equal((await api.call(`/api/execution-profile-versions/${created.active_version_id}`)).body.config.agent_limits.max_turns,7);
  button(ui.element,'Delete execution profile Limited coding').click();button(ui.element,'Confirm delete execution profile Limited coding').click();await wait(()=>visibleText(ui.element).includes('Execution profile deleted'));
  assert.equal((await api.call('/api/execution-profiles?limit=100')).body.items.some(p=>p.id===created.id),false);assert.equal((await api.call(`/api/execution-profile-versions/${created.active_version_id}`)).body.profile_deleted,true);
 }finally{ui?.close();await api.close();}
});
