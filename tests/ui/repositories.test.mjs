// Repositories page: one merged list, switches that save immediately, filters, detail panel.
import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { readFileSync } from "node:fs";
import { createBrowser, input, install, settle, visibleText } from "./dom.mjs";

const pageUrl = pathToFileURL(fileURLToPath(new URL("../../opendevengine/web/repositories.js", import.meta.url))).href;
const panelUrl = pathToFileURL(fileURLToPath(new URL("../../opendevengine/web/panel.js", import.meta.url))).href;
let loads = 0;
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const HASH = "c5bd264359aaf9f03b77644838ae7575855b6c53";
const css = readFileSync(new URL("../../opendevengine/web/styles.css", import.meta.url), "utf8");
const catalogItem = (id, name, over = {}) => ({ id, owner: "acme-dev", name, full_name: `acme-dev/${name}`, language: "TypeScript", pushed_at: "2026-11-03T16:45:50Z",
  index_enabled: true, implementation_enabled: false, access_state: "accessible", default_branch: "main", visibility: "private", ...over });
const contextItem = (id, name, over = {}) => ({ repository_id: id, name: `acme-dev/${name}`, kind: "github", eligible: true, status: "ready", implementation_enabled: false,
  active_snapshot: { id: "snap", revision: HASH, mode: "lexical_only", branch: "main", coverage: { mode: "lexical_only", files: 59, chunks: 219, skipped: { generated: 28, sensitive: 5, binary_or_sensitive: 2 } } }, active_run: null, ...over });

async function setup(overrides = {}) {
  const b = createBrowser("<!doctype html><html><body><main id=host></main><aside id=side hidden></aside></body></html>", { respond: () => ({ status: 404, body: {} }) });
  install(b);
  const store = new Map();
  globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) };
  loads += 1;
  const { createRepositoriesPage } = await import(`${pageUrl}?r=${loads}`);
  const { createSidePanel } = await import(`${panelUrl}?r=${loads}`);
  let catalog = [catalogItem("gh:1", "storefront-app"), catalogItem("gh:2", "deploy-tools"), catalogItem("gh:3", "cube-workbench", { access_state: "inaccessible" })];
  const contexts = [contextItem("gh:1", "storefront-app"), contextItem("gh:2", "deploy-tools", { status: "failed", active_snapshot: null }), contextItem("gh:3", "cube-workbench")];
  const calls = [];
  const request = async (path, method = "GET", body) => {
    calls.push([method, path, body]);
    if (overrides[`${method} ${path.split("?")[0]}`]) return overrides[`${method} ${path.split("?")[0]}`]({ path, body, catalog });
    if (path.startsWith("/api/repositories?")) return { items: catalog, next_cursor: null };
    if (path.startsWith("/api/context/repositories?")) return { items: contexts, next_cursor: null };
    if (path === "/api/settings/model-roles") return { roles: { embedding: { status: "unassigned" } }, settings: { local_only: true } };
    if (path === "/api/github/connections") return { connections: [{ id: "ghc", status: "connected", last_refresh_at: Date.now() - 300000 }] };
    if (method === "PATCH" && path.startsWith("/api/repositories/")) {
      const id = decodeURIComponent(path.split("/")[3]);
      catalog = catalog.map((item) => item.id === id ? { ...item, ...body } : item);
      return catalog.find((item) => item.id === id);
    }
    const one = /^\/api\/repositories\/(.+)\/context$/.exec(path);
    if (one) return contexts.find((c) => c.repository_id === decodeURIComponent(one[1]));
    if (path.endsWith("/exclusions")) return { exclusions: ["dist/**"] };
    return {};
  };
  const panel = createSidePanel(b.document.getElementById("side"));
  const page = createRepositoriesPage({ host: b.document.getElementById("host"), request, panel, shell: { setHeader() {} } });
  await page.refresh();
  await settle();
  return { b, page, calls, host: b.document.getElementById("host"), side: b.document.getElementById("side") };
}

const row = (host, name) => host.querySelectorAll("tr").find((tr) => visibleText(tr).includes(name));
const switchIn = (tr, label) => tr.querySelectorAll('[role="switch"]').find((s) => s.getAttribute("aria-label").startsWith(label));

test("one merged table with plain status, no hashes, and a keyword-only banner", async () => {
  const { host } = await setup();
  const text = visibleText(host);
  assert.deepEqual(host.querySelectorAll("tbody tr").map((tr) => tr.querySelector("th").textContent.split("TypeScript")[0]), ["storefront-app", "deploy-tools", "cube-workbench"]);
  assert.match(visibleText(row(host, "storefront-app")), /Indexed · keyword only/);
  assert.match(visibleText(row(host, "deploy-tools")), /Failed/);
  assert.match(visibleText(row(host, "cube-workbench")), /No access/);
  assert.doesNotMatch(text, /[0-9a-f]{40}/);
  assert.match(text, /Answers use keyword search only\. Turn on smarter search/);
  assert.ok(host.querySelectorAll("a").find((a) => a.getAttribute("href") === "#/settings/models"));
  assert.match(text, /synced 5 min ago/);
  assert.deepEqual(host.querySelectorAll(".filter-chip").map((button) => button.textContent), ["All", "Used in Ask", "Changes allowed", "Needs attention"]);
  const region = host.querySelector(".repos-table-box");
  assert.equal(region.getAttribute("role"), "region");
  assert.equal(region.getAttribute("aria-label"), "Repository catalog");
  const repoRow = row(host, "storefront-app");
  assert.deepEqual([...repoRow.querySelectorAll("th"), ...repoRow.querySelectorAll("td")].map((cell) => cell.dataset.label), ["Repository", "Status", "Used in Ask", "Changes allowed"]);
  assert.match(css, /\.repos-table tbody th small\s*\{\s*grid-column:\s*2;/, "mobile metadata stays beneath the repository name column");
  assert.match(text, /3 repositories loaded\./);
});

test("Use in Ask saves immediately with both selection fields", async () => {
  const { host, calls } = await setup();
  switchIn(row(host, "storefront-app"), "Use in Ask").click();
  await settle();
  assert.deepEqual(calls.find(([m]) => m === "PATCH"), ["PATCH", "/api/repositories/gh%3A1", { index_enabled: false, implementation_enabled: false }]);
  assert.equal(switchIn(row(host, "storefront-app"), "Use in Ask").getAttribute("aria-checked"), "false");
});

test("a failed save reverts the switch and ignores a second click while saving", async () => {
  let patches = 0;
  const { host } = await setup({ "PATCH /api/repositories/gh%3A1": async () => { patches += 1; await wait(20); throw Object.assign(new Error("Network down"), { status: 503 }); } });
  const control = switchIn(row(host, "storefront-app"), "Use in Ask");
  control.click();
  control.click();
  await wait(40); await settle();
  assert.equal(patches, 1);
  assert.equal(control.getAttribute("aria-checked"), "true");
  assert.match(visibleText(row(host, "storefront-app")), /Network down/);
});

test("Allow changes asks for confirmation only the first time per repository", async () => {
  const { b, host, calls } = await setup();
  switchIn(row(host, "storefront-app"), "Allow changes").click();
  await settle();
  const dialog = b.document.querySelector("dialog.modal");
  assert.match(visibleText(dialog), /Allow OpenDevEngine to push branches and open PRs in storefront-app\? You can turn this off any time\./);
  dialog.querySelectorAll("button").find((x) => x.textContent === "Allow").click();
  await settle();
  assert.deepEqual(calls.filter(([m]) => m === "PATCH").at(-1)[2], { index_enabled: true, implementation_enabled: true });
  switchIn(row(host, "storefront-app"), "Allow changes").click();
  await settle();
  switchIn(row(host, "storefront-app"), "Allow changes").click();
  await settle();
  assert.equal(b.document.querySelector("dialog.modal"), null, "no second confirmation");
  assert.equal(calls.filter(([m]) => m === "PATCH").length, 3);
});

test("cancelling the Allow changes confirmation keeps it off", async () => {
  const { b, host, calls } = await setup();
  const control = switchIn(row(host, "storefront-app"), "Allow changes");
  control.click();
  await settle();
  b.document.querySelector("dialog.modal").querySelectorAll("button").find((x) => x.textContent === "Cancel").click();
  await settle();
  assert.equal(calls.filter(([m]) => m === "PATCH").length, 0);
  assert.equal(control.getAttribute("aria-checked"), "false");
});

test("Needs attention filter shows failed and inaccessible repositories; search queries the server", async () => {
  const { host, calls } = await setup();
  host.querySelectorAll("button").find((x) => x.textContent === "Needs attention").click();
  await settle();
  assert.deepEqual(host.querySelectorAll("tbody tr").map((tr) => tr.querySelector("th").textContent.split("TypeScript")[0]), ["deploy-tools", "cube-workbench"]);
  input(host.querySelector('input[type="search"]'), "storefront");
  await wait(260); await settle();
  assert.ok(calls.some(([, path]) => path.startsWith("/api/repositories?") && path.includes("query=storefront")));
});

test("empty catalog links directly to GitHub settings", async () => {
  const empty = () => ({ items: [], next_cursor: null });
  const { host } = await setup({ "GET /api/repositories": empty, "GET /api/context/repositories": empty });
  const link = host.querySelector('a[href="#/settings/github"]');
  assert.ok(link);
  assert.equal(link.textContent, "Connect GitHub in Settings");
});

test("clicking a repository opens details with plain skipped reasons and its index controls", async () => {
  const { host, side } = await setup();
  row(host, "storefront-app").querySelector("th button").click();
  await settle(20);
  assert.equal(side.hidden, false);
  const text = visibleText(side);
  assert.match(text, /acme-dev\/storefront-app · private · main @ c5bd264/);
  assert.match(text, /59 files indexed · 35 skipped/);
  assert.match(text, /28 generated files, 5 files that look like secrets, 2 binary or secret-like files/);
  assert.ok(side.querySelectorAll("button").find((x) => x.textContent === "Reindex now"));
  assert.match(text, /Ignored paths/);assert.match(text, /dist\/\*\*/);assert.match(text, /Danger zone/);
  assert.ok(side.querySelectorAll("a").find((a) => a.getAttribute("href") === "https://github.com/acme-dev/storefront-app"));
});

test("Sync starts a catalog refresh for the connection", async () => {
  const { host, calls } = await setup();
  host.querySelectorAll("button").find((x) => x.textContent === "↻ Sync").click();
  await settle();
  assert.deepEqual(calls.find(([m, p]) => m === "POST" && p === "/api/github/refresh"), ["POST", "/api/github/refresh", { connection_id: "ghc" }]);
});

test("the saved server response wins and reloads wait for a pending save", async () => {
  let release;
  const { host, calls, page } = await setup({ "PATCH /api/repositories/gh%3A1": () => new Promise((resolve) => { release = resolve; }) });
  switchIn(row(host, "storefront-app"), "Use in Ask").click();
  await settle();
  const reads = calls.filter(([, p]) => p.startsWith("/api/repositories?")).length;
  await page.refresh();
  assert.equal(calls.filter(([, p]) => p.startsWith("/api/repositories?")).length, reads, "no reload while saving");
  release({ ...catalogItem("gh:1", "storefront-app"), index_enabled: true, implementation_enabled: false });
  await settle();
  assert.equal(switchIn(row(host, "storefront-app"), "Use in Ask").getAttribute("aria-checked"), "true", "server kept it on");
});

test("a lost connection reports an unknown outcome, never retries and reloads saved state", async () => {
  let patches = 0;
  const { host, calls } = await setup({ "PATCH /api/repositories/gh%3A1": () => { patches += 1; throw new Error("Connection lost; the outcome is unknown. Refresh and inspect saved state before submitting again."); } });
  const reads = calls.filter(([, p]) => p.startsWith("/api/repositories?")).length;
  switchIn(row(host, "storefront-app"), "Use in Ask").click();
  await settle(20);
  assert.equal(patches, 1);
  assert.match(visibleText(host), /the outcome is unknown/);
  assert.ok(calls.filter(([, p]) => p.startsWith("/api/repositories?")).length > reads, "saved state is reloaded");
});

test("catalog traversal reaches 205 repositories and owner/fork/archive filters span all pages", async () => {
  const all=Array.from({length:205},(_,i)=>catalogItem(`gh:${i}`,`repo-${i}`,{owner:i===204?'later':'first',full_name:`${i===204?'later':'first'}/repo-${i}`,fork:i===204,archived:i===204}));
  const {host,calls}=await setup({"GET /api/repositories":({path})=>{const u=new URL(path,'http://localhost'),start=Number(u.searchParams.get('cursor') || 0);return {items:all.slice(start,start+100),next_cursor:start+100<all.length?String(start+100):null};}});
  assert.match(visibleText(host),/repo-204/);assert.ok(calls.some(([,p])=>p.includes('cursor=200')));
  input([...host.querySelectorAll("input")].find(n=>n.getAttribute("aria-label")==="Exact repository owner"),'later');
  const archive=host.querySelectorAll('select').find(n=>n.parentNode.textContent.startsWith('Archive state'));archive.value='true';archive.dispatchEvent({type:'change'});
  const fork=host.querySelectorAll('select').find(n=>n.parentNode.textContent.startsWith('Fork state'));fork.value='true';fork.dispatchEvent({type:'change'});
  assert.equal(host.querySelectorAll('tbody tr').length,1);assert.match(visibleText(host),/repo-204/);
});

test("two switches on one row save in order and the second keeps the first change", async () => {
  const bodies = [];
  let inFlight = 0, overlapped = false;
  const { b, host } = await setup({ "PATCH /api/repositories/gh%3A1": async ({ body }) => {
    inFlight += 1; if (inFlight > 1) overlapped = true;
    bodies.push(body); await wait(15); inFlight -= 1;
    return { ...catalogItem("gh:1", "storefront-app"), ...body };
  } });
  globalThis.localStorage.setItem("od.allowConfirmed", JSON.stringify(["gh:1"]));
  switchIn(row(host, "storefront-app"), "Use in Ask").click();
  switchIn(row(host, "storefront-app"), "Allow changes").click();
  await wait(60); await settle();
  assert.equal(overlapped, false, "saves on one row never overlap");
  assert.ok(bodies.every((b) => b.index_enabled === false), "no save puts Use in Ask back");
  assert.deepEqual(bodies.at(-1), { index_enabled: false, implementation_enabled: true });
  assert.equal(switchIn(row(host, "storefront-app"), "Use in Ask").getAttribute("aria-checked"), "false");
  assert.equal(switchIn(row(host, "storefront-app"), "Allow changes").getAttribute("aria-checked"), "true");
  assert.ok(b);
});


test("failed first row save cannot overwrite an immutable queued second intent", async () => {
  const bodies=[]; let rejectFirst;
  const {host}=await setup({"PATCH /api/repositories/gh%3A1":({body})=>{
    bodies.push(structuredClone(body));
    if(bodies.length===1)return new Promise((_,reject)=>rejectFirst=reject);
    return {...catalogItem("gh:1","storefront-app"),...body};
  }});
  globalThis.localStorage.setItem("od.allowConfirmed",JSON.stringify(["gh:1"]));
  switchIn(row(host,"storefront-app"),"Use in Ask").click();await settle();
  switchIn(row(host,"storefront-app"),"Allow changes").click();await settle();
  rejectFirst(Object.assign(new Error("First selection rejected"),{status:409}));await settle(25);
  assert.deepEqual(bodies,[{index_enabled:false,implementation_enabled:false},{index_enabled:false,implementation_enabled:true}]);
  assert.equal(switchIn(row(host,"storefront-app"),"Use in Ask").getAttribute("aria-checked"),"false");
  assert.equal(switchIn(row(host,"storefront-app"),"Allow changes").getAttribute("aria-checked"),"true");
});


test("detail panel ignores and un-ignores paths, reindexes, pauses and deletes the local index only after confirming", async () => {
  let exclusions = ["dist/**"];
  const { host, side, calls, b } = await setup({
    "GET /api/repositories/gh%3A1/exclusions": () => ({ exclusions }),
    "PATCH /api/repositories/gh%3A1/exclusions": ({ body }) => { exclusions = body.exclusions; return { exclusions }; },
  });
  const sideButton = (text) => side.querySelectorAll("button").find((x) => x.textContent === text);
  row(host, "storefront-app").querySelector("th button").click();
  await settle(20);
  sideButton("＋ Add").click();
  input(side.querySelectorAll("label").find((x) => x.textContent.startsWith("Path or glob")).querySelector("input"), "vendor/**");
  side.querySelector("form.detail-add").requestSubmit();
  await settle(30);
  assert.deepEqual(exclusions, ["dist/**", "vendor/**"]);
  assert.match(visibleText(side), /Now ignoring vendor\/\*\*/);
  side.querySelectorAll("button").find((x) => x.getAttribute("aria-label") === "Stop ignoring dist/**").click();
  await settle(30);
  assert.deepEqual(exclusions, ["vendor/**"]);

  sideButton("Reindex now").click();
  await settle(30);
  assert.ok(calls.some(([m, p]) => m === "POST" && p === "/api/repositories/gh%3A1/index"));

  sideButton("Delete local index…").click();
  await settle();
  const dialog = b.document.querySelector("dialog");
  assert.ok(dialog);
  dialog.querySelectorAll("button").find((x) => x.textContent === "Cancel").click();
  await settle(20);
  assert.equal(calls.filter(([m, p]) => m === "POST" && p.endsWith("/purge")).length, 0);
  sideButton("Delete local index…").click();
  await settle();
  b.document.querySelector("dialog").querySelectorAll("button").find((x) => x.textContent === "Delete local index").click();
  await settle(30);
  assert.equal(calls.filter(([m, p]) => m === "POST" && p === "/api/repositories/gh%3A1/purge").length, 1);
  assert.match(visibleText(side), /Local index deleted\./);
});

test("a running index job offers Pause and disables Reindex", async () => {
  const { host, side, calls } = await setup({
    "GET /api/repositories/gh%3A1/context": () => contextItem("gh:1", "storefront-app", { active_run: { id: "job1", state: "running", phase: "embedding", progress: { files: 12 } } }),
  });
  row(host, "storefront-app").querySelector("th button").click();
  await settle(20);
  // Context in the table comes from the list; open detail reflects it after the first refresh of the row.
  const reindex = side.querySelectorAll("button").find((x) => x.textContent === "Reindex now");
  reindex.click(); await settle(30);
  const pause = side.querySelectorAll("button").find((x) => x.textContent === "Pause");
  assert.ok(pause);
  assert.equal(side.querySelectorAll("button").find((x) => x.textContent === "Reindex now").disabled, true);
  pause.click(); await settle(30);
  assert.ok(calls.some(([m, p]) => m === "POST" && p === "/api/index-jobs/job1/pause"));
});
