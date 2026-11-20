// Behavior tests for the OpenDevEngine web UI.
//
// Each test loads the real opendevengine/web/index.html and executes the real
// opendevengine/web/app.js against a minimal DOM stand-in (./dom.mjs). Responses are
// the accepted contract fixtures in tests/fixtures/api-contract.json, served by
// a fake fetch; the shipped UI itself contains no fixture data.
//
// Run: node --test tests/ui/

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { declaredStyle } from "./css.mjs";
import { createBrowser, input, install, keydown, settle, visibleText } from "./dom.mjs";

const root = new URL("../../", import.meta.url);
const html = readFileSync(new URL("opendevengine/web/index.html", root), "utf8");
const appUrl = pathToFileURL(fileURLToPath(new URL("opendevengine/web/app.js", root))).href;
const css = readFileSync(new URL("opendevengine/web/styles.css", root), "utf8");
const fixtures = JSON.parse(readFileSync(new URL("tests/fixtures/api-contract.json", root), "utf8"));

const LEGACY_404 = fixtures.ui.legacy_fallback.workspace_response;
const DEMO_REPOS = fixtures.legacy.repos_demo;
const LOCAL_REPOS = {
  model: "example-model:latest",
  repos: [
    { name: "billing", revision: "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0", files: 12, indexed_at: "2026-11-13T15:21:52.579534+00:00", demo: false, limited: false },
    { name: "ledger", revision: "0f1e2d3c4b5a69788796a5b4c3d2e1f0a9b8c7d6", files: 3000, indexed_at: "2026-11-13T15:21:52.579534+00:00", demo: false, limited: true },
    { name: "web", revision: "9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b3a2f1e0d", files: 41, indexed_at: "2026-11-13T15:21:52.579534+00:00", demo: false, limited: false },
  ],
};

let loads = 0;

// Boot the app. `api` maps "METHOD /path" to a response, a function returning
// one, or an Error instance (thrown as a network failure).
async function boot({ hash = "", api = {} } = {}) {
  const respond = (method, url) => {
    const path = url.split("?")[0];
    const handler = api[`${method} ${path}`];
    const value = typeof handler === "function" ? handler(url) : handler;
    if (value instanceof Error) throw value;
    return value ?? { status: 404, body: { error: "Page not found." } };
  };
  const browser = createBrowser(html, { hash, respond });
  install(browser);
  loads += 1;
  await import(`${appUrl}?load=${loads}`);
  await settle();
  const { document } = browser;
  const byId = (id) => document.getElementById(id);
  const go = async (href) => {
    document.querySelector(`.primary-nav a[href="${href}"]`).click();
    await settle();
  };
  return { ...browser, byId, go, text: (id) => visibleText(byId(id)) };
}

const legacyApi = () => ({
  "GET /api/repos": DEMO_REPOS,
  "GET /api/workspace": LEGACY_404,
  "GET /api/health": LEGACY_404,
});

test("navigation exposes Ask, Repositories and Settings as keyboard links with aria-current", async () => {
  const app = await boot({ api: legacyApi() });
  const links = app.document.querySelectorAll(".primary-nav a");
  assert.deepEqual(links.map((link) => link.getAttribute("href")), ["#/ask", "#/sessions", "#/repositories", "#/settings"]);
  assert.deepEqual(links.map((link) => link.textContent), ["Ask", "Sessions", "Repositories", "Settings"]);
  assert.equal(app.document.querySelector("nav.primary-nav").getAttribute("aria-label"), "Workspace");
  assert.ok(links.filter((link) => link.dataset.route !== "sessions").every((link) => link.focusable), "available nav links are focusable");
  assert.equal(app.byId("nav-sessions").hidden, true, "the unavailable capability keeps its stable DOM slot hidden");
  assert.equal(app.document.querySelector('.primary-nav a[aria-current="page"]').textContent, "Ask");
  // Initial load must not steal focus.
  assert.equal(app.document.activeElement, app.document.body);
  assert.equal(app.document.title, "Ask · OpenDevEngine");

  for (const [href, view, title] of [
    ["#/repositories", "view-repositories", "repositories-title"],
    ["#/settings", "view-settings", "settings-title"],
    ["#/ask", "view-ask", "ask-title"],
  ]) {
    await app.go(href);
    const views = ["view-ask", "view-repositories", "view-settings"];
    assert.deepEqual(views.filter((id) => !app.byId(id).hidden), [view], `${href} shows only ${view}`);
    const current = app.document.querySelectorAll('.primary-nav a[aria-current="page"]');
    assert.deepEqual(current.map((link) => link.getAttribute("href")), [href]);
    assert.equal(app.document.activeElement, app.byId(title), `${href} moves focus to its h1`);
    assert.equal(app.byId(title).tagName, "H1");
  }
});

test("route focus stays on the accessible H1 while the shell remains the only visible title", async () => {
  // Uses the real styles.css cascade (see css.mjs limits); a real browser check is still recommended before release.
  const app = await boot({ api: legacyApi() });
  const focusedStyle = (id) => declaredStyle(app.byId(id), css, { focused: app.document.activeElement === app.byId(id) });

  const resting = focusedStyle("ask-title");
  assert.equal(resting["clip-path"], "inset(50%)", "Ask h1 stays visually hidden until focused");
  assert.equal(resting.width, "1px");

  await app.go("#/settings");
  assert.equal(app.document.activeElement, app.byId("settings-title"));
  assert.equal(focusedStyle("settings-title")["clip-path"], "inset(50%)");
  assert.equal(app.byId("page-header-title").textContent, "Settings");
  assert.match(app.byId("view-settings").querySelector(".page-lede").textContent, /workspace health/i);
  await app.go("#/repositories");
  assert.equal(app.document.activeElement, app.byId("repositories-title"));
  assert.equal(focusedStyle("repositories-title")["clip-path"], "inset(50%)");
  assert.equal(app.byId("page-header-title").textContent, "Repositories");
  assert.match(app.byId("view-repositories").querySelector(".page-lede").textContent, /Choose which repositories/);

  await app.go("#/ask");
  assert.equal(app.document.activeElement, app.byId("ask-title"));
  assert.equal(app.byId("ask-title").tagName, "H1", "Ask keeps its main heading");
  const focused = focusedStyle("ask-title");
  assert.equal(focused["clip-path"], "none");
  assert.notEqual(focused.width, "1px");
  assert.notEqual(focused.height, "1px");
  assert.notEqual(focused.overflow, "hidden");
  assert.match(focused.outline || "", /solid/);
  assert.notEqual(focused.display, "none");
});

test("legacy 404 fallback: derives sample mode from /api/repos and invents no status", async () => {
  const app = await boot({ hash: "#/settings", api: legacyApi() });
  assert.equal(app.text("workspace-mode"), "◇Sample workspace — bundled example repositories");
  const settings = app.text("settings-content");
  assert.match(settings, /Status unavailable in this build\./);
  for (const invented of [/persistent/i, /in memory/i, /credential/i, /schema/i, /jobs?/i, /OK/, /Configured/]) {
    assert.doesNotMatch(settings, invented);
  }
  assert.equal(app.byId("settings-content").querySelector(".status-error"), null, "fallback is not an error");
  // Initial deep link renders Settings without moving focus.
  assert.equal(app.document.activeElement, app.document.body);
  assert.equal(app.document.querySelectorAll(".primary-nav li").filter((li) => !li.hidden).length, 3);
});

test("network and 5xx failures render a distinct error with a next action, then recover", async () => {
  let failing = true;
  const app = await boot({
    hash: "#/settings",
    api: {
      "GET /api/repos": DEMO_REPOS,
      "GET /api/workspace": () => (failing ? new TypeError("Failed to fetch") : fixtures.workspace.demo_memory),
      "GET /api/health": () => (failing ? fixtures.errors.internal : fixtures.health.demo_memory),
    },
  });
  const settings = app.text("settings-content");
  assert.match(settings, /Could not load workspace status\. Could not reach the OpenDevEngine server/);
  assert.match(settings, /Could not load health status\. The request could not be completed\./);
  assert.match(settings, /Refresh status to try again/);
  assert.doesNotMatch(settings, /Status unavailable in this build/);
  assert.equal(app.byId("settings-content").querySelectorAll(".status-error").length, 2);
  assert.equal(app.byId("settings-content").querySelectorAll('[role="alert"], [aria-live="assertive"]').length, 0,
    "errors do not interrupt assertively");

  // A failing user-requested refresh is announced politely.
  const refresh = app.byId("refresh-status");
  refresh.focus();
  refresh.click();
  await settle();
  await new Promise((resolve) => setTimeout(resolve, 80));
  assert.equal(app.byId("app-status").textContent, "Could not load all workspace status.");

  failing = false;
  refresh.click();
  await settle();
  await new Promise((resolve) => setTimeout(resolve, 80));
  assert.equal(app.document.activeElement, refresh, "refresh keeps focus on its button");
  assert.equal(app.byId("settings-content").querySelector(".status-error"), null);
  assert.match(app.text("settings-content"), /Health/);
  assert.equal(app.byId("app-status").textContent, "Status updated.");
});

test("non-object or wrong-contract responses are errors, never success", async () => {
  const app = await boot({
    hash: "#/settings",
    api: {
      "GET /api/repos": DEMO_REPOS,
      "GET /api/workspace": { status: 404, body: { error: "Not found.", code: "not_found" } },
      "GET /api/health": { ...fixtures.health.demo_memory, contract: "opendevengine.health/m9.0" },
    },
  });
  const settings = app.text("settings-content");
  assert.match(settings, /Could not load workspace status\. Not found\./);
  assert.match(settings, /Could not load health status\. The server reported status in a format/);
  assert.doesNotMatch(settings, /✓/);
});

test("workspace.demo_memory + health.demo_memory render truthful demo status without action buttons", async () => {
  const app = await boot({
    hash: "#/settings",
    api: {
      "GET /api/repos": DEMO_REPOS,
      "GET /api/workspace": fixtures.workspace.demo_memory,
      "GET /api/health": fixtures.health.demo_memory,
    },
  });
  const settings = app.text("settings-content");
  assert.equal(app.text("workspace-mode"), "◇Sample workspace — bundled example repositories");
  assert.match(settings, /Search only/);
  assert.match(settings, /--model NAME/);
  assert.match(settings, /Not persistent: kept in memory/);
  assert.match(settings, /Credential setup is disabled\./);
  assert.match(settings, /GitHub—Not available in this version/);
  assert.match(settings, /Implementation sessions—Not available in this version/);
  assert.match(settings, /Provider profiles—Not available in this version/);
  for (const forbidden of [/GitHub connected/i, /private repositories indexed/i, /session running/i, /Connect GitHub/i, /Start session/i]) {
    assert.doesNotMatch(settings, forbidden);
  }
  assert.deepEqual(app.byId("view-settings").querySelectorAll("button").map((b) => b.textContent), ["Refresh status", "Reload GitHub status", "Reload provider settings"],
    "Unavailable endpoints offer explicit read-only reload controls");
});

test("workspace.local_with_model_persistent shows local mode, loopback model, schema and index limit", async () => {
  const app = await boot({
    hash: "#/settings",
    api: {
      "GET /api/repos": LOCAL_REPOS,
      "GET /api/workspace": fixtures.workspace.local_with_model_persistent,
      "GET /api/health": fixtures.health.persistent_ok,
    },
  });
  const settings = app.text("settings-content");
  assert.equal(app.text("workspace-mode"), "◇Local workspace — local Git checkouts");
  assert.match(settings, /Ollama model example-model:latest\./);
  assert.match(settings, /Destination: loopback/);
  assert.match(settings, /Not checked yet/);
  assert.match(settings, /Persistent local database\. Schema 1\./);
  assert.match(settings, /1 repository hit the index limit/);
  assert.match(settings, /test\.echo failed \(timeout\)/);
  assert.doesNotMatch(visibleText(app.document.body), /Sample workspace/);
  assert.doesNotMatch(settings, /remote/i);
});

test("health.degraded_reconciliation shows degraded text, reconciliation and expired lease, no repo names", async () => {
  const app = await boot({
    hash: "#/settings",
    api: {
      "GET /api/repos": DEMO_REPOS,
      "GET /api/workspace": fixtures.workspace.demo_memory,
      "GET /api/health": fixtures.health.degraded_reconciliation,
    },
  });
  const cards = app.byId("settings-content").querySelectorAll(".status-card");
  const health = cards.find((card) => card.querySelector("h2").textContent === "Health");
  const text = visibleText(health);
  assert.match(text, /!Degraded/, "degraded state has an icon and text, not only color");
  assert.match(text, /1 job needs reconciliation/);
  assert.match(text, /1 expired lease/);
  assert.match(text, /not available in this version/);
  for (const repo of DEMO_REPOS.repos) assert.doesNotMatch(text, new RegExp(repo.name));
});

test("health.no_platform shows storage and jobs disabled, not as errors", async () => {
  const app = await boot({
    hash: "#/settings",
    api: {
      "GET /api/repos": DEMO_REPOS,
      "GET /api/workspace": fixtures.workspace.no_platform,
      "GET /api/health": fixtures.health.no_platform,
    },
  });
  const settings = app.text("settings-content");
  assert.match(settings, /Storage○Disabled/);
  assert.match(settings, /Storage is disabled in this build/);
  assert.match(settings, /Background jobs○Disabled/);
  assert.match(settings, /Background jobs are disabled in this build/);
  assert.equal(app.byId("settings-content").querySelector(".status-error"), null);
  assert.doesNotMatch(settings, /error|failed/i);
});

test("unknown enum values render as neutral Unknown, never success or connected", async () => {
  const workspace = structuredClone(fixtures.workspace.demo_memory);
  workspace.mode = "hybrid"; // "connected" became a real mode in the UI redesign
  workspace.providers.answer.status = "probing";
  workspace.storage.status = "rebuilding";
  workspace.catalog.github.status = "connected";
  const health = structuredClone(fixtures.health.demo_memory);
  health.status = "excellent";
  const app = await boot({
    hash: "#/settings",
    api: { "GET /api/repos": DEMO_REPOS, "GET /api/workspace": workspace, "GET /api/health": health },
  });
  assert.equal(app.text("workspace-mode"), "◇Workspace mode unknown");
  const settings = app.text("settings-content");
  assert.match(settings, /Answer model\?Unknown/);
  assert.match(settings, /Storage\?Unknown/);
  assert.match(settings, /GitHub\?Unknown/);
  assert.match(settings, /Health\?Unknown/);
  assert.doesNotMatch(settings, /connected/i);
  const unknownChips = app.byId("settings-content").querySelectorAll(".chip-neutral").filter((chip) => /Unknown/.test(chip.textContent));
  assert.ok(unknownChips.length >= 5);
});

test("Repositories view lists real /api/repos data read-only", async () => {
  const demo = await boot({ hash: "#/repositories", api: legacyApi() });
  const rows = demo.byId("repositories-content").querySelectorAll("tbody tr").map(visibleText);
  assert.deepEqual(rows, [
    "storefront-web Sample 1 ✓Indexed".replace(/ /g, ""),
    "orders-api Sample 4 ✓Indexed".replace(/ /g, ""),
    "orders-db Sample 1 ✓Indexed".replace(/ /g, ""),
    "notifications-worker Sample 1 ✓Indexed".replace(/ /g, ""),
  ]);
  assert.match(demo.text("repositories-content"), /Bundled example repositories/);
  assert.equal(demo.byId("view-repositories").querySelectorAll("button").some(b => b.textContent === "Save repository selection"), false);

  const local = await boot({
    hash: "#/repositories",
    api: { "GET /api/repos": LOCAL_REPOS, "GET /api/workspace": fixtures.workspace.local_with_model_persistent, "GET /api/health": fixtures.health.persistent_ok },
  });
  const text = local.text("repositories-content");
  assert.match(text, /billinga1b2c3d12/);
  assert.match(text, /ledger0f1e2d33000!Index limit reached: some files were not indexed/);
  assert.match(text, /GitHub: GitHub connection is not available in this version/);
});

test("repository load failure is shown as an error on Ask and Repositories", async () => {
  const app = await boot({
    hash: "#/repositories",
    api: { "GET /api/repos": new TypeError("Failed to fetch"), "GET /api/workspace": LEGACY_404, "GET /api/health": LEGACY_404 },
  });
  assert.match(app.text("repositories-content"), /Could not load repositories\. Failed to fetch/);
  assert.equal(app.text("workspace-mode"), "◇Workspace unavailable");
  assert.equal(app.byId("composer-status").textContent, "Could not load repositories");
});

test("unknown hash routes fall back to Ask and announce it politely", async () => {
  const app = await boot({ hash: "#/sessions", api: legacyApi() });
  assert.equal(app.window.location.hash, "#/ask");
  assert.equal(app.byId("view-ask").hidden, false);
  await new Promise((resolve) => setTimeout(resolve, 80));
  assert.equal(app.byId("app-status").getAttribute("aria-live"), "polite");
  assert.match(app.byId("app-status").textContent, /not available in this version/);
  assert.equal(app.byId("nav-sessions").hidden, true);
  assert.equal(app.document.querySelector('.primary-nav a[href="#/sessions"]').getAttribute("tabindex"), "-1");
});

test("Ask flow is preserved: example, scope, citations and source pane survive navigation", async () => {
  const askBodies = [];
  const sourceUrls = [];
  const app = await boot({
    api: {
      ...legacyApi(),
      "POST /api/ask": () => fixtures.legacy.ask_search_demo,
      "GET /api/source": (url) => {
        sourceUrls.push(url);
        return { first: 1, lines: ["CREATE TABLE orders (", "  id BIGINT PRIMARY KEY", ");"], total: 3 };
      },
    },
  });
  const examples = app.document.querySelectorAll(".example");
  assert.equal(app.byId("examples").hidden, false, "demo examples are shown");
  examples[1].click();
  assert.equal(app.document.activeElement, app.byId("question"));
  assert.match(app.byId("question").value, /orders table/);
  assert.equal(app.byId("scope-label").textContent, "4 repositories");

  // Narrow scope with the picker.
  app.byId("show-repos").click();
  assert.equal(app.byId("repo-dialog").open, true);
  const boxes = app.byId("repo-options").querySelectorAll("input");
  assert.equal(boxes.length, 4);
  boxes[0].click();
  assert.equal(app.byId("repo-count").textContent, "3 selected");
  app.byId("apply-repos").click();
  assert.equal(app.byId("repo-dialog").open, false);
  assert.equal(app.byId("scope-label").textContent, "3 repositories");

  app.byId("ask-button").click();
  await settle();
  const ask = app.requests.find((request) => request.method === "POST");
  askBodies.push(JSON.parse(ask.body));
  assert.deepEqual(askBodies[0].repos, ["orders-api", "orders-db", "notifications-worker"]);
  assert.match(app.text("messages"), /Matching source excerpts/);

  const citations = app.byId("messages").querySelectorAll(".citation");
  assert.equal(citations.length, 2);
  citations[0].focus();
  citations[0].click();
  await settle();
  const params = new URLSearchParams(sourceUrls[0].split("?")[1]);
  assert.deepEqual(Object.fromEntries(params), {
    repo: "orders-db",
    revision: "715ca263a149d8dcfa843da8b3a6ab060dfde58d8f0367045f23086438726c0b",
    path: "migrations/V001__checkout.sql",
    start: "1",
    end: "12",
  });
  assert.match(app.text("source-panel"), /orders-db715ca263a1migrations\/V001__checkout\.sql/);

  // Leaving Ask and returning keeps the conversation and open source pane.
  await app.go("#/settings");
  assert.equal(app.byId("show-repos").hidden, true, "Ask-only scope control hidden elsewhere");
  keydown(app.document.activeElement, "Escape");
  await app.go("#/ask");
  assert.equal(app.byId("show-repos").hidden, false);
  assert.equal(app.byId("source-panel").hidden, false, "Escape on another view did not close the source");
  assert.match(app.text("messages"), /Matching source excerpts/);
  assert.equal(app.byId("history").querySelectorAll(".history-item").length, 1);

  // Escape on Ask closes the source and returns focus to the citation.
  keydown(app.document.activeElement, "Escape");
  assert.equal(app.byId("source-panel").hidden, true);
  assert.equal(app.document.activeElement, citations[0]);

  // Ctrl+Enter still submits.
  input(app.byId("question"), "Which service writes orders?");
  keydown(app.byId("question"), "Enter", { ctrlKey: true });
  await settle();
  assert.equal(app.requests.filter((request) => request.method === "POST").length, 2);
});

test("ask errors are shown, not hidden", async () => {
  const app = await boot({
    api: { ...legacyApi(), "POST /api/ask": { status: 400, body: fixtures.legacy.ask_bad_request } },
  });
  input(app.byId("question"), "Where is checkout?");
  app.byId("ask-button").click();
  await settle();
  assert.match(app.text("messages"), /Enter a question and select valid repositories\./);
  assert.equal(app.byId("question").value, "Where is checkout?", "question restored for retry");
});
