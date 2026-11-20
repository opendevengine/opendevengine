// Agent messages render a small, safe Markdown subset (ui.js renderMarkdown): elements and text
// nodes only, so HTML-like input stays literal text and links never become anchors.
import assert from 'node:assert/strict';
import {test} from 'node:test';
import {readFileSync} from 'node:fs';
import {createBrowser, install, settle} from './dom.mjs';
import {renderMarkdown, MARKDOWN_LIMITS} from '../../opendevengine/web/ui.js';
import {createSessionPanel} from '../../opendevengine/web/sessions.js';

function md(text) {
  install(createBrowser('<!doctype html><html><body></body></html>', {respond: () => ({status: 404, body: {}})}));
  return renderMarkdown(text);
}
const tags = root => root.children.map(n => n.tagName.toLowerCase());

test('paragraphs split on blank lines and keep single line breaks', () => {
  const root = md('First line\nsecond line\n\nNext paragraph');
  assert.ok(root.classList.contains('md'));
  assert.deepEqual(tags(root), ['p', 'p']);
  assert.equal(root.children[0].textContent, 'First line\nsecond line'); assert.equal(root.children[1].textContent, 'Next paragraph');
});

test('bold and inline code become strong and code; code inside bold is still code', () => {
  const p = md('**Change:** edit `calc.py:2` and **keep `average()`** as is').children[0];
  assert.deepEqual(p.querySelectorAll('strong').map(n => n.textContent), ['Change:', 'keep average()']);
  assert.deepEqual(p.querySelectorAll('code').map(n => n.textContent), ['calc.py:2', 'average()']);
  assert.equal(p.textContent, 'Change: edit calc.py:2 and keep average() as is');
});

test('a ** inside inline code stays literal, and unmatched markers stay as written', () => {
  const p = md('Use `a ** b` for powers; 2 * 3 and **unclosed').children[0];
  assert.equal(p.querySelector('code').textContent, 'a ** b'); assert.equal(p.querySelector('strong'), null);
  assert.equal(p.textContent, 'Use a ** b for powers; 2 * 3 and **unclosed');
});

test('fenced code blocks keep their text exactly, including Markdown and HTML inside', () => {
  const root = md('Ran:\n```sh\npython -c "print(\'ok\')"\n**not bold** <b>x</b>\n```\nDone.');
  assert.deepEqual(tags(root), ['p', 'pre', 'p']);
  const code = root.querySelector('pre > code');
  assert.equal(code.textContent, 'python -c "print(\'ok\')"\n**not bold** <b>x</b>');
  assert.equal(code.children.length, 0, 'no elements inside a code block');
  assert.equal(md('```\nunclosed\nstill code').querySelector('pre code').textContent, 'unclosed\nstill code');
  assert.equal(md('~~~~\na\n~~~\nb\n~~~~').querySelector('pre code').textContent, 'a\n~~~\nb', 'a shorter fence does not close a longer one');
});

test('unordered and ordered lists, with inline formatting and a non-1 start', () => {
  const root = md('Changes:\n- `calc.py:2`: fixed **add**\n* average() unchanged\n\n3. third\n4) fourth');
  assert.deepEqual(tags(root), ['p', 'ul', 'ol']);
  const [ul, ol] = [root.querySelector('ul'), root.querySelector('ol')];
  assert.deepEqual(ul.querySelectorAll('li').map(li => li.textContent), ['calc.py:2: fixed add', 'average() unchanged']);
  assert.equal(ul.querySelector('li code').textContent, 'calc.py:2'); assert.equal(ul.querySelector('li strong').textContent, 'add');
  assert.equal(ol.getAttribute('start'), '3'); assert.deepEqual(ol.querySelectorAll('li').map(li => li.textContent), ['third', 'fourth']);
  assert.equal(md('1. one\n2. two').querySelector('ol').hasAttribute('start'), false);
});

test('links, headings and other syntax stay literal text; no anchors are created', () => {
  const root = md('# Heading\nSee [the docs](https://example.com/x) and <https://example.com>\n> quote');
  assert.equal(root.querySelector('a'), null); assert.equal(root.querySelector('h1'), null); assert.equal(root.querySelector('blockquote'), null);
  assert.equal(root.textContent, '# Heading\nSee [the docs](https://example.com/x) and <https://example.com>\n> quote');
});

test('HTML-like input stays text: no img, script or other elements are created from it', () => {
  const input = '<img src=x onerror=alert(1)>\n<script>alert(1)</script>\n- <b>item</b>\n**<i>bold</i>**';
  const root = md(input);
  for (const tag of ['img', 'script', 'b', 'i']) assert.equal(root.querySelector(tag), null, tag);
  assert.match(root.textContent, /<img src=x onerror=alert\(1\)>/); assert.match(root.textContent, /<script>alert\(1\)<\/script>/);
  assert.equal(root.querySelector('li').textContent, '<b>item</b>'); assert.equal(root.querySelector('strong').textContent, '<i>bold</i>');
});

test('the session result card renders the agent\'s final message as safe Markdown', async () => {
  const html = readFileSync(new URL('../../opendevengine/web/index.html', import.meta.url), 'utf8');
  const text = '**Change:**\n- `calc.py:2`: return a + b\n\n```\npython -c "ok"\n```\n<img src=x onerror=alert(1)>';
  const s = {id: 's', plan_id: 'p', plan_version_id: 'v', attempt: 1, state: 'completed', phase: 'completed', source_available: true, repositories: [], runner: {containment: 'confirmed'}, capabilities: {}, usage: {}, outcome: {}, publication: {}, created_at: Date.now() - 5000, pending_question: null};
  const b = createBrowser(html, {hash: '#/sessions/s', respond: (m, u) => {
    if (u === '/api/sessions/s') return s;
    if (u.includes('/events?')) return {items: [{session_id: 's', seq: 1, type: 'agent.summary', payload: {kind: 'result', status: 'completed', text}, created_at: Date.now()}], next_seq: 1, has_more: false};
    if (u.includes('/messages?')) return {items: [], next_cursor: null};
    if (u.endsWith('/validations')) return {items: [], fully_validated: false};
    return {status: 404, body: {code: 'not_found'}};
  }});
  install(b); b.document.getElementById('view-sessions').hidden = false;
  const data = new Map(); b.window.sessionStorage = {getItem: k => data.get(k), setItem: (k, v) => data.set(k, v), removeItem: k => data.delete(k)};
  const request = async (path, method = 'GET') => { const r = await b.window.fetch(path, {method}); const v = await r.json(); if (!r.ok) { const e = new Error(v.code); e.status = r.status; e.code = v.code; throw e; } return v; };
  const ui = createSessionPanel({host: b.document.getElementById('sessions-content'), request, pollMs: 1e6}); ui.open(); await settle(); await settle();
  const final = ui.panel.querySelector('.session-result .agent-final .md');
  assert.equal(final.querySelector('strong').textContent, 'Change:'); assert.equal(final.querySelector('li code').textContent, 'calc.py:2');
  assert.equal(final.querySelector('pre code').textContent, 'python -c "ok"');
  assert.equal(ui.panel.querySelector('img'), null); assert.match(final.textContent, /<img src=x onerror=alert\(1\)>/);
  assert.ok(ui.panel.querySelector('.session-activity .agent-message.is-clamped .md'), 'the timeline preview uses the same renderer'); ui.close();
});

// ---- Review round 3: linear parsing and output caps ----

test('inline parsing stays linear on adversarial input (caps lifted so the parser itself is measured)', () => {
  const saved = {...MARKDOWN_LIMITS};
  Object.assign(MARKDOWN_LIMITS, {chars: 1e7, nodes: 1e7});
  try {
    md('warm up **a** `b`');
    for (const unit of [' **a', '**a ', '`', ' `a', '** ', '**`a']) {
      const input = unit.repeat(Math.ceil(65536 / unit.length));
      // Best of three: other test files run in parallel, so one sample can absorb a GC pause.
      let root, ms = Infinity;
      for (let i = 0; i < 3; i++) { const started = performance.now(); root = md(input); ms = Math.min(ms, performance.now() - started); }
      // Generous absolute ceiling for shared CI runners; the doubling ratio below is the linearity check.
      assert.ok(ms < 1000, `${JSON.stringify(unit)} × ${input.length} chars took ${ms.toFixed(1)} ms`);
      assert.ok(root.children.length > 0, 'something rendered');
    }
    // Quadrupling the input: linear work grows ~4x, a quadratic scan ~16x. The wide gap keeps the check
    // meaningful on shared CI runners, where GC and memory pressure skew any single doubling.
    // Each sample is a per-call average over >= 40 ms of repeated calls.
    const time = n => { const input = ' **a'.repeat(n); let best = Infinity;
      for (let i = 0; i < 3; i++) {
        let calls = 0; const t = performance.now();
        do { md(input); calls++; } while (performance.now() - t < 40);
        best = Math.min(best, (performance.now() - t) / calls);
      }
      return best; };
    time(8192); const small = Math.max(time(16384), 0.5), large = time(65536);
    assert.ok(large < small * 10, `16k→64k units: ${small.toFixed(1)} ms → ${large.toFixed(1)} ms`);
  } finally { Object.assign(MARKDOWN_LIMITS, saved); }
});

test('a long bullet message is capped by node count with a visible marker and the full text as plain text', () => {
  const source = '- item\n'.repeat(10000);
  const root = md(source);
  const items = root.querySelectorAll('li').length;
  assert.ok(items > 0 && items <= MARKDOWN_LIMITS.nodes, `${items} list items`);
  const all = root.querySelectorAll('*').length;
  assert.ok(all <= MARKDOWN_LIMITS.nodes + 6, `${all} elements`);
  assert.equal(root.querySelector('.md-truncated').textContent, '… message truncated');
  const full = root.querySelector('details.md-full');
  assert.equal(full.open, false); assert.equal(full.querySelector('summary').textContent, 'Show the full message as plain text');
  assert.equal(full.querySelector('pre').textContent, source.replace(/\r\n?/g, '\n')); assert.equal(full.querySelector('pre').children.length, 0);
});

test('a message over the character cap renders only the cap and says it was truncated', () => {
  const source = 'word '.repeat(6000) + 'TAIL';
  const root = md(source);
  const rendered = root.children.filter(n => !n.classList.contains('md-truncated') && !n.classList.contains('md-full')).map(n => n.textContent).join('');
  assert.ok(rendered.length <= MARKDOWN_LIMITS.chars); assert.doesNotMatch(rendered, /TAIL/);
  assert.ok(root.querySelector('.md-truncated')); assert.match(root.querySelector('details.md-full pre').textContent, /TAIL$/);
});

test('ordinary messages are not marked as truncated', () => {
  const root = md('**Change:**\n- one\n- two\n\n```\ncode\n```');
  assert.equal(root.querySelector('.md-truncated'), null); assert.equal(root.querySelector('.md-full'), null);
});

// ---- Review round 4: every emitted node counts against the limit ----

const descendants = node => node.childNodes.reduce((n, child) => n + 1 + (child.childNodes ? descendants(child) : 0), 0);
const NODE_CASES = {
  'a 9,000-line paragraph': 'x\n'.repeat(9000),
  'many short paragraphs': 'short paragraph\n\n'.repeat(5000),
  'a long code block': '```\n' + 'code line\n'.repeat(5000) + '```\n' + 'after\n'.repeat(3000),
  'mixed content': 'Intro **bold** and `code` here\nsecond line\n- item `x` **y**\n- item\n1. one\n2. two\n```\nblock\n```\n\n'.repeat(600),
};

for (const [name, source] of Object.entries(NODE_CASES)) {
  test(`node limit holds for ${name}, including text nodes, line breaks and the marker`, () => {
    for (const chars of [MARKDOWN_LIMITS.chars, 1e7]) {
      const saved = {...MARKDOWN_LIMITS}; Object.assign(MARKDOWN_LIMITS, {chars});
      try {
        const root = md(source), total = descendants(root);
        assert.ok(total <= MARKDOWN_LIMITS.nodes, `${name} (char cap ${chars}): ${total} descendant nodes > ${MARKDOWN_LIMITS.nodes}`);
        assert.ok(root.querySelector('.md-truncated'), `${name}: truncation is shown`);
        assert.equal(root.querySelector('details.md-full pre').textContent, source, `${name}: full text kept as plain text`);
        assert.ok(root.children.length > 2, `${name}: some content rendered before the cap`);
      } finally { Object.assign(MARKDOWN_LIMITS, saved); }
    }
  });
}

test('the reported repro stays within the limit and renders the beginning in order', () => {
  const root = md('x\n'.repeat(9000));
  assert.ok(descendants(root) <= MARKDOWN_LIMITS.nodes, `${descendants(root)} nodes`);
  assert.match(root.querySelector('p').textContent, /^x\nx\nx/);
});

test('a message that fits exactly is not truncated and spends no nodes on a marker', () => {
  const root = md('one\ntwo\n\n- a\n- b');
  assert.equal(root.querySelector('.md-truncated'), null);
  assert.equal(descendants(root), 1 + 3 + 1 + 2 + 2, 'p with two lines and a break; ul with two items');
});
