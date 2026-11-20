// Repository search merges index state with GitHub catalog metadata and maps plain status labels.
import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createBrowser, install } from "./dom.mjs";

const url = pathToFileURL(fileURLToPath(new URL("../../opendevengine/web/repo-source.js", import.meta.url))).href;
let loads = 0;
async function load() {
  install(createBrowser("<!doctype html><html><body></body></html>", { respond: () => ({ status: 404, body: {} }) }));
  loads += 1;
  return import(`${url}?r=${loads}`);
}

const snapshot = (mode = "lexical_only") => ({ id: "snap", revision: "d8f5918f985503cd5a2fd0c0956abd32b4800f3c", mode, coverage: { mode, files: 59 } });
const ctx = (over = {}) => ({ repository_id: "gh:1", name: "acme-dev/storefront-app", kind: "github", eligible: true, status: "ready", active_snapshot: snapshot(), active_run: null, ...over });
const cat = (over = {}) => ({ id: "gh:1", owner: "acme-dev", name: "storefront-app", full_name: "acme-dev/storefront-app", language: "TypeScript", access_state: "accessible", index_enabled: true, ...over });

test("repoStatus maps every state to one plain label and tone", async () => {
  const { repoStatus } = await load();
  assert.deepEqual(repoStatus(ctx()), { tone: "warn", label: "Indexed · keyword only" });
  assert.deepEqual(repoStatus(ctx({ active_snapshot: snapshot("hybrid") })), { tone: "ok", label: "Indexed" });
  assert.deepEqual(repoStatus(ctx({ status: "stale" })), { tone: "warn", label: "Indexed · keyword only" });
  assert.deepEqual(repoStatus(ctx({ status: "indexing", active_snapshot: null, active_run: { state: "running", progress: { files_done: 30, files_total: 60 } } })), { tone: "info", label: "Indexing 50%" });
  assert.deepEqual(repoStatus(ctx({ status: "indexing", active_snapshot: null, active_run: { state: "queued", progress: {} } })), { tone: "info", label: "Indexing…" });
  const retry = new Date(2026, 10, 15, 14, 5).getTime();
  assert.deepEqual(repoStatus(ctx({ status: "indexing", active_snapshot: null, active_run: { state: "waiting", retry_at: retry, progress: {} } })), { tone: "info", label: "Waiting until 14:05" });
  assert.deepEqual(repoStatus(ctx({ status: "failed", active_snapshot: null })), { tone: "danger", label: "Failed" });
  assert.deepEqual(repoStatus(ctx({ status: "metadata_only", active_snapshot: null })), { tone: "off", label: "Not indexed" });
  assert.deepEqual(repoStatus(ctx({ status: "excluded", eligible: false })), { tone: "off", label: "Off" });
  assert.deepEqual(repoStatus(ctx(), cat({ access_state: "inaccessible" })), { tone: "off", label: "No access" });
});

test("searchRepositories merges catalog metadata and passes the cursor through", async () => {
  const { searchRepositories } = await load();
  const calls = [];
  const request = async (path) => {
    calls.push(path);
    if (path.startsWith("/api/context/repositories")) return { items: [ctx(), ctx({ repository_id: "gh:2", name: "acme-dev/legacy-site", status: "metadata_only", active_snapshot: null }), ctx({ repository_id: "local:tool", name: "tool", kind: "local" })], next_cursor: "gh:2" };
    return { items: [cat(), cat({ id: "gh:2", name: "legacy-site", language: null })], next_cursor: null };
  };
  const page = await searchRepositories(request, { query: "rc n", cursor: "gh:0", limit: 25 });
  assert.equal(calls[0], "/api/context/repositories?limit=25&query=rc%20n&cursor=gh%3A0");
  assert.match(calls[1], /^\/api\/repositories\?limit=100&query=rc%20n/);
  assert.equal(page.nextCursor, "gh:2");
  assert.deepEqual(page.items[0], { id: "gh:1", name: "storefront-app", owner: "acme-dev", language: "TypeScript", status: { tone: "warn", label: "Indexed · keyword only" }, selectable: true });
  assert.equal(page.items[1].selectable, false, "not indexed repos cannot be picked");
  assert.equal(page.items[2].name, "tool");
  assert.equal(page.items[2].owner, null);
});

test("searchRepositories still works when the catalog endpoint fails", async () => {
  const { searchRepositories } = await load();
  const request = async (path) => {
    if (path.startsWith("/api/repositories")) throw new Error("404");
    return { items: [ctx()], next_cursor: null };
  };
  const page = await searchRepositories(request, {});
  assert.equal(page.items[0].name, "storefront-app");
  assert.equal(page.items[0].owner, "acme-dev");
});

test("recent repository ids survive storage errors and cap at 8", async () => {
  const mod = await load();
  globalThis.localStorage = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
  assert.deepEqual(mod.recentRepositoryIds(), []);
  mod.rememberRepositories(["gh:1"]);
  const store = new Map();
  globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  mod.rememberRepositories(["gh:1", "gh:2"]);
  mod.rememberRepositories(["gh:3", "gh:1", "gh:4", "gh:5", "gh:6", "gh:7", "gh:8", "gh:9"]);
  assert.deepEqual(mod.recentRepositoryIds(), ["gh:3", "gh:1", "gh:4", "gh:5", "gh:6", "gh:7", "gh:8", "gh:9"]);
  delete globalThis.localStorage;
});
