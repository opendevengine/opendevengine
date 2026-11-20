// Chat-style Ask: landing composer creates a titled conversation, mid-conversation scope, recent titles.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { createBrowser, input, install, keydown, settle, visibleText } from "./dom.mjs";
import { createAskPanel, titleFrom } from "../../opendevengine/web/ask.js";
import { declaredStyle } from "./css.mjs";

const html = readFileSync(new URL("../../opendevengine/web/index.html", import.meta.url), "utf8");
const storage = () => { const map = new Map(); return { getItem: (k) => map.get(k) ?? null, setItem: (k, v) => map.set(k, v), removeItem: (k) => map.delete(k) }; };
const ctx = (id, name) => ({ repository_id: id, name: `acme-dev/${name}`, kind: "github", eligible: true, status: "ready", active_snapshot: { mode: "lexical_only", coverage: { files: 3 } }, active_run: null });
const REPOS = { "gh:1": ctx("gh:1", "storefront-app"), "gh:2": ctx("gh:2", "deploy-tools"), "gh:3": ctx("gh:3", "cube-workbench"), "gh:4": ctx("gh:4", "edgemesh"), "gh:5": ctx("gh:5", "mysociety") };

async function boot(respond, { hash = "#/ask" } = {}) {
  const b = createBrowser(html, { hash, respond });
  b.window.sessionStorage = storage();
  b.window.confirm = () => true;
  install(b);
  globalThis.sessionStorage = b.window.sessionStorage;
  globalThis.localStorage = storage();
  const recent = [];
  const headers = [];
  const shell = { setRecent: (items) => recent.splice(0, recent.length, ...items), setHeader: (h) => headers.push(h) };
  const ui = await createAskPanel({ host: b.document.getElementById("view-ask"), shell });
  await settle();
  return { ...b, ui, recent, headers, button: (t) => ui.panel.querySelectorAll("button").find((n) => n.textContent === t) };
}

test("titleFrom trims to 60 characters at a word boundary", () => {
  assert.equal(titleFrom("What does storefront-app do and how is it structured?"), "What does storefront-app do and how is it structured?");
  assert.equal(titleFrom("Explain how the deploy manager schedules rollouts across every edge region we run"), "Explain how the deploy manager schedules rollouts across…");
});

test("first send from the landing page creates a titled conversation with chosen repos, navigates and sends", async () => {
  const posts = [];
  let conversation = null;
  const b = await boot((m, u, o) => {
    if (u === "/api/conversations" && m === "GET") return { items: conversation ? [conversation] : [] };
    if (u === "/api/conversations" && m === "POST") { posts.push(JSON.parse(o.body)); conversation = { id: "conv_new", title: posts[0].title, scope_revision: 1, repository_ids: posts[0].repository_ids, messages: [], runs: [], plans: [] }; return conversation; }
    if (u === "/api/conversations/conv_new") return conversation;
    if (u === "/api/conversations/conv_new/messages") { posts.push(JSON.parse(o.body)); return { id: "run", state: "queued" }; }
    if (u.startsWith("/api/context/repositories")) return { items: Object.values(REPOS), next_cursor: null };
    const m2 = /^\/api\/repositories\/(.+)\/context$/.exec(u);
    if (m2) return REPOS[decodeURIComponent(m2[1])];
    return { status: 404, body: { code: "not_found" } };
  });
  assert.match(visibleText(b.ui.panel), /What are we working on\?/);
  b.ui.panel.querySelectorAll("button").find((n) => n.getAttribute("aria-label") === "Add repositories").click();
  await settle(30);
  b.document.querySelectorAll(".picker-row").find((r) => visibleText(r).includes("storefront-app")).querySelector("input").click();
  b.document.querySelectorAll("dialog.modal button").find((n) => n.textContent.startsWith("Add")).click();
  await settle();
  const box = b.ui.panel.querySelector("textarea");
  input(box, "What does storefront-app do and how is it structured?");
  keydown(box, "Enter");
  await settle(40);
  assert.deepEqual(posts[0], { title: "What does storefront-app do and how is it structured?", repository_ids: ["gh:1"] });
  assert.equal(b.window.location.hash, "#/ask/conv_new");
  assert.equal(posts[1].content, "What does storefront-app do and how is it structured?");
  assert.equal(posts[1].intent, "answer");
  assert.deepEqual(b.recent.map((r) => r.title), ["What does storefront-app do and how is it structured?"]);
});

test("adding a 5th repository mid-conversation patches scope, logs it and marks the plan stale", async () => {
  let c = { id: "c", title: "Deploy flow", scope_revision: 4, repository_ids: ["gh:1", "gh:2", "gh:3", "gh:4"], runs: [],
    messages: [{ id: "m1", role: "user", content: "Plan it", state: "complete", created_at: 1 }], plans: [{ id: "plan_1", title: "Deploy plan", latest_version_id: "v1", created_at: 2 }] };
  const patches = [];
  const b = await boot((m, u, o) => {
    if (u === "/api/conversations") return { items: [c] };
    if (u === "/api/conversations/c") { if (m === "PATCH") { patches.push(JSON.parse(o.body)); c = { ...c, repository_ids: patches.at(-1).repository_ids, scope_revision: 5 }; } return c; }
    if (u === "/api/plans/plan_1") return { id: "plan_1", latest_version_id: "v1", version: { version: 1, content: { steps: [{}, {}] }, readiness: { state: "ready", blocking_reasons: [] } } };
    if (u.startsWith("/api/context/repositories")) { const q = new URL(u, "http://x").searchParams.get("query") ?? ""; return { items: Object.values(REPOS).filter((r) => r.name.includes(q)), next_cursor: null }; }
    const m2 = /^\/api\/repositories\/(.+)\/context$/.exec(u);
    if (m2) return REPOS[decodeURIComponent(m2[1])];
    return { status: 404, body: { code: "not_found" } };
  }, { hash: "#/ask/c" });
  await settle(20);
  const box = b.ui.panel.querySelector("textarea");
  input(box, "Also check @mysoc");
  await new Promise((resolve) => setTimeout(resolve, 200));
  await settle();
  keydown(box, "Enter");
  await settle(40);
  assert.deepEqual(patches, [{ repository_ids: ["gh:1", "gh:2", "gh:3", "gh:4", "gh:5"], expected_revision: 4 }]);
  const text = visibleText(b.ui.panel);
  assert.match(text, /Added mysociety/);
  assert.match(text, /Made before mysociety was added · Regenerate/);
  assert.deepEqual(b.ui.panel.querySelectorAll(".chip-repo .chip-label").map((n) => n.textContent), ["storefront-app", "deploy-tools", "cube-workbench", "edgemesh", "mysociety"]);
  assert.deepEqual(b.headers.at(-1).pills.slice(0, 5).map((p) => p.text), ["storefront-app", "deploy-tools", "cube-workbench", "edgemesh", "mysociety"]);
});

test("recent list shows titles and falls back to Untitled, never ids", async () => {
  const b = await boot((m, u) => {
    if (u === "/api/conversations") return { items: [{ id: "conv_1", title: "Deploy flow", updated_at: Date.now() - 120000 }, { id: "conv_2", title: "", updated_at: Date.now() - 7200000 }] };
    return { status: 404, body: { code: "not_found" } };
  });
  assert.deepEqual(b.recent.map((r) => r.title), ["Deploy flow", "Untitled · 2 h ago"]);
  assert.deepEqual(b.recent.map((r) => r.href), ["#/ask/conv_1", "#/ask/conv_2"]);
});

test("unassigned model roles disable Ask and Generate plan before sending, on the landing page and in a conversation", async () => {
  const conversation = { id: "conv_1", title: "Deploy flow", scope_revision: 1, repository_ids: [], messages: [], runs: [], plans: [] };
  const roles = { settings: { local_only: true }, roles: { ask: { status: "unassigned" }, planning: { status: "ready", model: "sonnet", destination: { kind: "local" } } } };
  const respond = (m, u) => {
    if (u === "/api/conversations") return { items: [conversation] };
    if (u === "/api/conversations/conv_1") return conversation;
    if (u === "/api/settings/model-roles") return roles;
    return { status: 404, body: { code: "not_found" } };
  };
  for (const hash of ["#/ask", "#/ask/conv_1"]) {
    const b = await boot(respond, { hash });
    await settle(10);
    assert.equal(b.button("Ask").disabled, true, hash);
    assert.equal(b.button("Generate plan").disabled, false, hash);
    assert.match(visibleText(b.ui.panel), /No model is set for answering questions\. Choose a model in Settings/, hash);
  }
  roles.roles.planning = { status: "unassigned" };
  const b = await boot(respond, { hash: "#/ask/conv_1" });
  await settle(10);
  assert.equal(b.button("Generate plan").disabled, true);
  assert.match(visibleText(b.ui.panel), /No model is set for answering questions or planning\./);
});

test("citations open in the side panel when Ask has one", async () => {
  const cite = { repository_id: "gh:1", snapshot_id: "s", revision: "d8f5918f985503cd5a2fd0c0956abd32b4800f3c", path: "src/lib/api.ts", start_line: 2, end_line: 2 };
  const c = { id: "c", title: "T", scope_revision: 1, repository_ids: ["gh:1"], runs: [], plans: [], messages: [{ id: "m", role: "assistant", state: "complete", content: "See", citations: [cite], created_at: 1 }] };
  const b = createBrowser(html, { hash: "#/ask/c", respond: (m, u) => {
    if (u === "/api/conversations") return { items: [c] };
    if (u === "/api/conversations/c") return c;
    if (u === "/api/repositories/gh%3A1/context") return { ...REPOS["gh:1"], active_snapshot: { mode: "lexical_only", branch: "main" } };
    if (u.startsWith("/api/context/source")) return { revision: cite.revision, path: cite.path, first_line: 1, lines: ["a", "b", "c"], total_lines: 3 };
    return { status: 404, body: {} };
  } });
  b.window.sessionStorage = storage();
  install(b);
  const { createSidePanel } = await import("../../opendevengine/web/panel.js");
  const sidePanel = createSidePanel(b.document.getElementById("side-panel"));
  const ui = await createAskPanel({ host: b.document.getElementById("view-ask"), sidePanel });
  await settle();
  ui.panel.querySelector("button.cite").click();
  await settle();
  const side = b.document.getElementById("side-panel");
  assert.equal(side.hidden, false);
  assert.match(visibleText(side), /acme-dev\/storefront-app · main @ d8f5918/);
  assert.equal(side.querySelectorAll(".code-line.hl").length, 1);
});

// Text a sighted user sees: visually hidden live regions are announced, not shown.
const shownText = (node) => node.nodeType === 3 ? node.data : node.hidden || node.classList?.contains("sr-only") ? "" : node.childNodes.map(shownText).join("").replace(/\s+/g, " ");

async function noMatchThenChoose(edit) {
  const posts = [];
  let conversation = null;
  const b = await boot((m, u, o) => {
    if (u === "/api/conversations" && m === "GET") return { items: conversation ? [conversation] : [] };
    if (u === "/api/conversations" && m === "POST") { conversation = { id: "conv_nm", title: JSON.parse(o.body).title, scope_revision: 1, repository_ids: [], messages: [], runs: [], plans: [] }; return conversation; }
    if (u === "/api/conversations/conv_nm") { if (m === "PATCH") conversation = { ...conversation, repository_ids: JSON.parse(o.body).repository_ids, scope_revision: 2 }; return conversation; }
    if (u === "/api/route") return { status: "no_match", candidates: [], selected_repository_ids: [] };
    if (u === "/api/conversations/conv_nm/messages") { posts.push(JSON.parse(o.body)); return { id: "run", state: "queued" }; }
    if (u.startsWith("/api/context/repositories")) return { items: Object.values(REPOS), next_cursor: null };
    const m2 = /^\/api\/repositories\/(.+)\/context$/.exec(u);
    if (m2) return REPOS[decodeURIComponent(m2[1])];
    return { status: 404, body: { code: "not_found" } };
  });
  input(b.ui.panel.querySelector("textarea"), "How does checkout create an order?");
  keydown(b.ui.panel.querySelector("textarea"), "Enter");
  await settle(60);
  assert.equal(b.window.location.hash, "#/ask/conv_nm");
  const before = { shown: shownText(b.ui.panel), announced: b.ui.panel.querySelectorAll("[aria-live]").map((n) => n.textContent).join(" ") };
  if (edit !== undefined) input(b.ui.panel.querySelector("textarea"), edit);
  b.button("Choose…").click();
  await settle(30);
  b.document.querySelectorAll(".picker-row").find((r) => visibleText(r).includes("storefront-app")).querySelector("input").click();
  b.document.querySelectorAll("dialog.modal button").find((n) => n.textContent.startsWith("Add")).click();
  await settle(60);
  return { b, posts, before };
}

test("choosing repositories after no match clears the routing notice and sends the waiting question", async () => {
  const { b, posts, before } = await noMatchThenChoose();
  assert.match(before.shown, /No repository matched this question\./);
  assert.equal(before.shown.match(/No repository matched/g).length, 1, "routing notice is shown once, not repeated as a status line");
  assert.match(before.announced, /No repository matched/, "the routing outcome is still announced to screen readers");
  assert.doesNotMatch(shownText(b.ui.panel), /No repository matched/);
  assert.equal(posts.length, 1);
  assert.equal(posts[0].content, "How does checkout create an order?");
  assert.equal(posts[0].intent, "answer");
});

test("choosing repositories after no match sends the question as edited, and nothing once it is cleared", async () => {
  const edited = await noMatchThenChoose("How is the order saved?");
  assert.deepEqual(edited.posts.map((p) => p.content), ["How is the order saved?"]);
  const cleared = await noMatchThenChoose("");
  assert.equal(cleared.posts.length, 0);
  assert.doesNotMatch(shownText(cleared.b.ui.panel), /No repository matched/);
});

test("submitting while a resumed question is still sending keeps the text, even when the send then fails", async () => {
  let reject = null;
  let conversation = null;
  const b = await boot((m, u, o) => {
    if (u === "/api/conversations" && m === "GET") return { items: conversation ? [conversation] : [] };
    if (u === "/api/conversations" && m === "POST") { conversation = { id: "conv_nm", title: JSON.parse(o.body).title, scope_revision: 1, repository_ids: [], messages: [], runs: [], plans: [] }; return conversation; }
    if (u === "/api/conversations/conv_nm") { if (m === "PATCH") conversation = { ...conversation, repository_ids: JSON.parse(o.body).repository_ids, scope_revision: 2 }; return conversation; }
    if (u === "/api/route") return { status: "no_match", candidates: [], selected_repository_ids: [] };
    if (u === "/api/conversations/conv_nm/messages") return new Promise((resolve) => { reject = () => resolve({ status: 400, body: { code: "invalid", error: "Rejected." } }); });
    if (u.startsWith("/api/context/repositories")) return { items: Object.values(REPOS), next_cursor: null };
    const m2 = /^\/api\/repositories\/(.+)\/context$/.exec(u);
    if (m2) return REPOS[decodeURIComponent(m2[1])];
    return { status: 404, body: { code: "not_found" } };
  });
  const box = () => b.ui.panel.querySelector("textarea");
  input(box(), "How does checkout create an order?");
  keydown(box(), "Enter");
  await settle(60);
  b.button("Choose…").click();
  await settle(30);
  b.document.querySelectorAll(".picker-row").find((r) => visibleText(r).includes("storefront-app")).querySelector("input").click();
  b.document.querySelectorAll("dialog.modal button").find((n) => n.textContent.startsWith("Add")).click();
  await settle(60);
  assert.ok(reject, "the resumed question is being sent");
  keydown(box(), "Enter");
  await settle(20);
  assert.equal(box().value, "How does checkout create an order?", "a skipped submit is not treated as sent");
  reject();
  await settle(60);
  assert.equal(box().value, "How does checkout create an order?", "the question survives the failed send");
});

async function holdSend({ scoped }) {
  let release = null;
  let conversation = { id: "conv_h", title: "Held", scope_revision: 1, repository_ids: scoped ? ["gh:1"] : [], messages: [], runs: [], plans: [] };
  const b = await boot((m, u, o) => {
    if (u === "/api/conversations" && m === "GET") return { items: [conversation] };
    if (u === "/api/conversations/conv_h") { if (m === "PATCH") conversation = { ...conversation, repository_ids: JSON.parse(o.body).repository_ids, scope_revision: 2 }; return conversation; }
    if (u === "/api/route") return { status: "no_match", candidates: [], selected_repository_ids: [] };
    if (u === "/api/conversations/conv_h/messages") return new Promise((resolve) => { release = () => { conversation = { ...conversation, messages: [{ id: "m1", role: "user", content: JSON.parse(o.body).content, state: "complete", created_at: 1 }] }; resolve({ id: "run", state: "queued" }); }; });
    if (u.startsWith("/api/context/repositories")) return { items: Object.values(REPOS), next_cursor: null };
    const m2 = /^\/api\/repositories\/(.+)\/context$/.exec(u);
    if (m2) return REPOS[decodeURIComponent(m2[1])];
    return { status: 404, body: { code: "not_found" } };
  }, { hash: "#/ask/conv_h" });
  await settle(20);
  return { b, box: () => b.ui.panel.querySelector("textarea"), release: () => release(), sent: () => release !== null };
}

test("text typed while a send is in flight survives the successful send", async () => {
  const h = await holdSend({ scoped: true });
  input(h.box(), "First question");
  keydown(h.box(), "Enter");
  await settle(20);
  assert.ok(h.sent());
  input(h.box(), "Follow-up typed while sending");
  h.release();
  await settle(60);
  assert.equal(h.box().value, "Follow-up typed while sending");
  assert.equal(h.b.window.sessionStorage.getItem("ask-draft:conv_h"), "Follow-up typed while sending");
});

test("text typed while a resumed question is sending survives the successful send", async () => {
  const h = await holdSend({ scoped: false });
  input(h.box(), "First question");
  keydown(h.box(), "Enter");
  await settle(40);
  h.b.button("Choose…").click();
  await settle(30);
  h.b.document.querySelectorAll(".picker-row").find((r) => visibleText(r).includes("storefront-app")).querySelector("input").click();
  h.b.document.querySelectorAll("dialog.modal button").find((n) => n.textContent.startsWith("Add")).click();
  await settle(60);
  assert.ok(h.sent());
  input(h.box(), "Follow-up typed while sending");
  h.release();
  await settle(60);
  assert.equal(h.box().value, "Follow-up typed while sending");
  assert.equal(h.b.window.sessionStorage.getItem("ask-draft:conv_h"), "Follow-up typed while sending");
});

test("a send that finishes after the draft was discarded by leaving does not bring it back", async () => {
  const h = await holdSend({ scoped: true });
  input(h.box(), "First question");
  keydown(h.box(), "Enter");
  await settle(20);
  input(h.box(), "Discarded follow-up");
  h.b.window.location.hash = "#/ask";
  await settle(40);
  assert.equal(h.b.window.sessionStorage.getItem("ask-draft:conv_h"), null);
  h.release();
  await settle(60);
  assert.equal(h.b.window.sessionStorage.getItem("ask-draft:conv_h"), null);
});

test("focusing the Ask heading overlays it instead of shifting the page (a shift swallowed the next click)", async () => {
  const css = readFileSync(new URL("../../opendevengine/web/styles.css", import.meta.url), "utf8");
  const b = await boot((m, u) => u === "/api/conversations" ? { items: [] } : { status: 404, body: { code: "not_found" } });
  const title = b.ui.panel.querySelector("#persistent-ask-title");
  const focused = declaredStyle(title, css, { focused: true });
  assert.equal(focused.position, "absolute", "focused heading stays out of the layout flow");
  assert.match(focused.outline || "", /solid/);
});

test("the landing hint is styled to the composer's width, centered under it", async () => {
  const css = readFileSync(new URL("../../opendevengine/web/styles.css", import.meta.url), "utf8");
  const b = await boot((m, u) => u === "/api/conversations" ? { items: [] } : { status: 404, body: { code: "not_found" } });
  const hint = b.ui.panel.querySelectorAll("p").find((p) => p.textContent.startsWith("Pick repositories"));
  const composer = b.ui.panel.querySelector(".composer-v2");
  assert.ok(hint.classList.contains("landing-hint"), "hint carries its class");
  const style = declaredStyle(hint, css);
  assert.equal(style.width, declaredStyle(composer, css).width);
  assert.equal(style["text-align"], "center");
});

test("conversation Start launches once, retries a lost response with the same key, and navigates", async () => {
  const c = {id:'launch-c', repository_ids:[], messages:[], runs:[], plans:[{id:'launch-p', latest_version_id:'launch-v'}]};
  const posts = [];
  let fail = true, release;
  const b = await boot((method, url, options) => {
    if (url === '/api/conversations') return {items:[c]};
    if (url === '/api/conversations/launch-c') return c;
    if (url.startsWith('/api/plans/launch-p')) return {version:{id:'launch-v',content_hash:'saved-hash',version:1,content:{steps:[]},readiness:{state:'ready'}}};
    if (url === '/api/sessions') {
      posts.push(JSON.parse(options.body));
      if (fail) return new Promise((resolve, reject) => { release = () => reject(new Error('Network connection lost')); });
      return {session_id:'session-created'};
    }
    return {status:404,body:{code:'not_found'}};
  }, {hash:'#/ask/launch-c'});
  const start = b.button('Start session');
  start.click(); start.click();
  await settle();
  assert.equal(posts.length, 1);
  release(); await settle();
  assert.match(visibleText(b.ui.panel), /Couldn't start session: Network connection lost/);
  fail = false;
  start.click(); await settle();
  assert.equal(posts.length, 2);
  assert.deepEqual(posts[1], posts[0]);
  assert.equal(posts[0].plan_version_id, 'launch-v');
  assert.equal(posts[0].content_hash, 'saved-hash');
  assert.ok(posts[0].idempotency_key);
  assert.equal(b.window.location.hash, '#/sessions/session-created');
});
