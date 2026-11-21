import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createBrowser, install, settle, input, visibleText} from './dom.mjs';
import {createSessionPanel, createExecutionSettings} from '../../opendevengine/web/sessions.js';
const html = readFileSync(new URL('../../opendevengine/web/index.html', import.meta.url), 'utf8');
const H1 = '1'.repeat(40), H2 = '2'.repeat(40);
const session = (changes = {}) => ({id:'s',plan_id:'p',plan_version_id:'v',content_hash:'a'.repeat(64),attempt:1,state:'completed',phase:'done',source_available:true,execution:{},limits:{},repositories:[{repository_id:'gh:1',role:'write',base_sha:'b'.repeat(40),base_branch:'main',session_branch:'session/s'}],runner:{containment:'confirmed'},capabilities:{},usage:{},outcome:{},publication:{policy:'manual_draft'},created_at:Date.now()-5000,latest_seq:0,pending_question:null,...changes});
const preview = (changes = {}) => ({session_id:'s',repository_id:'gh:1',repository:'octo/demo',base_sha:'b'.repeat(40),base_branch:'main',head_branch:'session/s',diff_hash:H1,stats:{files:2,additions:5,deletions:1},checks:{fully_validated:true,summary:{passed:2}},policy:'manual_draft',title:'OpenDevEngine session s',body:'## Summary\n\nChanges',intent_version:1,changed_paths:['a.py','b.py'],base_observation:{sha:null,diverged:null},blocking_reasons:[],publishable:true,...changes});
const publication = (changes = {}) => ({id:'pub1',session_id:'s',repository_id:'gh:1',intent_version:1,policy:'manual_draft',expected_diff_hash:H1,expected_base_sha:'b'.repeat(40),base_branch:'main',head_branch:'opendevengine/s-abc',state:'queued',remote_base_sha:null,commit_sha:null,head_sha:null,pull_request_id:null,pull_request_number:null,pull_request_url:null,pull_request_state:null,error_code:null,created_at:1,updated_at:1,finished_at:null,...changes});
const notFound = {status:404, body:{error:'Not found.', code:'not_found'}};
const conflict = code => ({status:409, body:{error:'The operation could not complete.', code}});
const button = (root, text) => [...root.querySelectorAll('button')].find(n => n.textContent === text);
const field = (root, text) => [...root.querySelectorAll('label')].find(n => n.textContent.startsWith(text))?.querySelector('input, textarea, select');
function requestFor(b) { return async (path, method = 'GET', body, {signal} = {}) => { const r = await b.window.fetch(path, {method, ...(signal ? {signal} : {}), ...(body === undefined ? {} : {body: JSON.stringify(body)})}); const v = await r.json(); if (!r.ok) { const e = new Error(v.code || 'request failed'); e.status = r.status; e.code = v.code; throw e; } return v; }; }
// `routes(method, url, options)` answers publication calls; session reads fall back to a terminal session.
function setup(routes, {hash = '#/sessions/s', state = session()} = {}) {
  const b = createBrowser(html, {hash, respond: (m, u, o) => {
    const answer = routes(m, u, o); if (answer !== undefined) return answer;
    if (u.includes('/events?')) return {items:[], next_seq:0, has_more:false};
    if (u.includes('/messages?')) return {items:[], next_cursor:null};
    if (u.endsWith('/diff')) return {available:false, reason:'no_checkpoint'};
    if (u.endsWith('/validations')) return {items:[], fully_validated:false};
    if (u.startsWith('/api/plans/')) return {};
    if (u.startsWith('/api/sessions?')) return {items:[], next_cursor:null};
    if (u === '/api/sessions/s') return typeof state === 'function' ? state() : state;
    return {status:404, body:{error:'Page not found.'}};
  }});
  install(b); b.document.getElementById('view-sessions').hidden = false; b.document.getElementById('view-settings').hidden = false;
  const data = new Map(); b.window.sessionStorage = {getItem:k => data.get(k), setItem:(k, v) => data.set(k, v), removeItem:k => data.delete(k)};
  return b;
}
async function open(b, pollMs = 1e6) {
  const ui = createSessionPanel({host: b.document.getElementById('sessions-content'), request: requestFor(b), pollMs}); ui.open(); await settle();
  const tab = ui.panel.querySelectorAll('[role="tab"]').find(t => t.textContent === 'PR'); tab.click(); tab.focus();
  const area = b.document.getElementById(tab.getAttribute('aria-controls'));
  return {ui, area};
}
async function until(check, rounds = 200) { for (let i = 0; i < rounds; i++) { if (check()) return; await settle(2); await new Promise(r => setTimeout(r, 2)); } assert.fail('condition not reached'); }
const posts = (b, suffix) => b.requests.filter(r => r.method === 'POST' && r.url.endsWith(suffix)).map(r => JSON.parse(r.body));

test('checks skipped because the plan did not approve them are not reported as failing', async () => {
  const b = setup((m, u) => {
    if (u === '/api/sessions/s/publication') return notFound;
    if (u === '/api/sessions/s/publication-preview') return preview({checks:{fully_validated:false, summary:{passed:1, not_run:1}, skipped_not_approved:1}});
  });
  const {ui, area} = await open(b);
  button(area, 'Create draft PR').click(); await settle();
  const text = visibleText(area);
  assert.match(text, /1 skipped \(not approved\)/);
  assert.doesNotMatch(text, /not run/); ui.close();
});

test('a running session explains publishing waits for the session to finish and never asks for a publication', async () => {
  const b = setup(() => undefined, {state: session({state:'running', phase:'agent'})});
  const {ui, area} = await open(b);
  assert.match(visibleText(area), /Publishing isn't available yet\. You can create a draft pull request after the session finishes\./);
  assert.equal(button(area, 'Create draft PR'), undefined);
  assert.equal(b.requests.some(r => r.url.endsWith('/publication')), false); ui.close();
});

test('preview shows repository, branches, stats, collapsed paths, disclosed checks, editable fields and blockers with fix links', async () => {
  const paths = ['a.py', 'b.py', 'c.py', 'd.py', 'e.py', 'f.py', 'g/h.py'];
  const b = setup((m, u) => {
    if (u === '/api/sessions/s/publication') return notFound;
    if (u === '/api/sessions/s/publication-preview') return preview({repository:null, changed_paths:paths, checks:{fully_validated:false, summary:{passed:1, failed:1, unavailable:2}}, blocking_reasons:['publication_identity_required', 'publication_permission_denied'], publishable:false, stats:{files:7, additions:12, deletions:3}});
  });
  const {ui, area} = await open(b);
  const create = button(area, 'Create draft PR'); assert.ok(create); create.focus(); create.click(); await settle();
  assert.equal(b.requests.find(r => r.url.endsWith('/publication-preview')).body, '{}');
  const text = visibleText(area);
  assert.match(text, /Repositorygh:1 \(GitHub access not confirmed\)/); assert.match(text, /Base branchmain/); assert.match(text, /Head branchsession\/s/);
  assert.match(text, /7 files · \+12 · −3/);
  const details = area.querySelector('details'); assert.equal(details.open, false); assert.match(details.querySelector('summary').textContent, /7 changed paths/); assert.match(details.textContent, /g\/h\.py/);
  assert.match(text, /Not fully validated: 1 failed, 2 unavailable/);
  assert.equal(field(area, 'Pull request title').value, 'OpenDevEngine session s'); assert.match(field(area, 'Pull request description').value, /## Summary/);
  assert.match(text, /Set the commit author name and email used for pull requests\./); assert.match(text, /can't write to this repository/);
  assert.ok(area.querySelector('a[href="#/settings/providers"]')); assert.ok(area.querySelector('a[href="#/settings/github"]'));
  assert.match(text, /Code: publication_identity_required/);
  const submit = button(area, 'Publish draft PR'); assert.equal(submit.disabled, true); assert.equal(submit.getAttribute('aria-describedby'), 'publication-blocked-note');
  submit.closest('form').requestSubmit(); await settle(); assert.equal(posts(b, '/pull-requests').length, 0);
  // Focus moves into the review step, then back to the opener on Cancel.
  assert.equal(b.document.activeElement.textContent, 'Review draft pull request');
  assert.match(area.querySelector('[role="status"]').textContent, /can't be published yet/);
  button(area, 'Cancel').click(); await settle();
  assert.equal(b.document.activeElement, button(area, 'Create draft PR')); ui.close();
});

test('publish sends exactly the contract fields and reuses one key for double clicks and a lost response', async () => {
  let attempt = 0;
  const b = setup((m, u) => {
    if (u === '/api/sessions/s/publication') return notFound;
    if (u === '/api/sessions/s/publication-preview') return preview();
    if (u === '/api/sessions/s/pull-requests') { if (++attempt === 1) throw new Error('connection reset'); return {status:202, body:publication()}; }
  });
  const {ui, area} = await open(b);
  button(area, 'Create draft PR').click(); await settle();
  input(field(area, 'Pull request title'), 'Fix the calculator'); input(field(area, 'Pull request description'), 'Edited body');
  const submit = button(area, 'Publish draft PR'); submit.focus(); submit.click(); submit.click(); await settle();
  assert.equal(posts(b, '/pull-requests').length, 1, 'a double click sends one request');
  assert.match(visibleText(area), /outcome is unknown/); assert.equal(field(area, 'Pull request title').readOnly, true);
  button(area, 'Publish again (same request)').click(); await settle();
  const sent = posts(b, '/pull-requests'); assert.equal(sent.length, 2);
  assert.deepEqual(Object.keys(sent[0]).sort(), ['body', 'expected_diff_hash', 'idempotency_key', 'intent_version', 'title']);
  assert.deepEqual(sent[0], {title:'Fix the calculator', body:'Edited body', expected_diff_hash:H1, intent_version:1, idempotency_key:sent[0].idempotency_key});
  assert.deepEqual(sent[1], sent[0]); assert.match(sent[0].idempotency_key, /^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/);
  assert.match(visibleText(area), /Publishing draft pull request/); assert.equal(b.document.activeElement.textContent, 'Publishing draft pull request'); ui.close();
});

test('stale diff offers a new preview whose review step uses a new key and the new hash', async () => {
  let hash = H1, attempt = 0;
  const b = setup((m, u) => {
    if (u === '/api/sessions/s/publication') return notFound;
    if (u === '/api/sessions/s/publication-preview') return preview({diff_hash:hash});
    if (u === '/api/sessions/s/pull-requests') { if (++attempt === 1) { hash = H2; return conflict('stale_diff'); } return {status:202, body:publication({expected_diff_hash:H2})}; }
  });
  const {ui, area} = await open(b);
  button(area, 'Create draft PR').click(); await settle(); button(area, 'Publish draft PR').focus(); button(area, 'Publish draft PR').click(); await settle();
  assert.match(visibleText(area), /no longer match the preview you reviewed/); assert.match(visibleText(area), /Code: stale_diff/);
  assert.equal(b.document.activeElement.textContent, 'Preview again');
  b.document.activeElement.click(); await settle(); button(area, 'Publish draft PR').click(); await settle();
  const [first, second] = posts(b, '/pull-requests');
  assert.notEqual(first.idempotency_key, second.idempotency_key); assert.equal(second.expected_diff_hash, H2); ui.close();
});

test('idempotency conflict recovers the existing publication for this session', async () => {
  let created = false;
  const b = setup((m, u) => {
    if (u === '/api/sessions/s/publication') return created ? publication({state:'creating_branch'}) : notFound;
    if (u === '/api/sessions/s/publication-preview') return preview();
    if (u === '/api/sessions/s/pull-requests') { created = true; return conflict('idempotency_conflict'); }
  });
  const {ui, area} = await open(b);
  button(area, 'Create draft PR').click(); await settle(); button(area, 'Publish draft PR').click(); await settle();
  assert.match(visibleText(area), /In progress · Creating the branch/); ui.close();
});

test('progress polls to published, shows only a verified GitHub link, refreshes state and offers no merge', async () => {
  const published = publication({state:'published', commit_sha:'c'.repeat(40), pull_request_number:7, pull_request_url:'https://github.com/octo/demo/pull/7', pull_request_state:'open'});
  let server = publication();
  const b = setup((m, u) => {
    if (u === '/api/sessions/s/publication') return notFound;
    if (u === '/api/sessions/s/publication-preview') return preview();
    if (u === '/api/sessions/s/pull-requests') return {status:202, body:publication()};
    if (u === '/api/publications/pub1' && m === 'GET') return server;
    if (u === '/api/publications/pub1/refresh') return {...published, pull_request_state:'merged'};
  });
  const {ui, area} = await open(b, 5);
  button(area, 'Create draft PR').click(); await settle(); button(area, 'Publish draft PR').click(); await settle();
  const status = area.querySelector('[role="status"]'); assert.equal(status.getAttribute('aria-live'), 'polite');
  assert.match(visibleText(area), /In progress · Waiting to start/); assert.equal(area.querySelector('[aria-current="step"]').textContent, 'In progress · Waiting to start');
  server = publication({state:'creating_objects'});
  await until(() => /Done · Checking the base branchIn progress · Creating the commit/.test(visibleText(area)));
  assert.match(status.textContent, /Publishing: Creating the commit…/);
  server = published;
  await until(() => /Draft PR #7/.test(visibleText(area)));
  const link = area.querySelector('a.publication-pr-link');
  assert.equal(link.getAttribute('href'), 'https://github.com/octo/demo/pull/7'); assert.equal(link.getAttribute('target'), '_blank'); assert.equal(link.getAttribute('rel'), 'noopener noreferrer');
  assert.match(status.textContent, /Draft PR #7 published · open/); assert.match(visibleText(area), /Status on GitHub: open/);
  const after = b.requests.filter(r => r.url === '/api/publications/pub1').length; await new Promise(r => setTimeout(r, 30)); await settle();
  assert.equal(b.requests.filter(r => r.url === '/api/publications/pub1').length, after, 'polling stops once published');
  button(area, 'Refresh status').focus(); button(area, 'Refresh status').click(); await settle();
  assert.equal(b.requests.find(r => r.url.endsWith('/refresh')).body, '{}'); assert.match(visibleText(area), /Status on GitHub: merged/);
  assert.equal(b.document.activeElement.textContent, 'Refresh status');
  assert.equal([...ui.panel.querySelectorAll('button, a')].some(n => /merge/i.test(n.textContent)), false); ui.close();
});

test('an unverifiable pull request URL is never linked', async () => {
  for (const url of ['https://evil.example/octo/demo/pull/7', 'https://github.com/octo/demo/pull/7?x=1', 'javascript:alert(1)', 'https://github.com/octo/demo/pull/7/files', 'https://github.com.evil/octo/demo/pull/7']) {
    const b = setup((m, u) => u === '/api/sessions/s/publication' ? publication({state:'published', pull_request_number:7, pull_request_url:url, pull_request_state:'open'}) : undefined);
    const {ui, area} = await open(b);
    assert.match(visibleText(area), /Draft PR #7/); assert.match(visibleText(area), /could not be verified, so no link is shown/);
    assert.equal(area.querySelectorAll('a').some(a => a.getAttribute('href') === url || a.getAttribute('target') === '_blank'), false, url); ui.close();
  }
});

test('reconciling shows Checking GitHub with a Retry that uses a fresh key per deliberate retry', async () => {
  let retries = 0, state = 'reconciling';
  const b = setup((m, u) => {
    if (u === '/api/sessions/s/publication') return publication({state:'reconciling', error_code:'pr_outcome_unknown', commit_sha:'c'.repeat(40)});
    if (u === '/api/publications/pub1/retry') { if (++retries === 1) return conflict('reconciliation_pending'); state = 'creating_pr'; return {status:202, body:publication({state:'creating_pr'})}; }
    if (u === '/api/publications/pub1') return publication({state});
  });
  const {ui, area} = await open(b);
  assert.match(visibleText(area), /Checking GitHub…/); assert.match(visibleText(area), /didn't confirm whether the pull request was created/);
  const retry = button(area, 'Retry'); retry.focus(); retry.click(); await settle();
  assert.match(visibleText(area), /hasn't confirmed the earlier step yet/); assert.match(area.querySelector('[role="status"]').textContent, /has not confirmed the earlier step yet/);
  assert.equal(b.document.activeElement, button(area, 'Retry'));
  button(area, 'Retry').click(); await settle();
  const [first, second] = posts(b, '/retry'); assert.deepEqual(Object.keys(first), ['idempotency_key']); assert.notEqual(first.idempotency_key, second.idempotency_key);
  assert.match(visibleText(area), /In progress · Opening the draft pull request/); ui.close();
});

test('a lost or malformed retry response resends the same retry key; only a confirmed one clears it', async () => {
  let retries = 0;
  const b = setup((m, u) => {
    if (u === '/api/sessions/s/publication') return publication({state:'reconciling', error_code:'branch_outcome_unknown'});
    if (u === '/api/publications/pub1/retry') {
      retries += 1;
      if (retries === 1) throw new Error('offline');
      if (retries === 2) return {status:202, body:{accepted:true}};
      if (retries === 3) return {status:202, body:publication({state:'reconciling', error_code:'pr_outcome_unknown'})};
      return conflict('reconciliation_pending');
    }
  });
  const {ui, area} = await open(b);
  for (let i = 0; i < 4; i++) { button(area, 'Retry').click(); await settle(); }
  const keys = posts(b, '/retry').map(x => x.idempotency_key);
  assert.equal(keys.length, 4);
  assert.equal(keys[1], keys[0], 'network error keeps the key'); assert.equal(keys[2], keys[0], 'malformed success keeps the key');
  assert.notEqual(keys[3], keys[2], 'a validated confirmed response clears the key');
  ui.close();
});

test('reload recovers a published intent without previewing again', async () => {
  const b = setup((m, u) => u === '/api/sessions/s/publication' ? publication({state:'published', pull_request_number:12, pull_request_url:'https://github.com/octo/demo/pull/12', pull_request_state:'closed'}) : undefined);
  const {ui, area} = await open(b);
  assert.ok(area.querySelector('a[href="https://github.com/octo/demo/pull/12"]')); assert.match(visibleText(area), /Status on GitHub: closed/);
  assert.equal(button(area, 'Create draft PR'), undefined); assert.equal(b.requests.some(r => r.url.endsWith('/publication-preview')), false); ui.close();
});

test('no_changes explains that nothing was published', async () => {
  const b = setup((m, u) => u === '/api/sessions/s/publication' ? publication({state:'no_changes', finished_at:2}) : undefined);
  const {ui, area} = await open(b, 5);
  assert.match(visibleText(area), /Nothing to publish/); assert.match(visibleText(area), /no branch, commit or pull request was created/);
  await new Promise(r => setTimeout(r, 20)); await settle();
  assert.equal(b.requests.filter(r => r.url === '/api/publications/pub1').length, 0); ui.close();
});

test('blocked and failed outcomes show plain language with the raw code in small print', async () => {
  for (const [state, code, pattern, fix] of [['blocked', 'base_conflict', /same files/, null], ['blocked', 'publication_permission_denied', /can't write to this repository/, '#/settings/github'], ['failed', 'github_unavailable', /GitHub was unavailable/, null], ['failed', 'new_future_code', /Publishing stopped: new future code/, null]]) {
    const b = setup((m, u) => u === '/api/sessions/s/publication' ? publication({state, error_code:code}) : undefined);
    const {ui, area} = await open(b);
    assert.match(visibleText(area), /Couldn't publish the draft pull request/); assert.match(visibleText(area), pattern);
    assert.equal(area.querySelector('small.publication-code').textContent, `Code: ${code}`);
    if (fix) assert.ok(area.querySelector(`a[href="${fix}"]`));
    assert.equal(button(area, 'Retry'), undefined); ui.close();
  }
});

test('leaving the session page stops publication polling', async () => {
  const b = setup((m, u) => u === '/api/sessions/s/publication' || u === '/api/publications/pub1' ? publication({state:'preparing'}) : undefined);
  const {ui} = await open(b, 5);
  await until(() => b.requests.some(r => r.url === '/api/publications/pub1'));
  b.window.location.hash = '#/settings'; await settle();
  const seen = b.requests.filter(r => r.url === '/api/publications/pub1').length;
  await new Promise(r => setTimeout(r, 30)); await settle();
  assert.equal(b.requests.filter(r => r.url === '/api/publications/pub1').length, seen); ui.close();
});

test('a build without publication routes keeps the existing explanation', async () => {
  const b = setup(() => undefined);
  const {ui, area} = await open(b);
  assert.match(visibleText(area), /Publishing isn't available yet in this build\./); assert.match(visibleText(area), /Saved publication policy: manual draft/);
  assert.equal(button(area, 'Create draft PR'), undefined); ui.close();
});

test('preview failure is explained in place and Create can be tried again', async () => {
  let fail = true;
  const b = setup((m, u) => {
    if (u === '/api/sessions/s/publication') return notFound;
    if (u === '/api/sessions/s/publication-preview') return fail ? conflict('rejected_paths') : preview();
  });
  const {ui, area} = await open(b);
  const create = button(area, 'Create draft PR'); create.focus(); create.click(); await settle();
  assert.match(visibleText(area), /rejected by the artifact policy/); assert.equal(b.document.activeElement, button(area, 'Create draft PR'));
  fail = false; button(area, 'Create draft PR').click(); await settle(); assert.ok(button(area, 'Publish draft PR')); ui.close();
});

test('Pull request author card saves name and email and shows validation errors inline', async () => {
  const patches = []; let answer = {status:400, body:{error:'Invalid', code:'validation'}};
  const b = setup((m, u, o) => {
    if (u === '/api/settings/publication' && m === 'GET') return {commit_name:null, commit_email:null};
    if (u === '/api/settings/publication' && m === 'PATCH') { patches.push(JSON.parse(o.body)); return answer; }
    if (u.startsWith('/api/execution-profiles')) return {items:[], next_cursor:null};
    if (u === '/api/settings/execution') return {max_active_sessions:1};
    if (u === '/api/settings/model-roles') return {settings:{local_only:false}};
  }, {hash:'#/settings/agent'});
  const ui = createExecutionSettings({request:requestFor(b), pollMs:1e6}); b.document.body.append(ui.element); await ui.refresh(); await settle();
  const card = ui.element.querySelector('.publication-identity'); assert.equal(card.hidden, false);
  assert.equal(card.querySelector('h3').textContent, 'Pull request author'); assert.match(visibleText(card), /blocked until an author is saved/);
  const name = field(card, 'Commit author name'), email = field(card, 'Commit author email'); assert.equal(email.type, 'email');
  input(name, 'Ada Lovelace'); input(email, 'not-an-email'); button(card, 'Save pull request author').click(); await settle();
  assert.equal(patches.length, 0); assert.equal(email.getAttribute('aria-invalid'), 'true');
  const emailError = b.document.getElementById(email.getAttribute('aria-describedby')); assert.match(emailError.textContent, /valid email address/); assert.equal(b.document.activeElement, email);
  input(email, 'ada@example.com'); assert.equal(email.hasAttribute('aria-invalid'), false);
  button(card, 'Save pull request author').click(); await settle();
  assert.deepEqual(patches[0], {commit_name:'Ada Lovelace', commit_email:'ada@example.com'});
  assert.match(visibleText(card), /OpenDevEngine rejected this email/); assert.equal(name.getAttribute('aria-invalid'), 'true'); assert.match(visibleText(card), /Not saved/);
  answer = {commit_name:'Ada Lovelace', commit_email:'ada@example.com'}; button(card, 'Save pull request author').click(); await settle();
  assert.equal(patches.length, 2); assert.match(visibleText(card), /Pull request author saved/); assert.equal(name.hasAttribute('aria-invalid'), false);
  // A background reload keeps the saved values; unsaved typing is never overwritten.
  input(name, 'Typing…'); await ui.refresh(); await settle(); assert.equal(field(ui.element.querySelector('.publication-identity'), 'Commit author name').value, 'Typing…'); ui.close();
});

test('Pull request author card is hidden when the publication settings route is absent', async () => {
  const b = setup((m, u) => {
    if (u.startsWith('/api/execution-profiles')) return {items:[], next_cursor:null};
    if (u === '/api/settings/execution') return {max_active_sessions:1};
    if (u === '/api/settings/model-roles') return {settings:{local_only:false}};
  }, {hash:'#/settings/agent'});
  const ui = createExecutionSettings({request:requestFor(b), pollMs:1e6}); b.document.body.append(ui.element); await ui.refresh(); await settle();
  assert.equal(ui.element.querySelector('.publication-identity').hidden, true); assert.doesNotMatch(visibleText(ui.element), /Pull request author/); ui.close();
});

test('an intent created after the first not_found read is found by the bounded recheck', async () => {
  let reads = 0;
  const b = setup((m, u) => {
    if (u === '/api/sessions/s/publication') return ++reads < 3 ? notFound : publication({state:'queued', policy:'on_success_draft'});
    if (u === '/api/publications/pub1') return publication({state:'preparing', policy:'on_success_draft'});
  }, {state: session({publication:{policy:'on_success_draft'}})});
  const {ui, area} = await open(b, 20);
  assert.ok(button(area, 'Create draft PR')); assert.equal(reads, 1);
  await until(() => /Publishing draft pull request/.test(visibleText(area)));
  assert.equal(button(area, 'Create draft PR'), undefined); assert.equal(reads, 3);
  await until(() => /In progress · Checking the base branch/.test(visibleText(area)));
  await new Promise(r => setTimeout(r, 40)); await settle();
  assert.equal(reads, 3, 'rechecks stop once an intent exists'); ui.close();
});

test('rechecks while no intent exists are bounded and stop when the page is left', async () => {
  let reads = 0;
  const b = setup((m, u) => { if (u === '/api/sessions/s/publication') { reads += 1; return notFound; } });
  const {ui} = await open(b, 1);
  await until(() => reads >= 4);
  b.window.location.hash = '#/settings'; await settle();
  const left = reads; await new Promise(r => setTimeout(r, 60)); await settle();
  assert.equal(reads, left, 'no reads after leaving');
  const b2 = setup((m, u) => { if (u === '/api/sessions/s/publication') { reads += 1; return notFound; } });
  reads = 0; const second = await open(b2, 1);
  await new Promise(r => setTimeout(r, 400)); await settle();
  assert.equal(reads, 9, 'one recovery read plus at most eight backoff rechecks'); second.ui.close(); ui.close();
});

test('leaving the page aborts an unresolved publication read and ignores its late answer', async () => {
  let release, signal;
  const b = setup((m, u, o) => { if (u === '/api/sessions/s/publication') { signal = o.signal; return new Promise(r => { release = r; }); } });
  const {ui, area} = await open(b);
  assert.match(visibleText(area), /Checking for an existing draft pull request/); assert.ok(signal); assert.equal(signal.aborted, false);
  b.window.location.hash = '#/settings'; await settle();
  assert.equal(signal.aborted, true);
  release(publication({state:'published', pull_request_number:3, pull_request_url:'https://github.com/octo/demo/pull/3', pull_request_state:'open'})); await settle();
  assert.doesNotMatch(area.textContent, /Draft PR #3/); assert.equal(b.requests.filter(r => r.url === '/api/publications/pub1').length, 0); ui.close();
});

test('identity fields are read-only while a save is in flight and keep the saved values after it', async () => {
  let finish; const patches = [];
  const b = setup((m, u, o) => {
    if (u === '/api/settings/publication' && m === 'GET') return {commit_name:'Old', commit_email:'old@example.com'};
    if (u === '/api/settings/publication' && m === 'PATCH') { patches.push(JSON.parse(o.body)); return new Promise(r => { finish = r; }); }
    if (u.startsWith('/api/execution-profiles')) return {items:[], next_cursor:null};
    if (u === '/api/settings/execution') return {max_active_sessions:1};
    if (u === '/api/settings/model-roles') return {settings:{local_only:false}};
  }, {hash:'#/settings/agent'});
  const ui = createExecutionSettings({request:requestFor(b), pollMs:1e6}); b.document.body.append(ui.element); await ui.refresh(); await settle();
  const card = ui.element.querySelector('.publication-identity'), name = field(card, 'Commit author name'), email = field(card, 'Commit author email');
  input(name, 'Ada'); input(email, 'ada@example.com'); button(card, 'Save pull request author').click(); await settle();
  assert.equal(patches.length, 1); assert.equal(name.readOnly, true); assert.equal(email.readOnly, true); assert.equal(card.querySelector('form').getAttribute('aria-busy'), 'true');
  assert.equal(button(card, 'Save pull request author').disabled, true);
  finish({commit_name:'Ada', commit_email:'ada@example.com'}); await settle();
  assert.equal(name.readOnly, false); assert.equal(email.readOnly, false); assert.equal(card.querySelector('form').hasAttribute('aria-busy'), false);
  assert.equal(name.value, 'Ada'); assert.match(visibleText(card), /Pull request author saved\. It applies/); ui.close();
});

test('round-2 shapes: base observation, GitHub blocker codes, publication check summary and publication_stopped', async () => {
  const b = setup((m, u) => {
    if (u === '/api/sessions/s/publication') return notFound;
    if (u === '/api/sessions/s/publication-preview') return preview({head_branch:'opendevengine/s-<suffix>', base_observation:{sha:'9'.repeat(40), diverged:true}, blocking_reasons:['base_overlap', 'divergence_unknown', 'timeout'], publishable:false});
  });
  const {ui, area} = await open(b);
  button(area, 'Create draft PR').click(); await settle();
  const text = visibleText(area);
  assert.match(text, /base branch has moved since the session started \(now 999999999999\)/); assert.match(text, /opendevengine\/s-<suffix>/);
  assert.match(text, /changed some of the same files/); assert.match(text, /too far to check safely/); assert.match(text, /did not answer in time/);
  assert.equal(button(area, 'Publish draft PR').disabled, true); ui.close();
  for (const [checks, shown] of [[{status:'unavailable'}, 'unavailable'], [{fully_validated:true}, 'all passed'], [{fully_validated:false}, 'not fully validated']]) {
    const b2 = setup((m, u) => u === '/api/sessions/s/publication' ? publication({state:'blocked', error_code:'publication_stopped', checks}) : undefined);
    const opened = await open(b2);
    assert.match(visibleText(opened.area), new RegExp(`Local checks${shown}`)); assert.match(visibleText(opened.area), /Publishing stopped because/); assert.match(visibleText(opened.area), /Code: publication_stopped/); opened.ui.close();
  }
});


test('publication review shows a bounded omitted generated path list and total', async () => {
  const omitted = Array.from({length: 25}, (_, i) => `build/generated-${i}.js`);
  omitted[0] = '<img src=x onerror=alert(1)>';
  const b = setup((m, u) => {
    if (u.endsWith('/publication')) return notFound;
    if (u.endsWith('/publication-preview')) return preview({omitted_paths: omitted});
  });
  const {ui, area} = await open(b);
  button(area, 'Create draft PR').click(); await settle();
  const text = visibleText(area);
  assert.match(text, /Omitted generated paths \(25\)/);
  assert.match(text, /These generated artifacts will not be published/);
  assert.match(text, /5 more omitted paths/);
  assert.ok(text.includes(omitted[0]));
  assert.ok(text.includes(omitted[19]));
  assert.ok(!text.includes(omitted[20]));
  assert.equal(area.querySelector('img'), null);
  assert.equal(button(area, 'Publish draft PR').disabled, false);
  ui.close();
});

test('sensitive content preview rejection explains the block without source contents', async () => {
  const b = setup((m, u) => {
    if (u === '/api/sessions/s/publication') return notFound;
    if (u === '/api/sessions/s/publication-preview') return conflict('sensitive_content');
  });
  const {ui, area} = await open(b);
  button(area, 'Create draft PR').click(); await settle();
  assert.match(visibleText(area), /Sensitive content was detected in the changes, so this change can't be published\./);
  assert.equal(button(area, 'Publish draft PR'), undefined);
  ui.close();
});
