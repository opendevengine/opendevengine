import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createBrowser, install, settle, visibleText } from './dom.mjs';
import { createSettingsPage } from '../../opendevengine/web/settings.js';

test('System health explains durable index, role destinations and execution without raw records', async () => {
  const b = createBrowser('<!doctype html><html><body><main id=host></main></body></html>', {respond: () => ({status:404, body:{}})});
  install(b);
  const host = b.document.getElementById('host');
  const page = createSettingsPage({host, sections:{}, request: async () => ({status:'degraded', components:{
    index:{persistent:true,repository_count:7,counts:{ready:3,indexing:1,failed:2,not_indexed:1},latest_failure_code:'upstream_unavailable'},
    model:{roles:{ask:{status:'ready',adapter:'openai_compatible',destination:'remote'},planning:{status:'not_configured'}}},
    execution:{status:'available',runtime_status:'running',docker_code:'not_checked',agent_code:'not_checked',active_session_count:2},
    storage:{status:'ok'},jobs:{status:'ok',workers_running:1},secrets:{status:'available'},
  },private_source:'/private/SECRET'})});
  await page.show('health'); await settle();
  const text = visibleText(host);
  for (const expected of ['Saved index: 7 repositories','Ready: 3; indexing: 1; failed: 2; not indexed: 1','Latest index failure: upstream unavailable','Answers: ready, openai compatible, remote destination','Planning: not configured','Docker: not checked','Coding agent: not checked','Active sessions: 2','Background workers: 1']) assert.ok(text.includes(expected), expected);
  assert.doesNotMatch(text,/SECRET|private|rebuilt at startup/);
});

test('System health keeps legacy-only builds readable and offers refresh', async () => {
  const b = createBrowser('<!doctype html><html><body><main id=host></main></body></html>', {respond: () => ({status:404, body:{}})});
  install(b);
  let calls = 0;
  const host = b.document.getElementById('host');
  const page = createSettingsPage({host,sections:{},request:async () => {
    calls += 1;
    return {status:'ok',components:{index:{persistent:false,repository_count:4},model:{status:'not_configured'}}};
  }});
  await page.show('health'); await settle();
  assert.match(visibleText(host),/Index: 4 repositories, rebuilt at startup/);
  assert.match(visibleText(host),/Answers: not configured/);
  assert.match(visibleText(host),/Execution is not configured/);
  host.querySelectorAll('button').find(x => x.textContent === 'Refresh health').click();
  await settle(); assert.equal(calls, 2);
});

test('repeated health failures replace the message instead of stacking it', async () => {
  const b = createBrowser('<!doctype html><html><body><main id=host></main></body></html>', {respond: () => ({status:404, body:{}})});
  install(b);
  const host = b.document.getElementById('host');
  const page = createSettingsPage({host,sections:{},request:async () => { throw new Error('offline'); }});
  await page.show('health'); await settle();
  for (let i = 0; i < 2; i++) { host.querySelectorAll('button').find(x => x.textContent === 'Refresh health').click(); await settle(); }
  assert.equal(visibleText(host).match(/Health status is unavailable/g).length, 1);
  assert.equal(host.querySelectorAll('button').filter(x => x.textContent === 'Refresh health').length, 1);
});
