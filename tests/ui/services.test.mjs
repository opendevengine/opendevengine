import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {mkdtempSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createBrowser,install,settle,visibleText} from './dom.mjs';
test('UI profile CRUD, roles and local policy persist through real FeatureAPI and SQLite (no sockets)',async()=>{
 const directory=mkdtempSync(join(tmpdir(),'catalog-ui-'));
 try {
  const bridge=fileURLToPath(new URL('./service-bridge.py',import.meta.url));
  const browser=createBrowser(readFileSync(new URL('../../opendevengine/web/index.html',import.meta.url),'utf8'),{hash:'#/settings',respond(method,url,options){
   if(url==='/api/repos')return {repos:[],model:null};
   const parsed=new URL(url,'http://localhost');
   return JSON.parse(execFileSync(process.env.OPENDEVENGINE_TEST_PYTHON||'python3',[bridge,directory],{input:JSON.stringify({method,path:parsed.pathname,query:parsed.search.slice(1),body:options.body?JSON.parse(options.body):undefined}),encoding:'utf8'}));
  }});
  install(browser);await import(new URL('../../opendevengine/web/app.js?real-services',import.meta.url));await settle();
  const root=browser.document.getElementById('settings-content');
  const button=text=>root.querySelectorAll('button').find(x=>x.textContent===text);
  const input=(form,label)=>{const wrap=form.querySelectorAll('label').find(x=>x.textContent.startsWith(label));return wrap.querySelector('input')||wrap.querySelector('select');};
  assert.match(visibleText(root),/disconnected · 0 active installations/);
  browser.window.location.hash='#/settings/models';await settle();
  let form=button('Create profile').closest('form');input(form,'Profile name').value='Real local profile';input(form,'Model identifier').value='synthetic';form.requestSubmit();await settle();
  assert.match(visibleText(root),/Real local profile/);
  const api=(method,path,body)=>JSON.parse(execFileSync(process.env.OPENDEVENGINE_TEST_PYTHON||'python3',[bridge,directory],{input:JSON.stringify({method,path,query:'',body}),encoding:'utf8'})).body;
  const labelled=label=>root.querySelectorAll('button').find(x=>x.getAttribute('aria-label')===label);
  // Jobs are assigned with the pickers at the top of Models; the new profile is listed there without a reload.
  const ask=root.querySelectorAll('select').find(s=>s.getAttribute('aria-label')==='Answering questions');
  ask.value=ask.querySelectorAll('option').find(o=>o.textContent.startsWith('Real local profile')).value;ask.dispatchEvent({type:'change',bubbles:true});await settle();
  const assigned=api('GET','/api/settings/model-roles').roles.ask;
  assert.equal(assigned.status,'untested');assert.equal(assigned.destination.host,'127.0.0.1:11434');
  labelled('Edit Real local profile').click();form=button('Save new profile version').closest('form');input(form,'Model identifier').value='synthetic-v2';form.requestSubmit();await settle();
  assert.match(visibleText(root),/synthetic-v2/);assert.match(visibleText(root),/Version 2/);
  // Local-only lives in Privacy only.
  browser.window.location.hash='#/settings/privacy';await settle();
  root.querySelector('[role="switch"]').click();await settle();
  assert.equal(api('GET','/api/settings/model-roles').settings.local_only,false);
  browser.window.location.hash='#/settings/models';await settle();
  labelled('Delete Real local profile').click();labelled('Confirm delete Real local profile').click();await settle();assert.equal(labelled('Delete Real local profile'),undefined);
 } finally {rmSync(directory,{recursive:true,force:true});}
});
