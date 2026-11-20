// Repository picker modal: search on the server, paging, recent group, keyboard and focus.
import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createBrowser, input, install, keydown, settle, visibleText } from "./dom.mjs";

const url = pathToFileURL(fileURLToPath(new URL("../../opendevengine/web/picker.js", import.meta.url))).href;
let loads = 0;
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const ctx = (i, over = {}) => ({ repository_id: `gh:${i}`, name: `acme-dev/repo-${i}`, kind: "github", eligible: true, status: "ready",
  active_snapshot: { mode: "lexical_only" }, active_run: null, ...over });

async function setup(responder) {
  const b = createBrowser("<!doctype html><html><body><button id=plus>+</button></body></html>", { respond: () => ({ status: 404, body: {} }) });
  install(b);
  const store = new Map();
  globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  loads += 1;
  const { openRepoPicker } = await import(`${url}?p=${loads}`);
  const calls = [];
  const request = async (path, method = "GET", body) => { calls.push([method, path, body]); return responder(path, method, body); };
  const trigger = b.document.getElementById("plus");
  trigger.focus();
  return { b, openRepoPicker, request, calls, trigger, store };
}

const rows = (b) => b.document.querySelectorAll(".picker-row");
const rowFor = (b, name) => rows(b).find((row) => visibleText(row).includes(name));
const footerButton = (b, text) => b.document.querySelectorAll("dialog button").find((x) => x.textContent.startsWith(text));

test("picker selects two repos and resolves with ids and names", async () => {
  const { b, openRepoPicker, request } = await setup((path) => path.startsWith("/api/context") ? { items: [ctx(1), ctx(2), ctx(3)], next_cursor: null } : { items: [] });
  const result = openRepoPicker({ request, selected: [], trigger: b.document.getElementById("plus") });
  await settle();
  assert.equal(b.document.activeElement, b.document.querySelector('dialog input[type="search"]'), "search is focused");
  assert.equal(footerButton(b, "Add").disabled, true);
  rowFor(b, "repo-1").querySelector('input[type="checkbox"]').click();
  rowFor(b, "repo-3").querySelector('input[type="checkbox"]').click();
  assert.match(visibleText(b.document.querySelector("dialog footer")), /2 selected/);
  footerButton(b, "Add 2").click();
  assert.deepEqual(await result, [{ id: "gh:1", name: "repo-1" }, { id: "gh:3", name: "repo-3" }]);
});

test("picker keeps preselected repos and shows recently used first", async () => {
  const { b, openRepoPicker, request, store } = await setup((path) => path.startsWith("/api/context") ? { items: [ctx(1), ctx(2), ctx(3)], next_cursor: null } : { items: [] });
  store.set("od.recentRepos", JSON.stringify(["gh:3"]));
  const result = openRepoPicker({ request, selected: [{ id: "gh:2", name: "repo-2" }, { id: "gh:9", name: "far-away" }], trigger: b.document.getElementById("plus") });
  await settle();
  const groups = b.document.querySelectorAll(".picker-group").map((g) => g.textContent);
  assert.deepEqual(groups, ["Recently used", "All repositories · 2"]);
  assert.equal(rowFor(b, "repo-2").querySelector("input").checked, true);
  rowFor(b, "repo-1").querySelector("input").click();
  footerButton(b, "Add").click();
  assert.deepEqual((await result).map((r) => r.id).sort(), ["gh:1", "gh:2", "gh:9"]);
});

test("picker search hits the server once after typing pauses and pages with Load more", async () => {
  const { b, openRepoPicker, request, calls } = await setup((path) => {
    if (!path.startsWith("/api/context")) return { items: [] };
    const cursor = new URL(path, "http://x").searchParams.get("cursor");
    const start = cursor ? 26 : 1;
    return { items: Array.from({ length: 25 }, (_, i) => ctx(start + i)), next_cursor: cursor ? null : "gh:25" };
  });
  openRepoPicker({ request, selected: [], trigger: b.document.getElementById("plus") });
  await settle();
  const search = b.document.querySelector('dialog input[type="search"]');
  input(search, "r"); input(search, "re"); input(search, "rep");
  await wait(260); await settle();
  const contextCalls = calls.filter(([, path]) => path.startsWith("/api/context"));
  assert.equal(contextCalls.length, 2, "initial load plus one debounced search");
  assert.match(contextCalls[1][1], /query=rep/);
  assert.equal(rows(b).length, 25);
  footerButton(b, "Load more").click();
  await settle();
  assert.equal(rows(b).length, 50);
  assert.match(calls.filter(([, path]) => path.startsWith("/api/context")).at(-1)[1], /cursor=gh%3A25/);
});

test("not-indexed repo is disabled and offers Index now", async () => {
  const { b, openRepoPicker, request, calls } = await setup((path, method) => {
    if (method === "POST") return { id: "run-1", state: "queued" };
    return path.startsWith("/api/context") ? { items: [ctx(1), ctx(2, { status: "metadata_only", active_snapshot: null })], next_cursor: null } : { items: [] };
  });
  openRepoPicker({ request, selected: [], trigger: b.document.getElementById("plus") });
  await settle();
  const row = rowFor(b, "repo-2");
  assert.equal(row.querySelector("input").disabled, true);
  assert.match(visibleText(row), /Not indexed/);
  row.querySelectorAll("button").find((x) => x.textContent === "Index now").click();
  await settle();
  assert.deepEqual(calls.find(([method]) => method === "POST"), ["POST", "/api/repositories/gh%3A2/index", {}]);
});

test("arrow keys move between rows and Enter never confirms implicitly", async () => {
  const { b, openRepoPicker, request } = await setup((path) => path.startsWith("/api/context") ? { items: [ctx(1), ctx(2)], next_cursor: null } : { items: [] });
  const result = openRepoPicker({ request, selected: [], trigger: b.document.getElementById("plus") });
  await settle();
  const search = b.document.querySelector('dialog input[type="search"]');
  keydown(search, "ArrowDown");
  assert.equal(b.document.activeElement, rowFor(b, "repo-1").querySelector("input"));
  keydown(b.document.activeElement, "ArrowDown");
  assert.equal(b.document.activeElement, rowFor(b, "repo-2").querySelector("input"));
  b.document.activeElement.click();
  keydown(b.document.activeElement, "Enter");
  assert.ok(b.document.querySelector("dialog").open, "Enter on a checkbox leaves the picker open");
  keydown(search, "Enter");
  assert.ok(b.document.querySelector("dialog").open, "Enter in search leaves the picker open");
  footerButton(b, "Add").click();
  assert.deepEqual(await result, [{ id: "gh:2", name: "repo-2" }]);
});

test("Escape resolves null and focus returns to the trigger", async () => {
  const { b, openRepoPicker, request, trigger } = await setup(() => ({ items: [], next_cursor: null }));
  const result = openRepoPicker({ request, selected: [], trigger });
  await settle();
  assert.match(visibleText(b.document.querySelector("dialog")), /No repositories match/);
  keydown(b.document.querySelector("dialog"), "Escape");
  assert.equal(await result, null);
  assert.equal(b.document.activeElement, trigger);
});
