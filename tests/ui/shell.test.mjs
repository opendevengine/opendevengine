// Shell: left rail, header, honest workspace pill and the slide-in side panel.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createBrowser, install, keydown, settle, visibleText } from "./dom.mjs";

const root = new URL("../../", import.meta.url);
const html = readFileSync(new URL("opendevengine/web/index.html", root), "utf8");
const css = readFileSync(new URL("opendevengine/web/styles.css", root), "utf8");
const appUrl = pathToFileURL(fileURLToPath(new URL("opendevengine/web/app.js", root))).href;
const panelUrl = pathToFileURL(fileURLToPath(new URL("opendevengine/web/panel.js", root))).href;
const shellUrl = pathToFileURL(fileURLToPath(new URL("opendevengine/web/shell.js", root))).href;
const fixtures = JSON.parse(readFileSync(new URL("tests/fixtures/api-contract.json", root), "utf8"));
let loads = 0;

async function boot({ hash = "", api = {} } = {}) {
  const respond = (method, url) => {
    const value = api[`${method} ${url.split("?")[0]}`];
    return (typeof value === "function" ? value(url) : value) ?? { status: 404, body: { error: "Page not found." } };
  };
  const browser = createBrowser(html, { hash, respond });
  install(browser);
  loads += 1;
  await import(`${appUrl}?shell=${loads}`);
  await settle();
  return browser;
}

const legacyApi = () => ({
  "GET /api/repos": fixtures.legacy.repos_demo,
  "GET /api/workspace": fixtures.ui.legacy_fallback.workspace_response,
  "GET /api/health": fixtures.ui.legacy_fallback.workspace_response,
});

test("rail holds brand, new conversation, nav and a bottom-pinned Settings link", async () => {
  const b = await boot({ hash: "#/repositories", api: legacyApi() });
  const rail = b.document.getElementById("rail");
  assert.ok(rail, "rail exists");
  assert.match(visibleText(rail.querySelector(".brand")), /OpenDevEngine/);
  assert.equal(b.document.getElementById("rail-new").getAttribute("href"), "#/ask");
  const nav = b.document.getElementById("rail-nav");
  assert.ok(rail.querySelectorAll("nav").includes(nav));
  const settings = nav.querySelector('a[href="#/settings"]');
  assert.ok(settings.closest("li").classList.contains("rail-bottom"));
  assert.equal(nav.querySelector('a[aria-current="page"]').textContent, "Repositories");
  assert.equal(b.document.getElementById("page-header").tagName, "HEADER");
});

test("Sessions keeps a stable nav slot and becomes available without DOM insertion", async () => {
  assert.match(html, /id="nav-sessions"[^>]*class="nav-capability is-pending"|class="nav-capability is-pending"[^>]*id="nav-sessions"/);
  const api = { ...legacyApi(), "GET /api/sessions": { status: 200, body: { items: [], next_cursor: null } },
    "GET /api/execution-profiles": { status: 200, body: { items: [], next_cursor: null } }, "GET /api/settings/execution": { status: 200, body: { max_active_sessions: 1 } } };
  const b = await boot({ api });
  const item = b.document.getElementById("nav-sessions");
  assert.equal(item.classList.contains("is-pending"), false);
  assert.equal(item.getAttribute("aria-hidden"), "false");
  assert.equal(item.querySelector("a").hasAttribute("tabindex"), false);
  const labels = b.document.getElementById("rail-nav").querySelectorAll("li").filter((li) => !li.hidden).map((li) => li.textContent.trim());
  assert.deepEqual(labels, ["Ask", "Sessions", "Repositories", "Settings"]);
});

test("shell foundation has one visible route title and no obsolete topbar height overrides", async () => {
  const b = await boot({ hash: "#/repositories", api: legacyApi() });
  assert.equal(b.document.getElementById("page-header-title").tagName, "P");
  assert.equal(b.document.getElementById("repositories-title").tagName, "H1");
  assert.match(css, /\.page-header \.route-title\s*\{/);
  assert.doesNotMatch(css, /\.page-header \.page-lede/);
  assert.doesNotMatch(css, /\.topbar\b/);
  assert.doesNotMatch(css, /--top-height:\s*(?:96|100)px/);
  assert.match(css, /\.btn, \.button, \.ask-button, \.publication-primary/);
  assert.doesNotMatch(css, /\.persistent-ask button, \.persistent-ask a/);
});

test("modern command-center shell keeps theme, motion and compact-layout safeguards", () => {
  assert.match(html, /name="color-scheme" content="dark light"/);
  assert.match(html, /class="brand-mark"[^>]*>OD</);
  assert.match(css, /--accent:\s*#70e1c0/);
  assert.match(css, /--inset-highlight:/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)/);
  assert.doesNotMatch(css, /transition:\s*all/);
  assert.doesNotMatch(css, /will-change:\s*all/);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*?\.rail \{[^}]*position:\s*sticky/);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*?\.rail \{[^}]*flex-wrap:\s*nowrap[^}]*height:\s*62px/);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*?\.rail-new \{[^}]*min-height:\s*44px/);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*?\.rail \.primary-nav a \{[^}]*min-height:\s*44px/);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*?\.page-top \{[^}]*top:\s*62px/);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*?\.workspace \{[^}]*grid-template-columns:\s*minmax\(0, 1fr\)/);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*?\.workspace \{[^}]*height:\s*calc\(100dvh - 118px\)/);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*?\.inspector\.has-source \{[^}]*top:\s*118px/);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*?\.session-tabs \{[^}]*top:\s*118px/);
  assert.doesNotMatch(css, /var\(--accent\) 88%, white/);
  assert.match(css, /@media \(max-width: 1000px\)[\s\S]*?\.session-pane \{[^}]*position:\s*static[^}]*overflow:\s*clip/);
});

test("route polish styles repository filters and keeps compact targets usable", () => {
  assert.match(css, /\.repo-filters :is\(input, select\) \{[^}]*border:\s*1px solid var\(--line\)/);
  assert.match(css, /\.repo-filters > label, \.repo-filters > input \{[^}]*display:\s*inline-grid/);
  assert.match(css, /\.switch::before \{[^}]*inset:\s*-12px -5px/);
  assert.match(css, /\.sessions-panel \.session-filter\.is-date \{ left: auto; right: 0; box-shadow: var\(--shadow-float\); \}/);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*?\.switch \{ margin-block: 12px; \}/);
  assert.match(css, /:where\(a\) \{ color: var\(--accent\);/);
  assert.doesNotMatch(css, /0 16px 40px #0006;\s*\n?\s*backdrop|-18px 0 54px #0005; \}|--text-muted, #a9b1bf/);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*?\.settings-nav a \{[^}]*min-height:\s*44px[^}]*white-space:\s*nowrap/);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*?\.settings-body \.field-group > \.setting-field \{ flex: 1 1 100%; \}/);
});

test("route bar stays one row when the source or side panel narrows it", () => {
  assert.match(css, /\.page-top-pills \{[^}]*flex-wrap:\s*nowrap[^}]*overflow:\s*hidden/);
  assert.match(css, /\.page-top-pills \.pill \{[^}]*text-overflow:\s*ellipsis/);
  assert.match(css, /\.app-shell:has\(\.inspector\.has-source\) \.workspace-mode-detail/);
});

test("workspace pill is hidden in connected mode", async () => {
  const workspace = structuredClone(fixtures.workspace.demo_memory);
  workspace.mode = "connected";
  const b = await boot({ api: { ...legacyApi(), "GET /api/workspace": { status: 200, body: workspace } } });
  assert.equal(b.document.getElementById("workspace-mode").hidden, true);
});

test("app shell still loads when saved services are missing", async () => {
  const b = await boot({ api: legacyApi() });
  assert.equal(b.document.getElementById("view-ask").hidden, false);
  assert.ok(b.document.getElementById("rail"));
  assert.equal(b.document.getElementById("side-panel").hidden, true);
});

test("createShell sets recent items, header title and pills", async () => {
  const b = await boot({ api: legacyApi() });
  const { createShell } = await import(`${shellUrl}?s=${loads}`);
  const shell = createShell({ document: b.document });
  shell.setRecent([{ href: "#/ask/c1", title: "How is storefront-app structured?", meta: "2 min ago · storefront-app", attention: true }], { heading: "Recent" });
  const recent = b.document.getElementById("recent");
  assert.equal(recent.hidden, false);
  assert.match(visibleText(recent), /Recent/);
  const item = recent.querySelector('a[href="#/ask/c1"]');
  assert.match(visibleText(item), /How is storefront-app structured\?.*2 min ago · storefront-app/s);
  assert.ok(item.querySelector(".attention"));
  assert.equal(item.hasAttribute("title"), false);
  // A recent session label keeps its full plan title as a tooltip while the row truncates it.
  shell.setRecent([{ href: "#/sessions/s1", title: "Fix add() in calc.py", tooltip: "Fix add() in calc.py so test_add passes", meta: "demo · attempt 2 · completed" }], { heading: "Recent sessions" });
  const session = recent.querySelector('a[href="#/sessions/s1"]');
  assert.equal(session.getAttribute("title"), "Fix add() in calc.py so test_add passes");
  assert.match(visibleText(session), /Fix add\(\) in calc\.py.*demo · attempt 2 · completed/s);
  shell.setRecent([], { heading: "Recent" });
  assert.equal(recent.hidden, true);
  shell.setHeader({ title: "Ask", pills: [{ text: "storefront-app" }, { text: "llama3.2 · local", tone: "ok" }] });
  const header = b.document.getElementById("page-header");
  assert.match(visibleText(header), /Ask/);
  assert.deepEqual(header.querySelectorAll(".pill").map((p) => p.textContent), ["storefront-app", "llama3.2 · local"]);
});

test("side panel opens tabs, closes on Escape and restores focus", async () => {
  const b = await boot({ api: legacyApi() });
  const { createSidePanel } = await import(`${panelUrl}?p=${loads}`);
  const opener = b.document.createElement("button");
  b.document.body.append(opener);
  opener.focus();
  const host = b.document.createElement("aside");
  host.hidden = true;
  b.document.body.append(host);
  const panel = createSidePanel(host);
  panel.openTab({ id: "f1", title: "api.ts", render: (body) => body.append("first file") });
  panel.openTab({ id: "f2", title: "routes.ts", render: (body) => body.append("second file") });
  assert.equal(panel.isOpen(), true);
  assert.equal(host.hidden, false);
  assert.deepEqual(host.querySelectorAll('[role="tab"]').map((t) => t.textContent.replace("×", "").trim()), ["api.ts", "routes.ts"]);
  const selected = host.querySelector('[role="tab"][aria-selected="true"]');
  assert.equal(selected.getAttribute("aria-controls"), host.querySelector('[role="tabpanel"]').id);
  assert.equal(host.querySelector('[role="tabpanel"]').getAttribute("aria-labelledby"), selected.id);
  assert.match(visibleText(host), /second file/);
  host.querySelectorAll('[role="tab"]')[0].click();
  assert.match(visibleText(host), /first file/);
  panel.openTab({ id: "f1", title: "api.ts", render: (body) => body.append("first file") });
  assert.equal(host.querySelectorAll('[role="tab"]').length, 2, "reopening an existing tab reuses it");
  panel.closeTab("f2");
  assert.equal(host.querySelectorAll('[role="tab"]').length, 1);
  keydown(host, "Escape");
  assert.equal(panel.isOpen(), false);
  assert.equal(host.hidden, true);
  assert.equal(b.document.activeElement, opener);
});

test("the page header follows the route, not the Ask module", async () => {
  const b = await boot({ hash: "#/repositories", api: legacyApi() });
  assert.equal(b.document.getElementById("page-header-title").textContent, "Repositories");
  b.window.location.hash = "#/settings";
  await settle();
  assert.equal(b.document.getElementById("page-header-title").textContent, "Settings");
});

test("status refreshes and route changes leave Recent conversations visible", async () => {
  const api = { ...legacyApi(), "GET /api/workspace": { status: 200, body: fixtures.workspace.demo_memory }, "GET /api/health": { status: 200, body: fixtures.health.demo_memory } };
  const b = await boot({ api });
  const { createShell } = await import(`${shellUrl}?s=recent`);
  createShell({ document: b.document }).setRecent([{ href: "#/ask/c1", title: "Deploy flow", meta: "now" }], { heading: "Recent" });
  b.window.location.hash = "#/settings";
  await settle();
  b.window.location.hash = "#/repositories";
  await settle();
  const link = b.document.querySelector('#recent-list a[href="#/ask/c1"]');
  assert.equal(link.closest("li").hidden, false);
});

test("opening the panel moves focus into it; Escape anywhere closes it and returns to the latest opener", async () => {
  const b = await boot({ api: legacyApi() });
  const { createSidePanel } = await import(`${panelUrl}?p=focus`);
  const host = b.document.createElement("aside");
  host.hidden = true;
  b.document.body.append(host);
  const panel = createSidePanel(host);
  const first = b.document.createElement("button"), second = b.document.createElement("button");
  b.document.body.append(first, second);
  first.focus();
  panel.openTab({ id: "a", title: "a.ts", render: (body) => body.append("A") });
  let within = false;
  for (let node = b.document.activeElement; node; node = node.parentNode) if (node === host) within = true;
  assert.ok(within, "focus moved into the panel");
  second.focus();
  panel.openTab({ id: "b", title: "b.ts", render: (body) => body.append("B") });
  keydown(b.document.body, "Escape");
  assert.equal(panel.isOpen(), false);
  assert.equal(b.document.activeElement, second);
});

test("compact side panel is modal, contains focus, and restores the background", async () => {
  const b = await boot({ api: legacyApi() });
  let onChange;
  const query = { matches: true, addEventListener: (name, fn) => { if (name === "change") onChange = fn; } };
  globalThis.matchMedia = () => query;
  const { createSidePanel } = await import(`${panelUrl}?p=compact`);
  const main = b.document.createElement("main");
  const opener = b.document.createElement("button");
  const host = b.document.createElement("aside");
  host.hidden = true;
  main.append(opener, host);
  b.document.body.append(main);
  opener.focus();
  const panel = createSidePanel(host);
  panel.openTab({ id: "details", title: "Details", render: (body) => {
    const link = b.document.createElement("a"); link.href = "#details"; link.textContent = "Details";
    const action = b.document.createElement("button"); action.textContent = "Act";
    body.append(link, action);
  } });
  assert.equal(host.getAttribute("role"), "dialog");
  assert.equal(host.getAttribute("aria-modal"), "true");
  assert.equal(opener.inert, true);
  const action = host.querySelectorAll("button").find((node) => node.textContent === "Act");
  action.focus();
  const event = keydown(action, "Tab");
  assert.equal(event.defaultPrevented, true);
  assert.ok(host.querySelector('[role="tab"]') === b.document.activeElement);
  const reverse = keydown(b.document.activeElement, "Tab", { shiftKey: true });
  assert.equal(reverse.defaultPrevented, true);
  assert.equal(b.document.activeElement, action, "reverse Tab reaches the last control in DOM order");
  panel.openTab({ id: "more", title: "More", render: (body) => body.append("More") });
  assert.equal(opener.inert, true, "reopening preserves the original inert snapshot");
  query.matches = false; onChange();
  assert.equal(host.hasAttribute("aria-modal"), false);
  assert.equal(opener.inert, false);
  query.matches = true; onChange();
  assert.equal(host.getAttribute("aria-modal"), "true");
  assert.equal(opener.inert, true);
  panel.close();
  assert.equal(opener.inert, false);
  assert.equal(b.document.activeElement, opener);
});

test("a late conversation history answer on a direct load of Sessions keeps the sessions rail; Ask shows conversations again", async () => {
  let releaseHistory; let historyReads = 0;
  const session = { id: "s1", plan_id: "p1", plan_version_id: "v1", attempt: 2, state: "completed", phase: "completed", repositories: [{ repository_id: "gh:9", role: "write" }], created_at: Date.now() - 5000, publication: {} };
  const api = { ...legacyApi(),
    "GET /api/sessions": { status: 200, body: { items: [session], next_cursor: null } },
    "GET /api/conversations": () => { historyReads += 1; return historyReads === 1 ? new Promise((resolve) => { releaseHistory = resolve; }) : { items: [{ id: "c1", title: "Deploy flow", updated_at: Date.now() }], next_cursor: null }; },
  };
  // app.js awaits the first history read at top level, so this boot does not await the module import.
  const respond = (method, url) => { const value = api[`${method} ${url.split("?")[0]}`]; return (typeof value === "function" ? value(url) : value) ?? { status: 404, body: { error: "Page not found." } }; };
  const b = createBrowser(html, { hash: "#/sessions", respond }); install(b); loads += 1;
  const loaded = import(`${appUrl}?shell=${loads}`);
  for (let i = 0; i < 20 && !releaseHistory; i += 1) await settle();
  await settle();
  const heading = () => b.document.getElementById("recent-heading").textContent;
  const hrefs = () => b.document.getElementById("recent-list").querySelectorAll("a").map((a) => a.getAttribute("href"));
  assert.equal(heading(), "Recent sessions"); assert.deepEqual(hrefs(), ["#/sessions/s1"]);
  assert.ok(releaseHistory, "conversation history was requested at boot");
  releaseHistory({ items: [{ id: "c0", title: "Old chat", updated_at: Date.now() }], next_cursor: null }); await loaded; await settle(); await settle();
  assert.equal(heading(), "Recent sessions", "the late Ask answer does not replace the sessions rail"); assert.deepEqual(hrefs(), ["#/sessions/s1"]);
  // Returning to Ask repopulates conversations (app.js refreshes history when leaving Sessions).
  b.window.location.hash = "#/ask"; for (let i = 0; i < 5; i += 1) await settle();
  assert.equal(heading(), "Recent"); assert.ok(hrefs().includes("#/ask/c1") || hrefs().includes("#/ask/c0"), hrefs().join(","));
  assert.ok(!hrefs().some((h) => h.startsWith("#/sessions/")));
});

test("returning to Ask from any page restores conversations in the rail (Ask → Sessions → Settings → Ask)", async () => {
  const session = { id: "s1", plan_id: "p1", plan_version_id: "v1", attempt: 1, state: "completed", phase: "completed", repositories: [], created_at: Date.now() - 5000, publication: {} };
  const api = { ...legacyApi(),
    "GET /api/sessions": { status: 200, body: { items: [session], next_cursor: null } },
    "GET /api/conversations": { status: 200, body: { items: [{ id: "c1", title: "Deploy flow", updated_at: Date.now() }], next_cursor: null } },
  };
  const b = await boot({ hash: "#/ask", api });
  const heading = () => b.document.getElementById("recent-heading").textContent;
  const hrefs = () => b.document.getElementById("recent-list").querySelectorAll("a").map((a) => a.getAttribute("href"));
  const go = async (hash) => { b.window.location.hash = hash; for (let i = 0; i < 6; i += 1) await settle(); };
  assert.equal(heading(), "Recent"); assert.deepEqual(hrefs(), ["#/ask/c1"]);
  await go("#/sessions"); assert.equal(heading(), "Recent sessions"); assert.deepEqual(hrefs(), ["#/sessions/s1"]);
  await go("#/settings"); assert.equal(heading(), "Recent sessions", "Settings does not own the rail");
  await go("#/ask"); assert.equal(heading(), "Recent"); assert.deepEqual(hrefs(), ["#/ask/c1"]);
  // Moving within Ask does not rebuild the rail.
  const before = b.document.getElementById("recent-list").firstChild;
  await go("#/ask/c1"); assert.equal(b.document.getElementById("recent-list").firstChild, before);
});
