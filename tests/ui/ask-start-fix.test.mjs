import {test} from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {mkdtempSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createBrowser,install,settle,visibleText} from './dom.mjs';
import {createAskPanel} from '../../opendevengine/web/ask.js';

test('conversation fix opens the saved plan editor at Execution and publication', async()=>{
  const dir=mkdtempSync(join(tmpdir(),'ask-start-fix-'));
  const bridge=fileURLToPath(new URL('./ask-service-bridge.py',import.meta.url));
  const call=(path,method='GET',body)=>{
    const url=new URL(path,'http://localhost');
    return JSON.parse(execFileSync(process.env.OPENDEVENGINE_TEST_PYTHON || 'python3',[bridge,dir],{input:JSON.stringify({path:url.pathname,query:url.search.slice(1),method,body}),encoding:'utf8'}));
  };
  try {
    const seed=call('/test/seed','POST').body;
    const b=createBrowser(readFileSync(new URL('../../opendevengine/web/index.html',import.meta.url),'utf8'),{hash:`#/ask/${seed.conversation.id}`,respond:(m,u,o)=>call(u,m,o.body?JSON.parse(o.body):undefined)});
    install(b); b.window.confirm=()=>true;
    const editorHost=b.document.createElement('aside');b.document.body.append(editorHost);
    const sidePanel={close(){},openTab({render}){render(editorHost);}};
    const ui=await createAskPanel({host:b.document.getElementById('view-ask'),sidePanel});
    await settle();
    assert.match(visibleText(ui.panel.querySelector('.plan-card-reason')),/Choose a tested execution profile/);
    ui.panel.querySelector('.plan-card-reason a').click();await settle(30);
    const section=editorHost.querySelectorAll('details').find(n=>n.querySelector('summary')?.textContent==='Execution and publication');
    assert.ok(section);assert.equal(section.open,true);
    assert.match(b.window.location.hash,new RegExp(`^#/plans/${seed.plan.id}/`));
    assert.equal(call(`/api/plans/${seed.plan.id}`).body.version.id,seed.plan.version.id);
  } finally {rmSync(dir,{recursive:true,force:true});}
});

for (const outcome of ['success', 'rejection', 'network error']) {
  test(`conversation launch key is ${outcome === 'network error' ? 'reused' : 'regenerated'} after ${outcome}`, async()=>{
    const c={id:'launch-c',repository_ids:[],messages:[],runs:[],plans:[{id:'launch-p',latest_version_id:'launch-v'}]};
    const posts=[], stored=new Map();
    const b=createBrowser(readFileSync(new URL('../../opendevengine/web/index.html',import.meta.url),'utf8'),{
      hash:'#/ask/launch-c',respond:(method,url,options)=>{
        if(url==='/api/conversations')return {items:[c]};
        if(url==='/api/conversations/launch-c')return c;
        if(url.startsWith('/api/plans/launch-p'))return {version:{id:'launch-v',content_hash:'saved-hash',version:1,content:{steps:[]},readiness:{state:'ready'}}};
        if(url==='/api/sessions'){
          posts.push(JSON.parse(options.body));
          if(posts.length===1 && outcome==='rejection')return {status:409,body:{code:'launch_rejected'}};
          if(posts.length===1 && outcome==='network error')throw new Error('Network connection lost');
          return {session_id:`session-${posts.length}`};
        }
        return {status:404,body:{code:'not_found'}};
      }
    });
    b.window.sessionStorage={getItem:k=>stored.get(k)??null,setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)};
    install(b);
    const ui=await createAskPanel({host:b.document.getElementById('view-ask')});await settle();
    const start=()=>ui.panel.querySelectorAll('button').find(n=>n.textContent==='Start session');
    start().click();await settle();
    assert.equal(posts.length,1);
    const first=posts[0].idempotency_key;assert.ok(first);
    assert.equal(stored.get('ask-launch-launch-v')??null,outcome==='network error'?first:null);
    if(outcome==='success'){
      assert.equal(b.window.location.hash,'#/sessions/session-1');
      b.window.location.hash='#/ask/launch-c';await settle();
    } else assert.match(visibleText(ui.panel),/Couldn't start session/);
    start().click();await settle();
    assert.equal(posts.length,2);
    if(outcome==='network error')assert.equal(posts[1].idempotency_key,first);
    else assert.notEqual(posts[1].idempotency_key,first);
    assert.equal(b.window.location.hash,'#/sessions/session-2');
    assert.equal(stored.has('ask-launch-launch-v'),false);
  });
}
