import {test} from 'node:test';
import {webcrypto} from 'node:crypto';
globalThis.crypto ||= webcrypto;
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createBrowser,install,settle,visibleText} from './dom.mjs';
import {createAskPanel} from '../../opendevengine/web/ask.js';
import {renderBaseAdvanced} from '../../opendevengine/web/plan-view.js';

for (const action of ['Start on the saved base','Regenerate plan']) {
  test(`conversation freshness notice focuses and handles ${action}`,async()=>{
    const c={id:'c',repository_ids:[],messages:[],runs:[],plans:[{id:'p',latest_version_id:'v'}]};
    const posts=[], generations=[];
    const b=createBrowser(readFileSync(new URL('../../opendevengine/web/index.html',import.meta.url),'utf8'),{hash:'#/ask/c',respond:(method,url,options)=>{
      if(url==='/api/conversations')return {items:[c]};
      if(url==='/api/conversations/c/messages' && method==='POST'){generations.push(JSON.parse(options.body));return {run_id:'r'};}
      if(url==='/api/conversations/c')return c;
      if(url.startsWith('/api/plans/p'))return {version:{id:'v',version:1,content_hash:'hash',content:{steps:[],repositories:[],user_request:'Fix add'},readiness:{state:'ready'}}};
      if(url==='/api/sessions'){
        const data=JSON.parse(options.body);posts.push(data);
        if(!data.acknowledged_base)return {status:409,body:{code:'base_advanced',details:[{repository_id:'gh:1',pinned_revision:'c'.repeat(40),current_head:'d'.repeat(40)}]}};
        return {session_id:'s'};
      }
      return {status:404,body:{code:'not_found'}};
    }});
    install(b);
    const ui=await createAskPanel({host:b.document.getElementById('view-ask')});await settle();
    ui.panel.querySelectorAll('button').find(n=>n.textContent==='Start session').click();await settle();
    const notice=ui.panel.querySelector('.base-advanced-notice');assert.ok(notice);
    assert.match(visibleText(notice),/gh:1 moved from ccccccc to ddddddd since this plan was made\./);
    assert.equal(b.document.activeElement,notice);
    assert.equal(posts.length,1,'no silent retry');
    notice.querySelectorAll('button').find(n=>n.textContent===action).click();await settle();
    if(action==='Start on the saved base'){
      assert.equal(posts.length,2);assert.equal(posts[1].idempotency_key,posts[0].idempotency_key);
      assert.deepEqual(posts[1].acknowledged_base,{'gh:1':'c'.repeat(40)});
      assert.equal(b.window.location.hash,'#/sessions/s');
    }else{
      assert.equal(posts.length,1);assert.equal(generations.length,1);
      assert.equal(generations[0].intent,'plan');assert.equal(generations[0].content,'Fix add');
    }
  });
}

test('shared plan notice uses safe repository text and exposes both keyboard buttons',async()=>{
  const b=createBrowser('<html><body><div id="host"></div></body></html>',{respond:()=>({})});install(b);
  const host=b.document.getElementById('host');let retained, regenerated=false;
  const notice=renderBaseAdvanced(host,[{repository_id:'r',pinned_revision:'a'.repeat(40),current_head:'b'.repeat(40)}],{nameFor:()=>'<script>repo</script>',onRetain:ack=>{retained=ack;},onRegenerate:()=>{regenerated=true;}});
  assert.equal(b.document.activeElement,notice);assert.equal(notice.querySelectorAll('script').length,0);
  const buttons=notice.querySelectorAll('button');assert.equal(buttons.length,2);
  buttons[0].click();await settle();assert.deepEqual(retained,{r:'a'.repeat(40)});
  buttons[1].click();await settle();assert.equal(regenerated,true);
});

for(const action of ['Start on the saved base','Regenerate plan'])test(`saved plan Start handles ${action}`,async()=>{
  const content={goal:'Goal',user_request:'Request',repositories:[],citations:[],findings:[],requirements:['R'],steps:[],validation:[],risks:[],assumptions:[],open_questions:[],execution_context:{execution_profile_id:null,base_choices:{}},publication:{policy:'manual_draft'}};
  const plan={id:'p',conversation_id:'c',title:'Plan',latest_version_id:'v',version:{id:'v',version:1,content_hash:'hash',content,readiness:{state:'ready',blocking_reasons:[],warnings:[],source_freshness:{}}}};
  const posts=[],generations=[];
  const b=createBrowser(readFileSync(new URL('../../opendevengine/web/index.html',import.meta.url),'utf8'),{hash:'#/plans/p/v',respond:(method,url,options)=>{
    if(url==='/api/conversations')return {items:[]};
    if(url.startsWith('/api/plans/p'))return plan;
    if(url==='/api/conversations/c')return {id:'c',repository_ids:[],messages:[],runs:[],plans:[]};
    if(url==='/api/conversations/c/messages'){generations.push(JSON.parse(options.body));return {run_id:'r'};}
    if(url==='/api/sessions'){
      const data=JSON.parse(options.body);posts.push(data);
      if(!data.acknowledged_base)return {status:409,body:{code:'base_advanced',details:[{repository_id:'r',pinned_revision:'a'.repeat(40),current_head:'b'.repeat(40)}]}};
      return {session_id:'s'};
    }
    return {status:404,body:{}};
  }});install(b);
  const ui=await createAskPanel({host:b.document.getElementById('view-ask')});await settle();
  ui.panel.querySelectorAll('button').find(n=>n.textContent==='Start session').click();await settle();
  const notice=ui.panel.querySelector('.base-advanced-notice');assert.ok(notice);assert.equal(b.document.activeElement,notice);assert.equal(posts.length,1);
  notice.querySelectorAll('button').find(n=>n.textContent===action).click();await settle();
  if(action==='Start on the saved base'){
    assert.equal(posts.length,2);assert.equal(posts[0].idempotency_key,posts[1].idempotency_key);assert.deepEqual(posts[1].acknowledged_base,{r:'a'.repeat(40)});assert.equal(b.window.location.hash,'#/sessions/s');
  }else {assert.equal(posts.length,1);assert.equal(generations.length,1);assert.equal(generations[0].intent,'plan');}
});

test('session detail surfaces the persisted older-base acknowledgement',async()=>{
  const {createSessionPanel}=await import('../../opendevengine/web/sessions.js');
  const b=createBrowser(readFileSync(new URL('../../opendevengine/web/index.html',import.meta.url),'utf8'),{hash:'#/sessions/s',respond:()=>({})});install(b);
  const request=async path=>{
    if(path.includes('/events?'))return {items:[],next_seq:0,has_more:false};
    if(path.includes('/messages?'))return {items:[],next_cursor:null};
    if(path.endsWith('/diff'))return {available:false,reason:'no_checkpoint'};
    if(path.endsWith('/validations'))return {items:[],fully_validated:false};
    if(path==='/api/sessions/s')return {id:'s',state:'queued',phase:'queued',plan_id:'p',attempt:1,source_available:true,execution:{},limits:{},repositories:[{repository_id:'r',role:'write',base_sha:'a'.repeat(40),base_branch:'main'}],instructions:{acknowledged_base:{r:'a'.repeat(40)}},runner:{},capabilities:{},usage:{},outcome:{},publication:{},latest_seq:0};
    return {items:[]};
  };
  const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request,pollMs:1e6});
  try {ui.open();await settle();assert.match(visibleText(ui.panel),/Started on an older base \(acknowledged\)/);}finally{ui.close();}
});

for (const action of ['Retry on the saved base', 'Open plan']) test(`retry after advance offers ${action}`, async()=>{
  const {createSessionPanel,executionRequest}=await import('../../opendevengine/web/sessions.js');
  const posts=[];
  const b=createBrowser(readFileSync(new URL('../../opendevengine/web/index.html',import.meta.url),'utf8'),{hash:'#/sessions/s',respond:(method,path,options)=>{
    if(path==='/api/sessions/s/retry') {
      const data=JSON.parse(options.body); posts.push(data);
      if(!data.acknowledged_base)return {status:409,body:{code:'base_advanced',details:[{repository_id:'r',pinned_revision:'a'.repeat(40),current_head:'b'.repeat(40)}]}};
      return {session_id:'successor'};
    }
    if(path.includes('/events?'))return {items:[],next_seq:0,has_more:false};
    if(path.includes('/messages?'))return {items:[],next_cursor:null};
    if(path.endsWith('/diff'))return {available:false,reason:'no_checkpoint'};
    if(path.endsWith('/validations'))return {items:[],fully_validated:false};
    if(path==='/api/sessions/s')return {id:'s',state:'failed',phase:'agent',plan_id:'p',attempt:1,source_available:true,execution:{},limits:{},repositories:[{repository_id:'r',role:'write',base_sha:'a'.repeat(40),base_branch:'main'}],instructions:{},runner:{},capabilities:{},usage:{},outcome:{},publication:{},latest_seq:0};
    return {items:[]};
  }});install(b);b.document.getElementById('view-sessions').hidden=false;
  const ui=createSessionPanel({host:b.document.getElementById('sessions-content'),request:executionRequest,pollMs:1e6});
  try {
    ui.open();await settle();
    ui.panel.querySelectorAll('button').find(n=>n.textContent==='Retry' && !n.disabled).click();await settle();
    const notice=ui.panel.querySelector('.base-advanced-notice'); assert.ok(notice, JSON.stringify(posts)+' '+visibleText(ui.panel));assert.equal(b.document.activeElement,notice);
    assert.match(visibleText(notice),/aaaaaaa to bbbbbbb/);assert.equal(posts.length,1,'refusal does not silently retry');
    await ui.refresh();assert.equal(ui.panel.querySelector('.base-advanced-notice'),notice,'polling preserves the pending decision');
    const controls=notice.querySelectorAll('button');assert.ok(controls.every(n=>n.classList.contains('btn')));
    controls.find(n=>n.textContent===action).click();await settle();
    if(action==='Retry on the saved base') {
      assert.equal(posts.length,2);assert.equal(posts[0].idempotency_key,posts[1].idempotency_key);
      assert.deepEqual(posts[1].acknowledged_base,{r:'a'.repeat(40)});assert.equal(b.window.location.hash,'#/sessions/successor');
    } else {assert.equal(posts.length,1);assert.equal(b.window.location.hash,'#/plans/p');}
  } finally {ui.close();}
});
