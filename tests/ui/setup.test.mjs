// First-run checklist on the Ask landing page: GitHub, repositories, models.
import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createBrowser, install, settle, visibleText } from "./dom.mjs";

const url = pathToFileURL(fileURLToPath(new URL("../../opendevengine/web/setup.js", import.meta.url))).href;
let loads = 0;
const repo = (id, name, over = {}) => ({ id, name, full_name: `acme-dev/${name}`, index_enabled: false, implementation_enabled: false, ...over });

async function setup({ configured = false, connected = true, repos = [repo("gh:1", "storefront-app"), repo("gh:2", "deploy-tools")], askProfile = null, discover = { reachable: true, models: [{ name: "llama3.2:latest", embedding: false }, { name: "nomic-embed-text:latest", embedding: true }] }, fail = false } = {}) {
  const b = createBrowser("<!doctype html><html><body><main id=host></main></body></html>", { respond: () => ({ status: 404, body: {} }) });
  install(b);
  loads += 1;
  const mod = await import(`${url}?s=${loads}`);
  const calls = [];
  let profiles = [];
  const request = async (path, method = "GET", body) => {
    calls.push([method, path, body]);
    if (fail) throw Object.assign(new Error("not_found. Endpoint unavailable in this build."), { status: 404 });
    if (path === "/api/github/connections") return { connections: connected ? [{ id: "ghc", status: "connected" }] : configured ? [{ id: "ghc", status: "configured", slug: "opendevengine-99fc7a" }] : [] };
    if (path === "/api/github/connect") return { install_url: "https://github.com/apps/opendevengine-99fc7a/installations/new?state=s", expires_at: 1 };
    if (path.startsWith("/api/repositories?")) return { items: repos, next_cursor: null };
    if (method === "PATCH" && path.startsWith("/api/repositories/")) return { ...body };
    if (path === "/api/settings/model-roles" && method === "GET") return { roles: { ask: { profile_id: askProfile } }, settings: {} };
    if (path === "/api/settings/model-roles") return {};
    if (path === "/api/provider-profiles/discover/dimensions") return { dimensions: 768 };
    if (path.startsWith("/api/provider-profiles/discover")) return discover;
    if (path === "/api/provider-profiles" && method === "GET") return { profiles };
    if (path === "/api/provider-profiles" && method === "POST") { const p = { id: `prof_${profiles.length + 1}`, version: { adapter: "ollama", model: body.model, endpoint: body.endpoint } }; profiles = [...profiles, p]; return p; }
    if (path === "/api/github/manifest/start") return { action: "https://github.com/settings/apps/new", state: "S", manifest: {} };
    return {};
  };
  const host = b.document.getElementById("host");
  let done = 0;
  const submitted = [];
  const navigated = [];
  const rolesSeen = [];
  await mod.renderSetup(host, { request, onDone: () => { done += 1; }, onRoles: (roles) => rolesSeen.push(roles), submitForm: (form) => submitted.push(form), navigate: (href) => navigated.push(href) });
  await settle();
  return { b, mod, host, calls, done: () => done, submitted, navigated, rolesSeen };
}

const btn = (host, text) => host.querySelectorAll("button").find((x) => x.textContent === text);

test("needsSetup is true until GitHub, a repository and an answering model are all set", async () => {
  const { mod } = await setup();
  assert.equal(mod.needsSetup({ githubConnected: true, anyIndexed: true, askAssigned: true }), false);
  for (const missing of ["githubConnected", "anyIndexed", "askAssigned"]) {
    assert.equal(mod.needsSetup({ githubConnected: true, anyIndexed: true, askAssigned: true, [missing]: false }), true, missing);
  }
});

test("the checklist marks GitHub done and indexes the chosen repositories", async () => {
  const { host, calls } = await setup();
  const text = visibleText(host);
  assert.match(text, /Set up OpenDevEngine/);
  assert.match(text, /✓\s*Connect GitHub/);
  assert.match(text, /Choose repositories to search/);
  const boxes = host.querySelectorAll('input[type="checkbox"]');
  boxes[0].click();
  boxes[1].click();
  assert.ok(btn(host, "Index 2 repositories"));
  assert.match(text, /You can keep going while this runs\./);
  btn(host, "Index 2 repositories").click();
  await settle();
  assert.deepEqual(calls.filter(([m]) => m === "PATCH" && calls).filter(([, p]) => p.startsWith("/api/repositories/")).map(([, p, b]) => [p, b]),
    [["/api/repositories/gh%3A1", { index_enabled: true, implementation_enabled: false }], ["/api/repositories/gh%3A2", { index_enabled: true, implementation_enabled: false }]]);
});

test("Use these assigns the found chat model to answering and planning and the embedding model to search", async () => {
  const { host, calls, rolesSeen } = await setup({ repos: [repo("gh:1", "storefront-app", { index_enabled: true })] });
  assert.equal(rolesSeen.length, 1);
  assert.match(visibleText(host), /Found Ollama with llama3\.2:latest and nomic-embed-text:latest\. Use them\?/);
  btn(host, "Use these").click();
  await settle(30);
  const assignments = calls.filter(([m, p]) => m === "PATCH" && p === "/api/settings/model-roles").map(([, , b]) => b.assignments);
  assert.deepEqual(assignments, [{ ask: "prof_1" }, { planning: "prof_1" }, { embedding: "prof_2" }]);
  assert.equal(rolesSeen.length, 2, "the rerender after assigning reports the new roles to the page");
  assert.ok(host.querySelectorAll("a").find((a) => a.getAttribute("href") === "#/settings/models" && a.textContent === "Choose…"));
});

test("an Ollama on another machine is named in the intro, when found, when empty and when unreachable", async () => {
  const base = "http://100.102.234.29:11434";
  const indexed = [repo("gh:1", "storefront-app", { index_enabled: true })];
  const found = await setup({ repos: indexed, discover: { reachable: true, base_url: base, models: [{ name: "llama3.2:latest", embedding: false }] } });
  assert.match(visibleText(found.host), /Everything stays on this computer and your Ollama machine \(100\.102\.234\.29:11434\)\./);
  assert.match(visibleText(found.host), /Found Ollama on 100\.102\.234\.29:11434 with llama3\.2:latest\. Use them\?/);
  btn(found.host, "Use these").click();
  await settle(30);
  assert.equal(found.calls.find(([m, p]) => m === "POST" && p === "/api/provider-profiles")[2].endpoint, base);
  const empty = await setup({ repos: indexed, discover: { reachable: true, base_url: base, models: [] } });
  assert.match(visibleText(empty.host), /Ollama on 100\.102\.234\.29:11434 has no models yet\./);
  const down = await setup({ repos: indexed, discover: { reachable: false, base_url: base, models: [] } });
  assert.match(visibleText(down.host), /Can't reach Ollama on 100\.102\.234\.29:11434\./);
  assert.doesNotMatch(visibleText(down.host), /No local Ollama models found/);
  const local = await setup({ repos: indexed, discover: { reachable: false, base_url: "http://127.0.0.1:11434", models: [] } });
  assert.match(visibleText(local.host), /Everything stays on this computer\. About 3 minutes\./);
  assert.match(visibleText(local.host), /No local Ollama models found\./);
});

test("without GitHub the first step starts the manifest flow", async () => {
  const { host, submitted } = await setup({ connected: false, repos: [] });
  assert.match(visibleText(host), /Connect GitHub first; your repositories appear here\./);
  btn(host, "Connect GitHub").click();
  await settle();
  assert.equal(submitted.length, 1);
});

test("a finished setup renders nothing and calls onDone; a failing server renders nothing", async () => {
  const doneSetup = await setup({ repos: [repo("gh:1", "storefront-app", { index_enabled: true })], askProfile: "prof_9" });
  assert.equal(visibleText(doneSetup.host), "");
  assert.equal(doneSetup.done(), 1);
  const broken = await setup({ fail: true });
  assert.equal(visibleText(broken.host), "");
});

test("a created but uninstalled App offers Install, never a second Connect", async () => {
  const { host, navigated } = await setup({ connected: false, configured: true, repos: [] });
  assert.equal(btn(host, "Connect GitHub"), undefined);
  btn(host, "Install on your repositories").click();
  await settle();
  assert.deepEqual(navigated, ["https://github.com/apps/opendevengine-99fc7a/installations/new?state=s"]);
});
