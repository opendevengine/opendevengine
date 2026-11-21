// Session list and session page presentation: readable names instead of raw ids,
// a plain-language result card per terminal state, a folded timeline, inline diffs and the composer
// only while a session can take input. Fixtures only; no real session data.
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createBrowser, install, settle, input, visibleText} from './dom.mjs';
import {createSessionPanel, duration} from '../../opendevengine/web/sessions.js';
const html = readFileSync(new URL('../../opendevengine/web/index.html', import.meta.url), 'utf8');
const BASE = 'c'.repeat(40);
const T0 = Date.now() - 60_000;
const session = (id = 's', changes = {}) => ({id, plan_id: 'plan-a', plan_version_id: 'pv-a', content_hash: 'a'.repeat(64), attempt: 1, state: 'running', phase: 'agent', source_available: true, execution: {}, limits: {}, repositories: [{repository_id: 'gh:42', role: 'write', base_sha: BASE, base_branch: 'main', session_branch: `opendevengine/${id}`}], runner: {containment: 'pending'}, capabilities: {ask_user: true, follow_up: true}, usage: {}, outcome: {}, publication: {policy: 'manual_draft'}, created_at: T0, started_at: T0 + 100, updated_at: T0 + 30_000, latest_seq: 0, pending_question: null, ...changes});
const plan = {id: 'plan-a', title: 'Fix the rounding bug', conversation_id: 'conv-1', latest_version_id: 'pv-a', version: {id: 'pv-a', version: 2, content: {steps: [{title: 'Change round()'}, {title: 'Run the tests'}]}}};
const catalog = {items: [{id: 'gh:42', name: 'demo', full_name: 'octo/demo'}, {id: 'gh:7', name: 'tools', full_name: 'octo/tools'}], next_cursor: null};
const notFound = {status: 404, body: {code: 'not_found'}};
const button = (root, text) => [...root.querySelectorAll('button')].find(n => n.textContent === text);
const field = (root, text) => [...root.querySelectorAll('label')].find(n => n.textContent.startsWith(text))?.querySelector('input, textarea, select');
function requestFor(b) { return async (path, method = 'GET', body, {signal} = {}) => { const r = await b.window.fetch(path, {method, ...(signal ? {signal} : {}), ...(body === undefined ? {} : {body: JSON.stringify(body)})}); const v = await r.json(); if (!r.ok) { const e = new Error(v.code || 'request failed'); e.status = r.status; e.code = v.code; throw e; } return v; }; }
// `routes(method, url, options)` answers what a test cares about; everything else gets a neutral default.
function setup(routes, hash = '#/sessions/s') {
  const b = createBrowser(html, {hash, respond: (m, u, o) => {
    const answer = routes(m, u, o); if (answer !== undefined) return answer;
    if (u.includes('/events?')) return {items: [], next_seq: 0, has_more: false};
    if (u.includes('/messages?')) return {items: [], next_cursor: null};
    if (u.endsWith('/diff')) return {available: false, reason: 'no_checkpoint'};
    if (u.endsWith('/validations')) return {items: [], fully_validated: false};
    if (u.endsWith('/publication')) return notFound;
    if (u.startsWith('/api/plans/plan-a')) return plan;
    if (u.startsWith('/api/repositories?')) return catalog;
    if (u.startsWith('/api/sessions?')) return {items: [], next_cursor: null};
    if (u === '/api/sessions/s') return session('s');
    return {status: 404, body: {code: 'not_found'}};
  }});
  install(b); b.document.getElementById('view-sessions').hidden = false;
  const data = new Map(); b.window.sessionStorage = {getItem: k => data.get(k), setItem: (k, v) => data.set(k, v), removeItem: k => data.delete(k)};
  return b;
}
const open = async (b, extra = {}) => { const ui = createSessionPanel({host: b.document.getElementById('sessions-content'), request: requestFor(b), pollMs: 1e6, ...extra}); ui.open(); await settle(); await settle(); return ui; };
// Meta lines use aria-hidden dot separators drawn with CSS gaps; read the parts the way assistive technology does.
const parts = node => node.children.filter(c => !c.classList.contains('session-meta-sep')).map(c => visibleText(c).trim()).join(' | ');
const event = (seq, type, payload, at = T0 + seq * 1000) => ({session_id: 's', seq, type, payload, created_at: at});

test('a finished session leads with a plain-language result for each terminal state', async () => {
  const diff = {available: true, repository_id: 'gh:42', base_sha: BASE, diff_hash: 'h'.repeat(64), artifact_id: 'art_1', files: [{path: 'calc.py', status: 'modified', additions: 1, deletions: 1, diff: '--- a/calc.py\n+++ b/calc.py\n@@ -1 +1 @@\n-x\n+y\n'}], rejected: []};
  const cases = [
    [{state: 'completed', phase: 'completed', runner: {containment: 'confirmed'}}, {diff, checks: {items: [{ordinal: 1, status: 'passed', command: ['pytest']}], fully_validated: true}}, /^Changes ready for review: 1 file, \+1 −1, checks passed\.$/, /create a draft pull request/, 'is-ok'],
    [{state: 'completed', phase: 'completed', runner: {containment: 'confirmed'}}, {diff, checks: {items: [{ordinal: 1, status: 'failed', command: ['pytest']}], fully_validated: false}}, /^Changes ready for review: 1 file, \+1 −1, 1 check failed\.$/, /create a draft pull request/, 'is-warn'],
    [{state: 'cancelled', phase: 'cancelled', stop_reason: 'user_requested', stop_requested: true, runner: {containment: 'confirmed'}}, {}, /^Stopped by you; the container was removed\.$/, /Retry as a new attempt/, 'is-off'],
    [{state: 'interrupted', phase: 'interrupted', error_code: 'runtime_interrupted', stop_reason: 'runtime_interrupted', runner: {containment: 'confirmed'}, outcome: {edits_since_last_checkpoint_may_be_lost: true}}, {}, /^OpenDevEngine stopped while this session ran\. The container was cleaned up\. Edits after the last checkpoint may be lost\.$/, /Retry as a new attempt/, 'is-danger'],
    [{state: 'failed', phase: 'failed', error_code: 'docker_unavailable', runner: {containment: 'confirmed'}}, {}, /^This session failed: Docker isn't running or isn't reachable\.$/, /Fix the cause/, 'is-danger'],
  ];
  for (const [changes, extra, headline, next, tone] of cases) {
    const b = setup((m, u) => {
      if (u === '/api/sessions/s') return session('s', changes);
      if (u.endsWith('/diff') && extra.diff) return extra.diff;
      if (u.endsWith('/validations') && extra.checks) return extra.checks;
    });
    const ui = await open(b);
    const card = ui.panel.querySelector('.session-result');
    assert.equal(card.hidden, false, changes.state); assert.ok(card.classList.contains(tone), `${changes.state} tone`);
    assert.match(card.querySelector('.session-result-headline').textContent, headline);
    assert.match(visibleText(card), next);
    // The old "state · phase" heading is gone; the badge carries the state.
    assert.doesNotMatch(visibleText(ui.panel), new RegExp(`${changes.state} · ${changes.phase}`));
    if (changes.error_code) assert.equal(card.querySelector('.session-result-code').textContent, `Code: ${changes.error_code}`);
    // A finished session is never described as still stopping, even though stop_requested stays recorded.
    assert.doesNotMatch(visibleText(ui.panel), /not confirmed stopped/);
    ui.close();
  }
});

test('interrupted result offers Retry, which reuses the one retry key shared with the header', async () => {
  const keys = [];
  const b = setup((m, u, o) => {
    if (m === 'POST' && u.endsWith('/retry')) { keys.push(JSON.parse(o.body).idempotency_key); if (keys.length === 1) throw new Error('lost'); return {session_id: 'next', state: 'queued'}; }
    if (u === '/api/sessions/s') return session('s', {state: 'interrupted', phase: 'interrupted', error_code: 'runtime_interrupted', runner: {containment: 'confirmed'}});
  });
  const ui = await open(b);
  const cardRetry = ui.panel.querySelector('.session-result button');
  assert.equal(cardRetry.getAttribute('aria-label'), 'Retry as a new attempt'); assert.equal(cardRetry.disabled, false);
  // Only one Retry beside the result: the ended line does not repeat it.
  assert.equal(ui.panel.querySelector('.session-ended button'), null);
  cardRetry.click(); await settle();
  button(ui.panel, 'Retry as a new attempt').click(); await settle();
  assert.equal(keys.length, 2); assert.equal(keys[0], keys[1]); assert.equal(b.window.location.hash, '#/sessions/next'); ui.close();
});

test('a running session has no result card; the composer says Send guidance and switches for a question', async () => {
  let s = session('s');
  const b = setup((m, u) => u === '/api/sessions/s' ? s : undefined);
  const ui = await open(b);
  assert.equal(ui.panel.querySelector('.session-result').hidden, true);
  const form = ui.panel.querySelector('.session-composer');
  assert.equal(form.hidden, false); assert.ok(field(form, 'Send guidance'));
  assert.equal(field(form, 'Message target').closest('label').hidden, true, 'one target needs no picker');
  assert.equal(ui.panel.querySelector('.session-ended').hidden, true);
  s = session('s', {state: 'waiting_for_user', pending_question: {id: 'q1', prompt: 'Which file?'}}); await ui.refresh(); await settle();
  const answer = field(form, "Answer the agent's question");
  assert.ok(answer); assert.equal(answer.getAttribute('placeholder'), 'Type your answer…');
  assert.equal(field(form, 'Message target').value, 'q1'); assert.equal(field(form, 'Message target').closest('label').hidden, false);
  const target = field(form, 'Message target'); target.value = ''; target.dispatchEvent({type: 'change', bubbles: true});
  assert.ok(field(form, 'Send guidance'), 'choosing guidance relabels the composer'); ui.close();
});

test('a terminal session hides the composer and shows one ended line with the retry action', async () => {
  const b = setup((m, u) => u === '/api/sessions/s' ? session('s', {state: 'completed', phase: 'completed', runner: {containment: 'confirmed'}}) : undefined);
  const ui = await open(b);
  assert.equal(ui.panel.querySelector('.session-composer').hidden, true);
  const ended = ui.panel.querySelector('.session-ended');
  assert.equal(ended.hidden, false); assert.match(visibleText(ended), /^This session has ended\./);
  const retry = ended.querySelector('button'); assert.equal(retry.textContent, 'Retry'); assert.equal(retry.getAttribute('aria-label'), 'Retry as a new attempt'); assert.equal(retry.disabled, false);
  assert.equal(button(ui.panel, 'Stop session').hidden, true); assert.equal(button(ui.panel, 'Retry as a new attempt').hidden, false);
  ui.close();
});

test('the timeline folds setup, groups work, reads checkpoints plainly and shows agent text as text', async () => {
  const unsafe = '<img src=x onerror=alert(1)> **done**\n`code`';
  const items = [
    event(1, 'session.state', {state: 'queued', previous: null}),
    event(2, 'session.state', {state: 'provisioning', phase: 'access_check', previous: 'queued'}),
    event(3, 'session.state', {state: 'provisioning', phase: 'source_acquired', counts: {files: 3}}),
    event(4, 'checkpoint', {ok: true, files: 0, additions: 0, deletions: 0, rejected: 0}),
    event(5, 'session.state', {state: 'running', phase: 'agent', previous: 'provisioning'}),
    event(6, 'agent.summary', {kind: 'ready', model: 'model-x'}),
    event(7, 'tool.started', {tool: 'read_file', path: 'calc.py'}),
    event(8, 'tool.finished', {tool: 'read_file', ok: true}),
    event(9, 'tool.started', {tool: 'write_file', path: 'calc.py'}),
    event(10, 'checkpoint', {ok: true, files: 1, additions: 1, deletions: 1, rejected: 2}),
    event(11, 'tool.finished', {tool: 'write_file', ok: true}),
    event(12, 'agent.summary', {kind: 'assistant', text: unsafe}),
    event(13, 'agent.summary', {kind: 'result', status: 'completed', text: unsafe}),
  ];
  const b = setup((m, u) => u.includes('/events?') ? {items, next_seq: 13, has_more: false} : undefined);
  const ui = await open(b);
  const activity = ui.panel.querySelector('.session-activity');
  const [setupGroup, work] = activity.querySelectorAll('details.activity-group');
  assert.match(setupGroup.querySelector('summary').textContent, /^Set up · 5 s · model-x$/);
  assert.equal(setupGroup.open, false, 'setup starts folded once the agent is running');
  assert.match(visibleText(setupGroup), /Checked repository access/); assert.match(visibleText(setupGroup), /Copied the repository · 3 files/); assert.match(visibleText(setupGroup), /Checkpoint saved · no changes/);
  assert.match(work.querySelector('summary').textContent, /^Worked for 4 s · 2 steps in 1 file$/);
  assert.match(visibleText(work), /Checkpoint saved · \+1 −1 in 1 file · 2 paths left out/); assert.match(visibleText(work), /Read · calc\.py/);
  const message = activity.querySelector('.timeline-entry.has-message .agent-message');
  // Markdown renders as elements; the HTML-like part stays literal text.
  const para = message.querySelector('p');
  assert.equal(para.textContent, '<img src=x onerror=alert(1)> done\ncode'); assert.equal(para.querySelector('strong').textContent, 'done'); assert.equal(para.querySelector('code').textContent, 'code');
  assert.equal(ui.panel.querySelector('img'), null); assert.equal(ui.panel.querySelectorAll('[data-seq="13"]').length, 1);
  assert.equal(activity.querySelectorAll('.agent-message').length, 1, 'the result repeating the last message is shown once');
  const when = activity.querySelector('[data-seq="12"] time');
  assert.match(when.getAttribute('title'), / · #12$/); assert.ok(when.getAttribute('datetime')); assert.match(when.textContent, /^(now|1 min ago)$/);
  assert.equal(activity.querySelector('[data-seq="12"] .timeline-mark').getAttribute('aria-hidden'), 'true');
  ui.close();
});

test('the final agent message in the result card is text, never markup', async () => {
  const unsafe = '<script>alert(1)</script><b>bold</b>';
  const b = setup((m, u) => {
    if (u === '/api/sessions/s') return session('s', {state: 'completed', phase: 'completed', runner: {containment: 'confirmed'}});
    if (u.includes('/events?')) return {items: [event(1, 'agent.summary', {kind: 'result', status: 'completed', text: unsafe})], next_seq: 1, has_more: false};
  });
  const ui = await open(b);
  const final = ui.panel.querySelector('.session-result .agent-final');
  assert.equal(final.querySelector('figcaption').textContent, "Agent's final message"); assert.equal(final.querySelector('p').textContent, unsafe);
  assert.equal(ui.panel.querySelector('script'), null); assert.equal(ui.panel.querySelector('b'), null);
  // In the timeline the same text is a clamped preview, still as text.
  assert.ok(ui.panel.querySelector('.session-activity .agent-message.is-clamped')); ui.close();
});

test('the header names the plan, repository, base and attempt; the live indicator and Reconnect follow the connection', async () => {
  let down = false;
  const b = setup((m, u) => { if (down) throw new Error('offline'); if (u === '/api/sessions/s') return session('s', {attempt: 3, predecessor_id: 'p2'}); });
  const ui = await open(b);
  const header = ui.panel.querySelector('.session-header');
  assert.equal(header.querySelector('h2').textContent, 'Fix the rounding bug');
  assert.match(parts(header.querySelector('.session-meta')), /^octo\/demo main@ccccccc \| Attempt 3 \(previous\) \| Running \d+ (s|min)[^|]* \| Plan v2 · conversation$/);
  assert.equal(header.querySelector('.session-meta code').getAttribute('title'), `Base ${BASE}`);
  assert.equal(header.querySelector('a[href="#/sessions/p2"]').getAttribute('aria-label'), 'Previous attempt');
  assert.ok(header.querySelector('a[href="#/ask/conv-1"]'));
  assert.doesNotMatch(visibleText(header), /gh:42|pv-a|ccccccccccccc/);
  const indicator = header.querySelector('.session-live');
  assert.equal(indicator.getAttribute('role'), 'status'); assert.equal(visibleText(indicator), 'Live'); assert.equal(button(header, 'Reconnect').hidden, true);
  assert.equal(button(header, 'Stop session').hidden, false);
  down = true; await ui.refresh(); await settle();
  assert.match(visibleText(indicator), /^Reconnecting…/); assert.equal(button(header, 'Reconnect').hidden, false);
  assert.equal(header.querySelector('.session-phase').textContent, 'running', 'the last confirmed state stays visible');
  down = false; button(header, 'Reconnect').click(); await settle();
  assert.equal(visibleText(indicator), 'Live'); assert.equal(button(header, 'Reconnect').hidden, true); ui.close();
});

test('a completed session offers Create draft PR… in the header, which jumps to the PR tab; a published one says View draft PR', async () => {
  let published = false;
  const b = setup((m, u) => {
    if (u === '/api/sessions/s') return session('s', {state: 'completed', phase: 'completed', runner: {containment: 'confirmed'}});
    if (u === '/api/sessions/s/publication') return published ? {id: 'pub1', session_id: 's', state: 'published', pull_request_number: 4, pull_request_url: 'https://github.com/octo/demo/pull/4', pull_request_state: 'open', base_branch: 'main', head_branch: 'opendevengine/s'} : undefined;
  });
  const ui = await open(b);
  const action = button(ui.panel, 'Create draft PR…');
  assert.ok(action); assert.equal(action.hidden, false); assert.ok(action.closest('.session-header'));
  action.click(); await settle();
  assert.equal(b.document.getElementById('session-tab-3').getAttribute('aria-selected'), 'true');
  assert.equal(b.document.activeElement.textContent, 'Create draft PR', 'focus lands on the real create control');
  ui.close();
  published = true;
  const ui2 = await open(b);
  assert.equal(button(ui2.panel, 'View draft PR').hidden, false); assert.equal(ui2.panel.querySelector('.session-phase').textContent, 'PR open');
  assert.match(visibleText(ui2.panel.querySelector('.session-result')), /Draft PR #4 is on GitHub/); ui2.close();
});

test('an interrupted session does not offer to create a draft PR from the header', async () => {
  const b = setup((m, u) => u === '/api/sessions/s' ? session('s', {state: 'interrupted', phase: 'interrupted', runner: {containment: 'confirmed'}}) : undefined);
  const ui = await open(b);
  assert.equal(button(ui.panel, 'Create draft PR…').hidden, true); ui.close();
});

test('the Diff tab shows a summary line, the unified diff inline with +/- lines, and plain reasons for left-out paths', async () => {
  const diff = {available: true, repository_id: 'gh:42', base_sha: BASE, diff_hash: 'f'.repeat(64), artifact_id: 'art_9', captured_at: T0, files: [{path: 'calc.py', status: 'modified', additions: 1, deletions: 1, mode: '100644', bytes: 92, diff: '--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n'}], rejected: [{path: '__pycache__/calc.pyc', reason: 'excluded_generated'}, {path: 'big.bin', reason: 'some_new_reason'}]};
  const b = setup((m, u) => u.endsWith('/diff') ? diff : undefined);
  const ui = await open(b);
  const tab = b.document.getElementById('session-tab-1'); tab.click();
  const panel = b.document.getElementById('session-tabpanel-1');
  assert.equal(panel.querySelector('.diff-totals').textContent, '1 file · +1 −1 · base main@ccccccc');
  const file = panel.querySelector('details.diff-file'); assert.equal(file.open, true, 'small diffs open by default');
  assert.match(file.querySelector('summary').textContent, /modified.*calc\.py.*\+1 −1/);
  assert.equal(file.querySelector('.diff-del').textContent, '-    return a - b\n'); assert.equal(file.querySelector('.diff-add').textContent, '+    return a + b\n');
  assert.equal(file.querySelector('.diff-hunk').textContent, '@@ -1,2 +1,2 @@\n'); assert.equal(file.querySelectorAll('.diff-meta').length, 2);
  const rejected = panel.querySelector('.diff-rejected');
  assert.match(visibleText(rejected), /2 paths left out/); assert.match(visibleText(rejected), /__pycache__\/calc\.pyc — generated file, left out/); assert.match(visibleText(rejected), /big\.bin — some new reason/);
  const meta = panel.querySelector('details.diff-details'); assert.equal(meta.open, false);
  assert.match(meta.textContent, /f{64}/); assert.match(meta.textContent, /art_9/); assert.match(meta.textContent, new RegExp(BASE));
  // Hashes and artifact ids live only in the folded details.
  meta.remove(); assert.doesNotMatch(panel.textContent, /fffffffff|art_9/); ui.close();
});

test('large diffs start folded and a reader-opened file stays open across refreshes', async () => {
  const big = Array.from({length: 450}, (_, i) => `+line ${i}`).join('\n');
  let diff = {available: true, files: [{path: 'a.py', status: 'added', additions: 450, deletions: 0, diff: big}, {path: 'b.py', status: 'modified', additions: 1, deletions: 0, diff: '+x'}]};
  const b = setup((m, u) => u.endsWith('/diff') ? diff : undefined);
  const ui = await open(b);
  const files = () => b.document.getElementById('session-tabpanel-1').querySelectorAll('details.diff-file');
  assert.deepEqual(files().map(f => f.open), [false, false]);
  files()[1].setAttribute('open', '');
  diff = {...diff, captured_at: 5}; await ui.refresh(); await settle();
  assert.deepEqual(files().map(f => f.open), [false, true]); ui.close();
});

test('the Checks tab shows each command, a pass/fail badge, duration and output, with the environment folded', async () => {
  const checks = {fully_validated: false, items: [{ordinal: 1, status: 'passed', required: true, exit_code: 0, command: {argv: ['sh', '-c', 'pytest -q'], command: 'pytest -q', expected: 'All tests pass.'}, source: 'proposed', output: '3 passed', started_at: T0, finished_at: T0 + 2500, environment: {image_id: 'sha256:img', cwd: '/workspace', network: 'none'}}, {ordinal: 2, status: 'failed', required: false, exit_code: 1, command: ['ruff', 'check'], output: 'E501', started_at: T0, finished_at: T0 + 100, environment: {}}]};
  const b = setup((m, u) => u.endsWith('/validations') ? checks : undefined);
  const ui = await open(b);
  b.document.getElementById('session-tab-2').click();
  const panel = b.document.getElementById('session-tabpanel-2');
  const [first, second] = panel.querySelectorAll('.check-item');
  assert.equal(first.querySelector('.badge').textContent, 'passed'); assert.ok(first.querySelector('.badge').classList.contains('badge-ok'));
  assert.match(first.querySelector('.check-head').textContent, /Check 1 · required · 3 s · exit 0/);
  assert.equal(first.querySelector('pre.check-command code').textContent, 'pytest -q'); assert.equal(first.querySelector('pre.check-output').textContent, '3 passed');
  assert.match(visibleText(first), /Expected: All tests pass\./);
  const env = first.querySelector('details.check-env'); assert.equal(env.open, false); assert.match(env.textContent, /sha256:img/); assert.match(env.textContent, /Network none/);
  assert.ok(second.querySelector('.badge').classList.contains('badge-danger')); assert.match(second.querySelector('.check-head').textContent, /optional · under 1 s · exit 1/);
  assert.match(visibleText(panel), /Not fully validated/); ui.close();
});

test('session list rows show status, plan title, repository name, attempt, time and short base; raw ids only in titles', async () => {
  const items = [session('ses_one', {state: 'completed', phase: 'completed', finished_at: T0 + 25_100}), session('ses_two', {state: 'waiting_for_user', attempt: 2, plan_id: 'plan-b', repositories: [{repository_id: 'gh:7', role: 'write', base_sha: 'd'.repeat(40), base_branch: 'dev'}]})];
  const b = setup((m, u) => {
    if (u.startsWith('/api/sessions?')) return {items, next_cursor: null};
    if (u.startsWith('/api/plans/plan-b')) return {id: 'plan-b', title: 'Add a CLI flag'};
  }, '#/sessions');
  const ui = await open(b);
  const rows = ui.panel.querySelectorAll('.session-row');
  assert.equal(rows.length, 2);
  assert.equal(rows[0].querySelector('.badge').textContent, 'completed');
  const title = rows[0].querySelector('a.session-row-title');
  assert.equal(title.textContent, 'Fix the rounding bug'); assert.equal(title.getAttribute('href'), '#/sessions/ses_one'); assert.match(title.getAttribute('title'), /ses_one/);
  assert.equal(parts(rows[0].querySelector('.session-row-meta')), 'demo | attempt 1 | 1 min ago | ran 25 s | main@ccccccc');
  assert.match(visibleText(rows[1]), /needs your input.*Add a CLI flag.*tools.attempt 2/); assert.ok(rows[1].classList.contains('needs-input'));
  assert.doesNotMatch(visibleText(ui.panel.querySelector('.session-list')), /ses_|gh:|pv-a|plan-a|cccccccccc/);
  assert.equal(ui.panel.querySelectorAll('.session-row-meta .session-meta-sep[aria-hidden="true"]').length, 8);
  // No count sentence unless filtering.
  assert.equal(ui.panel.querySelector('.session-status').textContent, '');
  ui.close();
});

test('the compact toolbar filters by text, state and repository, and only then reports a count', async () => {
  const items = [session('s1', {state: 'completed'}), session('s2', {state: 'running', repositories: [{repository_id: 'gh:7', role: 'write'}]})];
  const b = setup((m, u) => u.startsWith('/api/sessions?') ? {items, next_cursor: null} : undefined, '#/sessions');
  const ui = await open(b);
  const bar = ui.panel.querySelector('form.session-toolbar');
  assert.equal(bar.getAttribute('role'), 'search');
  const repo = field(bar, 'Repository');
  assert.deepEqual(repo.querySelectorAll('option').map(o => [o.value, o.textContent]), [['', 'All repositories'], ['gh:42', 'octo/demo'], ['gh:7', 'octo/tools']]);
  repo.value = 'gh:7'; repo.dispatchEvent({type: 'change', bubbles: true});
  assert.deepEqual(ui.panel.querySelectorAll('a.session-row-title').map(a => a.getAttribute('href')), ['#/sessions/s2']);
  assert.equal(ui.panel.querySelector('.session-status').textContent, '1 of 2 loaded sessions match.');
  repo.value = ''; repo.dispatchEvent({type: 'change', bubbles: true});
  const state = field(bar, 'Session state'); state.value = 'terminal'; state.dispatchEvent({type: 'change', bubbles: true});
  assert.deepEqual(ui.panel.querySelectorAll('a.session-row-title').map(a => a.getAttribute('href')), ['#/sessions/s1']);
  state.value = ''; state.dispatchEvent({type: 'change', bubbles: true});
  input(field(bar, 'Search sessions'), 'rounding'); assert.equal(ui.panel.querySelectorAll('.session-row').length, 2, 'plan titles are searchable');
  input(field(bar, 'Search sessions'), 'nothing-matches'); assert.match(visibleText(ui.panel.querySelector('.session-empty')), /No loaded sessions match these filters/);
  assert.ok(button(bar, 'Reload')); assert.equal(button(bar, 'Reload').getAttribute('aria-label'), 'Reload sessions');
  assert.ok(field(bar, 'Created on or after').closest('details'), 'the date filter is folded'); ui.close();
});

test('an empty session history is calm and points to Ask', async () => {
  const b = setup(() => undefined, '#/sessions');
  const ui = await open(b);
  assert.match(visibleText(ui.panel.querySelector('.session-empty')), /No sessions yet/);
  assert.ok(ui.panel.querySelector('a[href="#/ask"]')); assert.equal(ui.panel.querySelector('.session-status').textContent, ''); ui.close();
});

test('the rail lists recent sessions by plan title with repository · attempt · status', async () => {
  const recent = []; const shell = {setHeader() {}, setRecent(items, opts) { recent.push({items, opts}); }};
  const b = setup((m, u) => u.startsWith('/api/sessions?') ? {items: [session('s1', {state: 'cancelled', attempt: 2})], next_cursor: null} : undefined, '#/sessions');
  const ui = await open(b, {shell});
  const last = recent.at(-1);
  assert.equal(last.opts.heading, 'Recent sessions');
  assert.deepEqual(last.items[0], {href: '#/sessions/s1', title: 'Fix the rounding bug', tooltip: 'Fix the rounding bug', meta: 'demo · attempt 2 · cancelled', attention: false});
  ui.close();
});

test('durations read at a glance', () => {
  assert.equal(duration(0.2), 'under 1 s'); assert.equal(duration(25), '25 s'); assert.equal(duration(250), '4 min 10 s'); assert.equal(duration(120), '2 min'); assert.equal(duration(7500), '2 h 5 min'); assert.equal(duration(null), null);
});

// ---- Review round 1 ----

test('every terminal state wins over a recorded stop request in the badge', async () => {
  const {phaseBadge} = await import('../../opendevengine/web/sessions.js');
  for (const [state, label] of [['failed', 'failed'], ['interrupted', 'interrupted'], ['blocked', 'blocked'], ['cancelled', 'cancelled'], ['completed', 'completed']]) {
    assert.equal(phaseBadge(session('s', {state, stop_requested: true})).label, label, state);
  }
  assert.equal(phaseBadge(session('s', {state: 'running', stop_requested: true})).label, 'stopping');
  const b = setup((m, u) => u === '/api/sessions/s' ? session('s', {state: 'interrupted', stop_requested: true, runner: {containment: 'confirmed'}}) : undefined);
  const ui = await open(b);
  assert.equal(ui.panel.querySelector('.session-phase').textContent, 'interrupted'); ui.close();
});

test('a failed child read degrades the indicator and keeps Reconnect until a refresh fully succeeds', async () => {
  let failing = true;
  const b = setup((m, u) => { if (failing && u.endsWith('/validations')) return {status: 503, body: {code: 'upstream_unavailable'}}; });
  const ui = await open(b);
  const header = ui.panel.querySelector('.session-header'), indicator = header.querySelector('.session-live');
  assert.equal(indicator.dataset.state, 'reconnecting'); assert.match(visibleText(indicator), /^Reconnecting… · some saved state unavailable$/);
  assert.equal(button(header, 'Reconnect').hidden, false); assert.match(visibleText(header.querySelector('.session-status')), /Some saved state is unavailable: upstream.unavailable\. Reconnect retries/);
  failing = false; button(header, 'Reconnect').click(); await settle();
  assert.equal(visibleText(indicator), 'Live'); assert.equal(button(header, 'Reconnect').hidden, true);
  assert.equal(header.querySelector('.session-status').textContent, '', 'the stale reconnect advice clears'); ui.close();
});

test('a plan title still loading when the reader leaves and returns is awaited, not treated as missing', async () => {
  let release; let reads = 0;
  const b = setup((m, u) => {
    if (u.startsWith('/api/sessions?')) return {items: [session('s', {state: 'completed'})], next_cursor: null};
    if (u === '/api/plans/plan-a') { reads += 1; return new Promise(r => { release = r; }); }
    // The detail page's own versioned plan read never answers, so only the list read can supply the title.
    if (u.startsWith('/api/plans/plan-a?')) return new Promise(() => {});
  }, '#/sessions');
  const ui = await open(b);
  assert.equal(ui.panel.querySelector('a.session-row-title').textContent, 'Saved plan');
  b.window.location.hash = '#/sessions/s'; await settle();
  b.window.location.hash = '#/sessions'; await settle(); await settle();
  assert.equal(reads, 1, 'the pending read is reused, not repeated');
  release(plan); await settle(); await settle();
  assert.equal(ui.panel.querySelector('a.session-row-title').textContent, 'Fix the rounding bug'); ui.close();
});

test('a failed plan title read is retried by the next load', async () => {
  let fail = true; let reads = 0;
  const b = setup((m, u) => {
    if (u.startsWith('/api/sessions?')) return {items: [session('s', {state: 'completed'})], next_cursor: null};
    if (u === '/api/plans/plan-a') { reads += 1; return fail ? {status: 503, body: {code: 'upstream_unavailable'}} : plan; }
  }, '#/sessions');
  const ui = await open(b);
  assert.equal(ui.panel.querySelector('a.session-row-title').textContent, 'Saved plan');
  fail = false; button(ui.panel, 'Reload').click(); await settle(); await settle();
  assert.equal(reads, 2); assert.equal(ui.panel.querySelector('a.session-row-title').textContent, 'Fix the rounding bug'); ui.close();
});

test('a check the plan did not approve is shown as skipped, not failed', async () => {
  const checks = {fully_validated: false, skipped_not_approved: 1, summary: {not_run: 1}, items: [{ordinal: 1, status: 'not_run', required: false, exit_code: null, command: {argv: ['sh', '-c', 'npm test'], command: 'npm test', expected: ''}, source: 'proposed', output: null, started_at: null, finished_at: null, environment: {reason: 'not_approved', network: 'none'}}]};
  const b = setup((m, u) => u.endsWith('/validations') ? checks : undefined);
  const ui = await open(b);
  b.document.getElementById('session-tab-2').click();
  const panel = b.document.getElementById('session-tabpanel-2');
  assert.match(visibleText(panel.querySelector('.check-item')), /Skipped: not approved in the plan\./); ui.close();
});
