import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createBrowser,install,settle,visibleText} from './dom.mjs';
import {createContextPanel} from '../../opendevengine/web/context.js';
const initial = {repository_id:'demo:one',name:'Example',kind:'demo',eligible:true,status:'ready',
 active_snapshot:{id:'s1',revision:'immutable-old',mode:'hybrid',coverage:{files:2,chunks:3,partial:false,skipped:{sensitive:1}}},active_run:null};
const roles = {roles:{embedding:{version_id:'v1',profile_name:'Local embeddings',destination:{kind:'local',host:'127.0.0.1:11434'}}}};
function deferred(){let resolve;return {promise:new Promise(done=>{resolve=done;}),resolve:value=>resolve(value)};}
async function boot(custom=()=>undefined){
 let repo=structuredClone(initial);const calls=[];
 const browser=createBrowser('<!doctype html><html><body><main></main></body></html>',{respond(){}});install(browser);
 const request=async(path,method='GET',body)=>{
  calls.push({path,method,body:structuredClone(body)});
  const override=custom({path,method,body,repo,setRepo:value=>{repo=value;}});
  if(override!==undefined)return override;
  if(path.startsWith('/api/context/repositories?'))return {items:[repo],next_cursor:null};
  if(path==='/api/settings/model-roles')return roles;
  if(path.endsWith('/exclusions')&&method==='GET')return {exclusions:['vendor/**']};
  return {};
 };
 const panel=createContextPanel({request,isVisible:()=>true});browser.document.body.append(panel.element);await panel.refresh();
 return {...browser,panel,calls};
}
const button=(root,label)=>root.querySelectorAll('button').find(b=>b.textContent===label);

test('context exposes real coverage and pins the displayed embedding version for indexing',async()=>{
 const app=await boot(({path,method,repo,setRepo})=>{
  if(method==='POST'&&path.endsWith('/index')){
   setRepo({...repo,status:'indexing',active_run:{id:'run1',state:'queued',phase:'queued',progress:{}}});return {id:'run1',state:'queued'};
  }
 });
 const root=app.panel.element;assert.match(visibleText(root),/immutable-old.*hybrid/);assert.match(visibleText(root),/sensitive: 1/);
 assert.match(visibleText(root),/Embedding destination: local/);
 button(root,'Reindex Example').click();await settle();
 const call=app.calls.find(c=>c.method==='POST');assert.deepEqual(call.body,{embedding_version_id:'v1'});
 assert.equal(call.path,'/api/repositories/demo%3Aone/index');assert.match(visibleText(root),/Indexing queued/);
 assert.equal(button(root,'Reindex Example').disabled,true);assert.ok(button(root,'Pause Example'));
});

test('pause and resume use the exact durable run without starting another index',async()=>{
 const running={...initial,active_run:{id:'run-one',state:'running',phase:'embedding',progress:{files:2,chunks:3}}};
 const app=await boot(({path,method,repo,setRepo})=>{
  if(path.startsWith('/api/context/repositories?'))return {items:[repo.active_run?repo:running],next_cursor:null};
  if(method==='POST'&&path.includes('/api/index-jobs/')){
   const state=path.endsWith('/pause')?'paused':'queued';setRepo({...running,active_run:{...running.active_run,state}});return {id:'run-one',state};
  }
 });
 button(app.panel.element,'Pause Example').click();await settle();assert.ok(button(app.panel.element,'Resume Example'));
 button(app.panel.element,'Resume Example').click();await settle();
 assert.deepEqual(app.calls.filter(c=>c.method==='POST').map(c=>c.path),['/api/index-jobs/run-one/pause','/api/index-jobs/run-one/resume']);
});

test('exclusion drafts survive refresh attempts and save only explicit relative patterns',async()=>{
 const app=await boot();const root=app.panel.element;
 button(root,'Edit exclusions for Example').click();await settle();
 const input=root.querySelector('textarea');assert.equal(input.value,'vendor/**');
 input.value='vendor/**\ninternal/private.py\n';input.dispatchEvent({type:'input'});
 const before=app.calls.filter(c=>c.path.startsWith('/api/context/repositories')).length;
 await app.panel.refresh();assert.equal(app.calls.filter(c=>c.path.startsWith('/api/context/repositories')).length,before);
 assert.match(visibleText(root),/Unsaved exclusions/);assert.equal(input.value,'vendor/**\ninternal/private.py\n');
 input.closest('form').requestSubmit();await settle();
 const call=app.calls.find(c=>c.method==='PATCH');assert.equal(call.path,'/api/repositories/demo%3Aone/exclusions');
 assert.deepEqual(call.body,{exclusions:['vendor/**','internal/private.py']});
});

test('purge requires an explicit second action and renders the returned excluded state',async()=>{
 const app=await boot(({path,method,repo,setRepo})=>{
  if(method==='POST'&&path.endsWith('/purge')){setRepo({...repo,eligible:false,status:'excluded',active_snapshot:null});return {state:'completed'};}
 });const root=app.panel.element;
 button(root,'Purge context for Example').click();assert.equal(app.calls.filter(c=>c.method==='POST').length,0);
 button(root,'Confirm purge for Example').click();await settle();
 assert.equal(app.calls.filter(c=>c.method==='POST').length,1);assert.match(visibleText(root),/Retrieval excluded/);
 assert.doesNotMatch(visibleText(root),/immutable-old/);assert.equal(button(root,'Index Example').disabled,true);
});

test('stale context reads cannot append duplicate actionable rows',async()=>{
 let hold=false;const reads=[];
 const app=await boot(({path})=>{
  if(hold&&path.startsWith('/api/context/repositories?')){const gate=deferred();reads.push(gate);return gate.promise;}
 });hold=true;
 const first=app.panel.refresh();const second=app.panel.refresh();
 reads[1].resolve({items:[{...initial,name:'Newest'}],next_cursor:null});await second;
 reads[0].resolve({items:[{...initial,name:'Old'}],next_cursor:null});await first;
 assert.match(visibleText(app.panel.element),/Newest/);assert.doesNotMatch(visibleText(app.panel.element),/Old/);
 assert.equal(app.panel.element.querySelectorAll('h2').length,1);
 assert.equal(app.panel.element.querySelectorAll('button').filter(b=>b.textContent==='Reindex Newest').length,1);
});

test('malformed context cannot enable actions from a truthy permission string',async()=>{
 const app=await boot(({path})=>path.startsWith('/api/context/repositories?')?{items:[{...initial,eligible:'true'}]}:undefined);
 assert.match(visibleText(app.panel.element),/Unsupported context response/);
 assert.equal(Boolean(button(app.panel.element,'Reindex Example')),false);
});

test('pending exclusion save freezes controls and preserves newer programmatic edits',async()=>{
 const gate=deferred();
 const app=await boot(({path,method})=>method==='PATCH'&&path.endsWith('/exclusions')?gate.promise:undefined);
 const root=app.panel.element;
 button(root,'Edit exclusions for Example').click();await settle();
 const input=root.querySelector('textarea');
 input.value='first/**';input.dispatchEvent({type:'input'});
 input.closest('form').requestSubmit();await settle();
 assert.equal(input.disabled,true);
 assert.equal(button(root,'Discard local exclusion edits').disabled,true);
 assert.deepEqual(app.calls.find(c=>c.method==='PATCH').body,{exclusions:['first/**']});
 // Even a programmatic edit must not be mistaken for the submitted draft.
 input.value='newer/**';input.dispatchEvent({type:'input'});
 gate.resolve({});await settle();
 assert.equal(root.querySelector('textarea'),input);
 assert.equal(input.value,'newer/**');assert.equal(input.disabled,false);
 assert.match(visibleText(root),/Unsaved exclusions/);
});

test('failed exclusion save restores editing and retains submitted draft',async()=>{
 const gate=deferred();
 const app=await boot(({path,method})=>method==='PATCH'&&path.endsWith('/exclusions')?gate.promise:undefined);
 const root=app.panel.element;button(root,'Edit exclusions for Example').click();await settle();
 const input=root.querySelector('textarea');input.value='keep/**';input.dispatchEvent({type:'input'});
 input.closest('form').requestSubmit();await settle();gate.resolve(Promise.reject(new Error('Save failed')));await settle();
 assert.equal(input.disabled,false);assert.equal(input.value,'keep/**');
 assert.equal(button(root,'Discard local exclusion edits').disabled,false);
 assert.match(visibleText(root),/Save failed/);
});

test('repositoryId narrows the panel to one repository read from its own context endpoint',async()=>{
 const calls=[];const browser=createBrowser('<!doctype html><html><body></body></html>',{respond(){}});install(browser);
 const request=async(path)=>{calls.push(path);if(path==='/api/repositories/demo%3Aone/context')return structuredClone(initial);if(path==='/api/settings/model-roles')return roles;return {};};
 const panel=createContextPanel({request,isVisible:()=>true,repositoryId:'demo:one'});browser.document.body.append(panel.element);await panel.refresh();
 assert.ok(calls.includes('/api/repositories/demo%3Aone/context'));assert.equal(calls.some(p=>p.startsWith('/api/context/repositories')),false);
 assert.match(visibleText(panel.element),/Example/);assert.ok(button(panel.element,'Reindex Example'));
 assert.equal(button(panel.element,'Next context page'),undefined);
});
