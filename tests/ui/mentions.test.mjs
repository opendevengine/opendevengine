// @ mentions: inline repository menu on the composer textarea.
import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createBrowser, input, install, keydown, settle, visibleText } from "./dom.mjs";

const url = pathToFileURL(fileURLToPath(new URL("../../opendevengine/web/mentions.js", import.meta.url))).href;
let loads = 0;
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const ctx = (i, name, over = {}) => ({ repository_id: `gh:${i}`, name: `acme-dev/${name}`, kind: "github", eligible: true, status: "ready",
  active_snapshot: { mode: "lexical_only" }, active_run: null, ...over });

async function setup() {
  const b = createBrowser("<!doctype html><html><body><div id=box><textarea id=t></textarea></div></body></html>", { respond: () => ({ status: 404, body: {} }) });
  install(b);
  loads += 1;
  const { attachMentions } = await import(`${url}?m=${loads}`);
  const calls = [];
  const request = async (path) => {
    calls.push(path);
    if (!path.startsWith("/api/context")) return { items: [] };
    return { items: [ctx(1, "storefront-app"), ctx(2, "storefront-legacy", { status: "metadata_only", active_snapshot: null })], next_cursor: null };
  };
  const textarea = b.document.getElementById("t");
  const picked = [];
  const handle = attachMentions(textarea, { request, onPick: (repo) => picked.push(repo) });
  return { b, textarea, picked, calls, handle };
}

async function type(textarea, value) {
  input(textarea, value);
  await wait(190);
  await settle();
}

test("typing @rc opens a listbox and Enter picks the highlighted repo", async () => {
  const { b, textarea, picked, calls } = await setup();
  await type(textarea, "Explain @rc");
  const menu = b.document.querySelector('[role="listbox"]');
  assert.ok(menu && !menu.hidden, "menu opens");
  assert.equal(textarea.getAttribute("role"), "combobox");
  assert.equal(textarea.getAttribute("aria-haspopup"), "listbox");
  assert.equal(textarea.getAttribute("aria-expanded"), "true");
  assert.match(calls.find((p) => p.startsWith("/api/context")), /query=rc/);
  const options = menu.querySelectorAll('[role="option"]');
  assert.deepEqual(options.map((o) => visibleText(o).split("·")[0].trim()), ["storefront-app"], "only pickable repos are offered");
  assert.equal(textarea.getAttribute("aria-activedescendant"), options[0].id);
  const event = keydown(textarea, "Enter");
  assert.equal(event.defaultPrevented, true, "Enter does not reach the composer");
  assert.deepEqual(picked, [{ id: "gh:1", name: "storefront-app" }]);
  assert.equal(textarea.value, "Explain ");
  assert.equal(menu.hidden, true);
  assert.equal(textarea.getAttribute("aria-expanded"), "false");
  assert.equal(textarea.hasAttribute("aria-activedescendant"), false);
});

test("Escape closes the menu and keeps the typed text", async () => {
  const { b, textarea, picked } = await setup();
  await type(textarea, "@rc");
  const event = keydown(textarea, "Escape");
  assert.equal(event.defaultPrevented, true);
  assert.equal(b.document.querySelector('[role="listbox"]').hidden, true);
  assert.equal(textarea.value, "@rc");
  assert.deepEqual(picked, []);
});

test("an email address does not open the menu", async () => {
  const { b, textarea, calls } = await setup();
  await type(textarea, "mail a@b");
  const menu = b.document.querySelector('[role="listbox"]');
  assert.ok(!menu || menu.hidden);
  assert.equal(calls.length, 0);
});

test("Tab picks and clicking an option picks; detach removes the menu", async () => {
  const { b, textarea, picked, handle } = await setup();
  await type(textarea, "@rc");
  keydown(textarea, "Tab");
  assert.deepEqual(picked.map((p) => p.id), ["gh:1"]);
  await type(textarea, "and @rc");
  b.document.querySelector('[role="option"]').click();
  assert.equal(picked.length, 2);
  assert.equal(textarea.value, "and ");
  handle.detach();
  assert.equal(b.document.querySelector('[role="listbox"]'), null);
});

test("keys pass through when the menu is closed", async () => {
  const { textarea } = await setup();
  input(textarea, "hello");
  assert.equal(keydown(textarea, "Enter").defaultPrevented, false);
});

test("repositories already chosen are not offered", async () => {
  const b = createBrowser("<!doctype html><html><body><div><textarea id=t></textarea></div></body></html>", { respond: () => ({ status: 404, body: {} }) });
  install(b);
  const { attachMentions } = await import(`${url}?m=exclude`);
  const request = async (path) => path.startsWith("/api/context") ? { items: [ctx(1, "storefront-app"), ctx(3, "storefront-tools")], next_cursor: null } : { items: [] };
  const textarea = b.document.getElementById("t");
  attachMentions(textarea, { request, onPick: () => {}, exclude: () => ["gh:1"] });
  await type(textarea, "@rc");
  assert.deepEqual(b.document.querySelectorAll('[role="option"]').map((o) => o.querySelector("strong").textContent), ["storefront-tools"]);
});

test("indexed matches past the first unindexed ones are still offered; no match says so", async () => {
  const b = createBrowser("<!doctype html><html><body><div><textarea id=t></textarea></div></body></html>", { respond: () => ({ status: 404, body: {} }) });
  install(b);
  const { attachMentions } = await import(`${url}?m=large`);
  const limits = [];
  const unindexed = Array.from({ length: 10 }, (_, i) => ctx(100 + i, `storefront-old-${i}`, { status: "metadata_only", active_snapshot: null }));
  const request = async (path) => {
    if (!path.startsWith("/api/context")) return { items: [] };
    const q = new URL(path, "http://x").searchParams;
    limits.push(Number(q.get("limit")));
    return q.get("query") === "zzz" ? { items: [], next_cursor: null } : { items: [...unindexed, ctx(1, "storefront-app")], next_cursor: null };
  };
  const textarea = b.document.getElementById("t");
  attachMentions(textarea, { request, onPick: () => {} });
  await type(textarea, "@storefront");
  assert.ok(limits[0] >= 25, "asks for a wider page than it shows");
  assert.deepEqual(b.document.querySelectorAll('[role="option"]').map((o) => o.querySelector("strong").textContent), ["storefront-app"]);
  await type(textarea, "@zzz");
  const menu = b.document.querySelector('[role="listbox"]');
  assert.equal(menu.hidden, false);
  assert.equal(textarea.getAttribute("aria-expanded"), "true");
  assert.match(visibleText(menu), /No indexed repositories match/);
  assert.equal(keydown(textarea, "Enter").defaultPrevented, false, "Enter is not swallowed when there is nothing to pick");
});
