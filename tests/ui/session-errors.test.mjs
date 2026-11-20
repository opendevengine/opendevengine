import {test} from 'node:test';
import {webcrypto} from 'node:crypto';
globalThis.crypto ||= webcrypto;
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createBrowser,install,settle,visibleText} from './dom.mjs';

test('a stale Retry that finds a newer attempt explains it and links the next attempt', async()=>{
  const {createSessionPanel,executionRequest}=await import('../../opendevengine/web/sessions.js');
  let successor=null; const posts=[];
  const session=()=>({id:'s',state:'failed',phase:'agent',plan_id:'p',attempt:1,successor_id:successor,source_available:true,execution:{},limits:{},repositories:[{repository_id:'r',role:'write',base_sha:'a'.repeat(40),base_branch:'main'}],instructions:{},runner:{},capabilities:{},usage:{},outcome:{},publication:{},latest_seq:0});
  const b=createBrowser(readFileSync(new URL('../../opendevengine/web/index.html',import.meta.url),'utf8'),{hash:'#/sessions/s',respond:(method,path,options)=>{
    if(path==='/api/sessions/s/retry'){posts.push(JSON.parse(options.body));successor='s2';return {status:409,body:{code:'retry_exists'}};}
    if(path.includes('/events?'))return {items:[],next_seq:0,has_more:false};
    if(path.includes('/messages?'))return {items:[],next_cursor:null};
    if(path.endsWith('/diff'))return {available:false,reason:'no_checkpoint'};
    if(path.endsWith('/validations'))return {items:[],fully_validated:false};
    if(path==='/api/sessions/s')return session();
    return {items:[]};
  }});install(b);b.document.getElementById('view-sessions').hidden=false;
  const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:executionRequest,pollMs:1e6});
  try {
    ui.open();await settle();
    ui.panel.querySelectorAll('button').find(n=>n.textContent==='Retry' && !n.disabled).click();await settle(20);
    const text=visibleText(ui.panel);
    assert.equal(posts.length,1);
    assert.match(text,/A newer attempt of this session already exists; open it to continue\./);
    assert.doesNotMatch(text,/retry exists/,'no raw error code');
    assert.ok(ui.panel.querySelectorAll('a').find(a=>a.getAttribute('href')==='#/sessions/s2'),'the next attempt is linked');
  } finally {ui.close();}
});
