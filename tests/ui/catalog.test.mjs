import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createBrowser, install, settle, visibleText } from './dom.mjs';
const html = readFileSync(new URL('../../opendevengine/web/index.html', import.meta.url), 'utf8');
let sequence = 0;
const repo = {id:'gh:1',full_name:'team/widget',description:'Widget source',topics:['widget'],language:'Python',visibility:'private',access_state:'accessible',index_enabled:false,implementation_enabled:false,default_branch:'main'};
const version = {id:'v1',version:1,adapter:'ollama',endpoint:'http://127.0.0.1:11434',model:'test-model',credential_configured:true,config:{capabilities:['chat','structured_output'],timeout_seconds:60,context_limit:8192,max_output_tokens:1024,allow_external_source:false}};
const profile = {id:'p1',name:'Local test',active_version_id:'v1',version};
async function boot(custom = () => undefined, hash = '#/settings') {
 const browser = createBrowser(html, {hash, respond(method,url,options) {
  const override = custom(method,url,options); if (override !== undefined) return override;
  if (url === '/api/repos') return {repos:[],model:null};
  if (url === '/api/github/connections') return {connections:[{id:'c1',slug:'test-app',app_id:'1',status:'configured'}],summary:{status:'configured',installations:0,repository_count:1}};
  if (url === '/api/provider-profiles') return {profiles:[profile]};
  if (url === '/api/settings/model-roles') return {settings:{local_only:true},roles:{}};
  if (url.startsWith('/api/repositories?')) return {items:[repo],total:1,next_cursor:null};
  return {status:404,body:{code:'not_found'}};
 }});
 install(browser); await import(new URL(`../../opendevengine/web/app.js?catalog=${++sequence}`,import.meta.url)); await settle();
 return {...browser, text:()=>visibleText(browser.document.body)};
}
const button = (root,text) => root.querySelectorAll('button').find(x=>x.textContent===text);
// Profile actions show short text (Edit, Test, Delete); their accessible names say which profile.
const labelled = (root,label) => root.querySelectorAll('button').find(x=>x.getAttribute('aria-label')===label);
// The stand-in selector intentionally has no comma selector support.
function input(root,label) { const wrap=root.querySelectorAll('label').find(x=>x.textContent.startsWith(label)); return wrap?.querySelector('input') || wrap?.querySelector('select') || wrap?.querySelector('textarea'); }
function formFor(root,text) { return button(root,text).closest('form'); }


test('App setup clears multiline PEM immediately, creates safe connect link and exposes refresh/disconnect',async()=>{
 const app=await boot((method,url)=>{
  if(method==='POST'&&url==='/api/github/configure') return {id:'c1'};
  if(url==='/api/github/connect')return {install_url:'https://github.com/apps/test-app/installations/new?state=bounded',expires_at:1900000000000};
  if(url==='/api/github/refresh')return {state:'queued'};
  if(url==='/api/github/disconnect')return {status:'disconnected'};
 });
 const root=app.document.getElementById('settings-content');
 const form=formFor(root,'Save App configuration');input(form,'App ID').value='123';input(form,'App slug').value='test-app';
 const key=input(form,'RSA private key PEM (never redisplayed)');key.value='PRIVATE\nKEY';form.requestSubmit();assert.equal(key.value,'');await settle();
 const call=app.requests.find(x=>x.url==='/api/github/configure');assert.deepEqual(JSON.parse(call.body),{app_id:'123',slug:'test-app',private_key:'PRIVATE\nKEY',index_new:false});
 assert.doesNotMatch(visibleText(root),/PRIVATE\nKEY/);
 button(root,'Create connect link').click();await settle();assert.match(visibleText(root),/Continue installation on GitHub/);
 button(root,'Refresh catalog').click();await settle();assert.match(visibleText(root),/Discovery queued/);
 button(root,'Disconnect').click();await settle();assert.match(visibleText(root),/Disconnected/);
});

test('profile creation and credential rotation clear secrets and send exact capabilities',async()=>{
 const app=await boot((method,url)=>method==='POST'||method==='PATCH'?{}:undefined,'#/settings/models');
 const root=app.document.getElementById('settings-content');
 const form=formFor(root,'Create profile');input(form,'Profile name').value='Created';input(form,'Model identifier').value='model';
 const secret=input(form,'Credential (optional for local Ollama)');secret.value='never-render-this';input(form,'Structured output').checked=true;
 form.requestSubmit();assert.equal(secret.value,'');await settle();
 const created=app.requests.find(x=>x.method==='POST'&&x.url==='/api/provider-profiles');const body=JSON.parse(created.body);
 assert.deepEqual(body.config.capabilities,['chat','structured_output']);assert.equal(body.secret_value,'never-render-this');assert.doesNotMatch(visibleText(root),/never-render-this/);
 labelled(root,'Edit Local test').click();
 const edit=formFor(root,'Save new profile version');const rotate=input(edit,'New credential (leave blank to retain current credential)');rotate.value='replacement';edit.requestSubmit();assert.equal(rotate.value,'');await settle();
 assert.equal(JSON.parse(app.requests.find(x=>x.method==='PATCH'&&x.url==='/api/provider-profiles/p1').body).secret_value,'replacement');
});

test('failed secret submission is sanitized, cleared and never automatically retried',async()=>{
 const app=await boot((method,url)=> method==='POST'&&url==='/api/provider-profiles'?{status:503,body:{code:'secret_store_unavailable',error:'reflected-api-key'}}:undefined,'#/settings/models');
 const root=app.document.getElementById('settings-content');const form=formFor(root,'Create profile');
 input(form,'Profile name').value='Test';input(form,'Model identifier').value='model';const secret=input(form,'Credential (optional for local Ollama)');secret.value='reflected-api-key';form.requestSubmit();await settle();
 assert.equal(secret.value,'');assert.match(visibleText(root),/secret_store_unavailable/);assert.doesNotMatch(visibleText(root),/reflected-api-key/);
 assert.equal(app.requests.filter(x=>x.method==='POST').length,1);
});

test('provider test renders capability failures and delete requires explicit confirmation',async()=>{
 const app=await boot((method,url)=>{
  if(url.endsWith('/test'))return {status:'partial',capabilities:{chat:{status:'verified'},structured_output:{status:'failed',error_code:'invalid_output'}},destination:{kind:'local',host:'127.0.0.1:11434'}};
  if(method==='DELETE')return {deleted:true};
 },'#/settings/models');
 const root=app.document.getElementById('settings-content');labelled(root,'Test Local test').click();await settle();
 assert.match(visibleText(root),/Chat verified/);assert.match(visibleText(root),/Structured output failed \(invalid_output\)/);
 labelled(root,'Delete Local test').click();assert.equal(app.requests.filter(x=>x.method==='DELETE').length,0);
 labelled(root,'Cancel deletion').click();assert.equal(app.requests.filter(x=>x.method==='DELETE').length,0);
 labelled(root,'Delete Local test').click();labelled(root,'Confirm delete Local test').click();await settle();assert.equal(app.requests.filter(x=>x.method==='DELETE').length,1);
});

test('callback keeps Settings route; unavailable APIs do not expose mutation forms',async()=>{
 const app=await boot((_method,url)=>url.startsWith('/api/')&&url!=='/api/repos'?{status:404,body:{code:'not_found'}}:undefined,'#/settings?github=pending_approval');
 assert.equal(app.document.getElementById('view-settings').hidden,false);
 const root=app.document.getElementById('settings-content');assert.match(visibleText(root),/Endpoint unavailable/);assert.equal(root.querySelectorAll('form').length,0);
});

test('embeddings preserve exact dimensions and secret-free saves omit credential rotation',async()=>{
 const app=await boot((method)=>method==='POST'?{}:undefined,'#/settings/models');
 const root=app.document.getElementById('settings-content');const form=formFor(root,'Create profile');
 input(form,'Profile name').value='Embeddings';input(form,'Model identifier').value='embedding-model';
 input(form,'Chat').checked=false;input(form,'Embeddings').checked=true;input(form,'Embedding dimensions (required for embeddings)').value='768';
 form.requestSubmit();await settle();const saved=JSON.parse(app.requests.find(x=>x.method==='POST').body);
 assert.deepEqual(saved.config.capabilities,['embeddings']);assert.equal(saved.config.embedding_dimensions,768);assert.equal('secret_value' in saved,false);
});



test('GitHub settings renders with browser-like getter-only textarea.type', async () => {
 const app = await boot();
 const textarea = app.document.createElement('textarea');
 assert.equal(textarea.type, 'textarea');
 assert.throws(() => { textarea.type = 'pem'; }, TypeError);
 const ordinaryInput = app.document.createElement('input');
 ordinaryInput.type = 'password';
 assert.equal(ordinaryInput.type, 'password');
 const root = app.document.getElementById('settings-content');
 assert.doesNotMatch(visibleText(root), /GitHub unavailable/);
 const form = formFor(root, 'Save App configuration');
 assert.ok(form, 'App configuration form must finish construction');
 const key = input(form, 'RSA private key PEM (never redisplayed)');
 assert.equal(key.tagName, 'TEXTAREA');
 assert.equal(key.type, 'textarea');
 assert.equal(key.hasAttribute('type'), false);
 assert.equal(input(form, 'App ID').type, 'number');
});


function delayed() { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; }
test('newest provider load wins when older response arrives last', async () => {
 let delayReads = false; const reads = [];
 const app = await boot((method, url) => {
  if (delayReads && method === 'GET' && ['/api/provider-profiles', '/api/settings/model-roles'].includes(url)) {
   const gate = delayed(); reads.push({url, ...gate}); return gate.promise;
  }
 },'#/settings/models');
 const root = app.document.getElementById('settings-content');
 delayReads = true;
 button(root, 'Reload provider settings').click(); await settle();
 button(root, 'Reload provider settings').click(); await settle();
 assert.equal(reads.length, 4);
 const respond = (entries, name, localOnly) => entries.forEach(entry => entry.resolve(entry.url === '/api/provider-profiles'
  ? {profiles: [{...profile, name}]} : {settings:{local_only:localOnly}, roles:{}}));
 respond(reads.slice(2), 'Newest profile', true); await settle();
 respond(reads.slice(0, 2), 'Stale profile', false); await settle();
 assert.ok(labelled(root, 'Edit Newest profile'));
 assert.equal(Boolean(labelled(root, 'Edit Stale profile')), false);
 assert.equal(root.querySelectorAll('.profile-list').length, 1);
});

test('provider mutation prevents reloads from rendering pre-mutation settings', async () => {
 const mutation = delayed(); let pending = false; let readsWhilePending = 0;
 const app = await boot((method, url) => {
  if (method === 'POST' && url === '/api/provider-profiles') { pending = true; return mutation.promise; }
  if (pending && method === 'GET' && ['/api/provider-profiles', '/api/settings/model-roles'].includes(url)) readsWhilePending++;
 },'#/settings/models');
 const root = app.document.getElementById('settings-content');
 const form = formFor(root, 'Create profile'); input(form, 'Profile name').value = 'Pending'; input(form, 'Model identifier').value = 'model'; form.requestSubmit(); await settle();
 button(root, 'Reload provider settings').click(); await settle();
 assert.equal(readsWhilePending, 0, 'reads must wait for the pending mutation to settle');
 pending = false; mutation.resolve({}); await settle();
 assert.equal(root.querySelectorAll('.profile-list').length, 1);
 assert.equal(root.querySelectorAll('button').filter(x => x.textContent === 'Create profile').length, 1);
});


test('newest GitHub load wins and callback guidance survives reloads', async () => {
 let delay = false; const reads = [];
 const app = await boot((method,url) => {
  if (delay && method==='GET' && url==='/api/github/connections') {
   const gate=delayed(); reads.push(gate); return gate.promise;
  }
 }, '#/settings?github=setup_failed');
 const root=app.document.getElementById('settings-content');
 assert.match(visibleText(root),/GitHub setup returned/);
 delay=true;
 button(root,'Reload GitHub status').click(); await settle();
 button(root,'Reload GitHub status').click(); await settle();
 assert.equal(reads.length,2);
 const response=slug=>({connections:[{id:'c1',slug,app_id:'1',status:'configured'}],summary:{status:'configured',installations:0,repository_count:1}});
 reads[1].resolve(response('newest-app'));await settle();
 reads[0].resolve(response('obsolete-app'));await settle();
 assert.match(visibleText(root),/newest-app/);assert.doesNotMatch(visibleText(root),/obsolete-app/);
 assert.equal(root.querySelectorAll('button').filter(x=>x.textContent==='Save App configuration').length,1);
 assert.equal(root.querySelectorAll('p').filter(x=>x.textContent.startsWith('GitHub setup returned')).length,1);
});

test('GitHub configure blocks stale reloads until its outcome settles', async () => {
 const gate=delayed();let pending=false;let reads=0;
 const app=await boot((method,url)=>{
  if(method==='POST'&&url==='/api/github/configure'){pending=true;return gate.promise;}
  if(pending&&method==='GET'&&url==='/api/github/connections')reads++;
 });
 const root=app.document.getElementById('settings-content');const form=formFor(root,'Save App configuration');
 input(form,'App ID').value='1';input(form,'App slug').value='test';input(form,'RSA private key').value='synthetic';
 form.requestSubmit();await settle();button(root,'Reload GitHub status').click();await settle();
 assert.equal(reads,0);assert.match(app.document.getElementById('app-status').textContent,/update is pending/);
 pending=false;gate.resolve({id:'c1'});await settle();
 assert.equal(root.querySelectorAll('button').filter(x=>x.textContent==='Save App configuration').length,1);
});


test('invalid GitHub installation URLs are sanitized and saved settings retain keyboard focus', async () => {
 const app=await boot((method,url)=>{
  if(url==='/api/github/connect')return {install_url:'https://[private-callback-state',expires_at:1};
  if(method==='POST'&&url==='/api/provider-profiles')return {};
 });
 const root=app.document.getElementById('settings-content');button(root,'Create connect link').click();await settle();
 assert.match(visibleText(root),/Invalid installation destination/);assert.doesNotMatch(visibleText(root),/private-callback-state/);
 app.window.location.hash='#/settings/models';await settle();
 const form=formFor(root,'Create profile');input(form,'Profile name').value='Name';input(form,'Model identifier').value='model';
 button(form,'Create profile').focus();form.requestSubmit();await settle();
 assert.equal(app.document.activeElement.tagName,'H2');
 assert.equal(app.document.activeElement.textContent,'Provider profiles');
});

test('advanced profiles do not repeat the job pickers or the Privacy switch', async () => {
 const app = await boot(undefined, '#/settings/models');
 const root = app.document.getElementById('settings-content');
 for (const text of ['Save destination policy', 'Save ask role', 'Save embedding role']) assert.equal(button(root, text), undefined, text);
 assert.equal(root.querySelectorAll('.profile-row').length, 1);
 assert.match(visibleText(root), /Ollama · test-model · 127\.0\.0\.1:11434/);
 assert.match(visibleText(root), /Chat, Structured output · Credential saved · Never receives repository source/);
});

test('the profile form asks for embedding dimensions only with Embeddings, and its URL hint follows the adapter', async () => {
 const app = await boot(undefined, '#/settings/models');
 const root = app.document.getElementById('settings-content'); const form = formFor(root, 'Create profile');
 const dimensions = input(form, 'Embedding dimensions');
 assert.equal(dimensions.parentNode.hidden, true);
 const embeddings = input(form, 'Embeddings'); embeddings.checked = true; embeddings.dispatchEvent({type: 'change', bubbles: true});
 assert.equal(dimensions.parentNode.hidden, false);
 const hint = form.querySelector('.field-hint');
 assert.match(hint.textContent, /^HTTP only to this computer or the --ollama-url machine; HTTPS anywhere else/);
 const adapter = input(form, 'Adapter'); adapter.value = 'claude_cli'; adapter.dispatchEvent({type: 'change', bubbles: true});
 assert.match(hint.textContent, /api\.anthropic\.com/);
 adapter.value = 'openai_compatible'; adapter.dispatchEvent({type: 'change', bubbles: true});
 assert.match(hint.textContent, /^Use the \/v1 base\./);
});

test('a hidden embedding-dimensions value never blocks saving a chat-only profile', async () => {
 const app = await boot(undefined, '#/settings/models');
 const form = formFor(app.document.getElementById('settings-content'), 'Create profile');
 const dimensions = input(form, 'Embedding dimensions'); const embeddings = input(form, 'Embeddings');
 assert.equal(dimensions.disabled, true);
 embeddings.checked = true; embeddings.dispatchEvent({type: 'change', bubbles: true});
 assert.equal(dimensions.disabled, false); dimensions.value = '0';
 embeddings.checked = false; embeddings.dispatchEvent({type: 'change', bubbles: true});
 assert.equal(dimensions.disabled, true, 'disabled controls are skipped by browser validation');
});
