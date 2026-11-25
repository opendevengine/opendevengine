import { createAskPanel } from "./ask.js";
import { createRepositoriesPage } from "./repositories.js";
import { createSettingsPage } from "./settings.js";
import { mountModels } from "./settings-models.js";
import { createProvidersSection } from "./settings-providers.js";
import { createSessionPanel, createExecutionSettings } from "./sessions.js";
import { createShell } from "./shell.js";
import { createSidePanel } from "./panel.js";
const byId = (id) => document.getElementById(id);
const shell = createShell({ document });
const sidePanel = createSidePanel(byId("side-panel"));

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

const question = byId("question");
const thread = byId("thread");
const messages = byId("messages");
const dialog = byId("repo-dialog");
const repoFilter = byId("repo-filter");
const state = {
  repos: [],
  conversations: [],
  current: null,
  pending: false,
  picker: new Set(),
  sourceRequest: null,
  sourceTrigger: null,
  reposLoaded: false,
  reposError: null,
  route: "ask",
  // Workspace and health come from /api/workspace and /api/health. Each is
  // {kind: "loading" | "ok" | "legacy" | "error", data?, message?}.
  status: { workspace: { kind: "loading" }, health: { kind: "loading" }, loading: false, sequence: 0 },
};

async function api(path, options) {
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "The request failed.");
  return result;
}

function repoLabel(count) {
  return count === 1 ? "1 repository" : `${count} repositories`;
}

function syncControls() {
  const ready = Boolean(state.current) && !state.pending;
  byId("show-repos").disabled = !ready;
  byId("new-chat").disabled = !ready;
  question.disabled = !ready;
  byId("ask-button").disabled = !ready || !question.value.trim();
  if (state.current) {
    byId("scope-label").textContent = repoLabel(state.current.repos.length);
  }
  if (ready) {
    byId("composer-status").textContent = state.model ? "Local model" : "Search only · Add Ollama for generated answers";
  }
}

function newConversation(repos) {
  return { repos: [...repos], turns: [] };
}

function switchConversation(conversation, draft = "") {
  state.current = conversation;
  question.value = draft;
  closeSource();
  toggleHistory(false);
  renderThread();
  syncControls();
  question.focus();
}

function renderHistory() {
  const list = byId("history");
  byId("history-empty").hidden = state.conversations.length > 0;
  list.replaceChildren();
  for (const conversation of state.conversations) {
    const label = conversation.turns[0].question;
    const button = node("button", label, "history-item");
    button.type = "button";
    button.title = label;
    button.disabled = state.pending;
    if (conversation === state.current) button.setAttribute("aria-current", "true");
    button.addEventListener("click", () => switchConversation(conversation));
    list.append(button);
  }
}

function addInlineCode(element, text) {
  for (const part of text.split(/(`[^`\n]+`)/)) {
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      element.append(node("code", part.slice(1, -1)));
    } else {
      element.append(document.createTextNode(part));
    }
  }
}

function citationButton(source, inList = false) {
  const name = source.path.split("/").at(-1);
  const lines = source.start === source.end ? `L${source.start}` : `L${source.start}–${source.end}`;
  const button = node("button", undefined, inList ? "citation source-result" : "citation");
  button.type = "button";
  button.title = `${source.repo}/${source.path} · ${lines}`;
  button.setAttribute("aria-controls", "inspector");
  if (inList) button.append(node("span", "›", "result-icon"));
  const file = node("span", name, "citation-name");
  if (inList) {
    const label = node("span", undefined, "result-label");
    label.append(file, node("small", source.repo));
    button.append(label);
  } else {
    button.append(file);
  }
  button.append(node("span", lines, "citation-lines"));
  button.addEventListener("click", () => showSource(source, button));
  return button;
}

function renderAnswer(result) {
  const answer = node("article", undefined, "answer");
  const bySourceId = new Map(result.sources.map((source) => [source.id, source]));
  answer.append(node("h2", result.mode === "answer" ? "Answer" : "Matching source excerpts", "answer-heading"));
  for (const section of result.sections) {
    const block = node("section", undefined, "answer-section");
    const paragraph = node("p");
    addInlineCode(paragraph, section.text);
    block.append(node("h3", section.heading), paragraph);
    const references = node("div", undefined, "citations");
    for (const id of new Set(section.sources)) references.append(citationButton(bySourceId.get(id)));
    block.append(references);
    answer.append(block);
  }
  if (result.note) answer.append(node("p", result.note, "answer-note"));
  if (result.matches.length) {
    const matches = node("section", undefined, "cross-references");
    matches.append(node("h3", "Cross-repository matches"));
    for (const match of result.matches) {
      const row = node("div", undefined, "cross-reference");
      const label = node("p");
      label.append(document.createTextNode(`${match.label} `), node("code", match.literal));
      const refs = node("div", undefined, "citations");
      for (const id of match.sources) refs.append(citationButton(bySourceId.get(id)));
      row.append(label, refs);
      matches.append(row);
    }
    answer.append(matches);
  }
  if (result.mode === "search" && result.sources.length) {
    const sources = node("div", undefined, "search-results");
    for (const source of result.sources) sources.append(citationButton(source, true));
    answer.append(sources);
  }
  return answer;
}

function renderThread(announce = false) {
  const turns = state.current.turns;
  byId("intro").hidden = turns.length > 0;
  messages.hidden = turns.length === 0;
  messages.replaceChildren();
  for (const turn of turns) {
    const item = node("div", undefined, "turn");
    const prompt = node("div", undefined, "prompt");
    prompt.append(node("span", "You", "small-heading"), node("p", turn.question));
    item.append(prompt);
    if (turn.result) item.append(renderAnswer(turn.result));
    else if (turn.error) item.append(node("p", turn.error, "error"));
    else item.append(node("p", state.model ? "Searching and drafting an answer…" : "Searching the selected repositories…", "pending"));
    messages.append(item);
  }
  thread.setAttribute("aria-busy", String(state.pending));
  renderHistory();
  if (announce && messages.lastElementChild) {
    requestAnimationFrame(() => {
      messages.lastElementChild?.scrollIntoView({ block: "start", behavior: "instant" });
    });
  } else {
    thread.scrollTop = 0;
  }
}

byId("ask-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = question.value.trim();
  if (!text || !state.current || state.pending) return;
  const conversation = state.current;
  const previous = conversation.turns.at(-1)?.question ?? "";
  const turn = { question: text };
  if (!conversation.turns.length) state.conversations.unshift(conversation);
  conversation.turns.push(turn);
  state.pending = true;
  question.value = "";
  byId("composer-status").textContent = "Working…";
  syncControls();
  renderThread(true);
  try {
    turn.result = await api("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: text, previous, repos: conversation.repos }),
    });
  } catch (error) {
    turn.error = error.message;
    question.value = text;
  } finally {
    state.pending = false;
    renderThread(true);
    syncControls();
  }
});

question.addEventListener("input", syncControls);
question.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    byId("ask-form").requestSubmit();
  }
});
byId("new-chat").addEventListener("click", () => switchConversation(newConversation(state.current.repos)));
for (const button of document.querySelectorAll(".example")) {
  button.addEventListener("click", () => {
    question.value = button.dataset.question;
    syncControls();
    question.focus();
  });
}

function updatePickerCount() {
  byId("repo-count").textContent = `${state.picker.size} selected`;
  byId("apply-repos").disabled = !state.picker.size;
}

function renderPicker() {
  const list = byId("repo-options");
  const filter = repoFilter.value.trim().toLowerCase();
  list.replaceChildren();
  for (const repo of state.repos.filter(({ name }) => name.toLowerCase().includes(filter))) {
    const option = node("label", undefined, "repo-option");
    const checkbox = node("input");
    checkbox.type = "checkbox";
    checkbox.checked = state.picker.has(repo.name);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.picker.add(repo.name);
      else state.picker.delete(repo.name);
      updatePickerCount();
    });
    const label = node("span", undefined, "repo-option-label");
    const fileCount = repo.files === 1 ? "1 file" : `${repo.files} files`;
    label.append(node("strong", repo.name), node("small", `${fileCount}${repo.limited ? " · Index limit reached" : ""}`));
    const revision = node("span", repo.demo ? "Sample" : repo.revision.slice(0, 7), "repo-revision");
    revision.title = `Indexed ${new Date(repo.indexed_at).toLocaleString()}`;
    option.append(checkbox, label, revision);
    list.append(option);
  }
  if (!list.childElementCount) list.append(node("p", "No matching repositories.", "no-repos"));
  updatePickerCount();
}

byId("show-repos").addEventListener("click", () => {
  state.picker = new Set(state.current.repos);
  byId("repo-dialog-help").textContent = state.current.turns.length
    ? "Changing the repository selection starts a new conversation."
    : "Search and answers use the repositories you select.";
  repoFilter.value = "";
  renderPicker();
  dialog.showModal();
  repoFilter.focus();
});
repoFilter.addEventListener("input", renderPicker);
byId("close-repos").addEventListener("click", () => dialog.close());
byId("cancel-repos").addEventListener("click", () => dialog.close());
byId("repo-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!state.picker.size) return;
  const repos = state.repos.map(({ name }) => name).filter((name) => state.picker.has(name));
  dialog.close();
  const old = state.current.repos;
  if (repos.length !== old.length || repos.some((name) => !old.includes(name))) {
    if (state.current.turns.length) switchConversation(newConversation(repos), question.value);
    else {
      state.current.repos = repos;
      syncControls();
    }
  }
});

function toggleHistory(open) {
  byId("sidebar").classList.toggle("is-open", open);
  byId("history-toggle").setAttribute("aria-expanded", String(open));
}
byId("history-toggle").addEventListener("click", () => {
  toggleHistory(!byId("sidebar").classList.contains("is-open"));
});

function closeSource(restoreFocus = false) {
  state.sourceRequest?.abort();
  state.sourceRequest = null;
  byId("inspector").classList.remove("has-source");
  byId("source-panel").hidden = true;
  byId("source-panel").replaceChildren();
  byId("inspector-empty").hidden = false;
  byId("close-source").hidden = true;
  if (restoreFocus && state.sourceTrigger?.isConnected) state.sourceTrigger.focus();
  state.sourceTrigger = null;
}

const tokenPattern = /("[^"\n]*"|'[^'\n]*'|\/\/.*$|--\s.*$|\b(?:import|package|export|async|await|const|var|let|type|return|if|throw|new|public|private|class|void|final|for|in|def|from|CREATE|TABLE|PRIMARY|KEY|VARCHAR|TEXT|TIMESTAMP|REFERENCES|INSERT|INTO|VALUES|NULL)\b|@[A-Za-z]+|\b\d+\b)/g;

function highlight(code, line) {
  for (const [index, part] of line.split(tokenPattern).entries()) {
    if (index % 2) {
      const category = /^["']/.test(part) ? "string" : /^(\/\/|--)/.test(part) ? "comment" : "keyword";
      code.append(node("span", part, `token-${category}`));
    } else {
      code.append(document.createTextNode(part));
    }
  }
}

async function showSource(citation, trigger) {
  state.sourceRequest?.abort();
  const request = new AbortController();
  state.sourceRequest = request;
  state.sourceTrigger = trigger;
  toggleHistory(false);
  const inspector = byId("inspector");
  const panel = byId("source-panel");
  const close = byId("close-source");
  inspector.classList.add("has-source");
  byId("inspector-empty").hidden = true;
  close.hidden = false;
  panel.hidden = false;
  panel.replaceChildren(node("p", "Loading source…", "source-status"));
  if (matchMedia("(max-width: 1000px)").matches) close.focus();

  const { repo, revision, path, start, end } = citation;
  const params = new URLSearchParams({ repo, revision, path, start, end });
  try {
    const source = await api(`/api/source?${params}`, { signal: request.signal });
    if (state.sourceRequest !== request) return;
    const header = node("div", undefined, "source-header");
    const revisionLabel = node("span", revision.slice(0, 10), "source-revision");
    revisionLabel.title = revision;
    const identity = node("div", undefined, "source-identity");
    identity.append(node("span", repo), revisionLabel);
    header.append(identity, node("p", path, "source-path"));
    const viewer = node("div", undefined, "code-viewer");
    viewer.setAttribute("tabindex", "0");
    viewer.setAttribute("aria-label", `${path}, cited lines ${start} to ${end}`);
    for (const [offset, line] of source.lines.entries()) {
      const number = source.first + offset;
      const row = node("div", undefined, "code-row");
      if (start <= number && number <= end) row.classList.add("highlighted");
      const gutter = node("span", number, "gutter");
      gutter.setAttribute("aria-hidden", "true");
      const code = node("code");
      highlight(code, line);
      row.append(gutter, code);
      viewer.append(row);
    }
    panel.replaceChildren(header, viewer);
  } catch (error) {
    if (state.sourceRequest === request) panel.replaceChildren(node("p", error.message, "error"));
  }
}

byId("close-source").addEventListener("click", () => closeSource(true));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !dialog.open && state.route === "ask") {
    if (!byId("source-panel").hidden) closeSource(true);
    else toggleHistory(false);
  }
});

// ---------------------------------------------------------------------------
// Navigation (hash routes, contract opendevengine.m0/1 §7)

const ROUTES = {
  ask: { view: "view-ask", title: "ask-title", label: "Ask" },
  repositories: { view: "view-repositories", title: "repositories-title", label: "Repositories" },
  settings: { view: "view-settings", title: "settings-title", label: "Settings" },
  sessions: { view: "view-sessions", title: "sessions-title", label: "Sessions" },
};

function announce(text) {
  const region = byId("app-status");
  region.textContent = "";
  // Clearing first makes repeated messages announce again.
  setTimeout(() => { region.textContent = text; }, 50);
}

function routeFromHash() {
  const hash = location.hash;
  if (hash === "" || hash === "#" || hash === "#/") return "ask";
  if (/^#\/(ask|plans)\/[^/]+(?:\/[^/]+)?$/.test(hash)) return "ask";
  if (/^#\/sessions\/[^/]+$/.test(hash)) return "sessions";
  if (/^#\/settings\/(github|providers|models|agent|privacy|health)(?:\?github=[a-z_]{1,64})?$/.test(hash)) return "settings";
  const match = /^#\/([a-z]+)(?:\?github=[a-z_]{1,64})?$/.exec(hash);
  return match && Object.hasOwn(ROUTES, match[1]) ? match[1] : null;
}

let persistentAsk = null;
function showRoute(moveFocus) {
  if (persistentAsk && !persistentAsk.canNavigate()) return;
  let route = routeFromHash();
  if (route === null) {
    route = "ask";
    history.replaceState(null, "", "#/ask");
    announce("That page is not available in this version. Showing Ask.");
  }
  const previous = state.route;
  state.route = route;
  for (const [id, info] of Object.entries(ROUTES)) byId(info.view).hidden = id !== route;
  shell.setRoute(route);
  if (route !== "ask") shell.setHeader({ title: ROUTES[route].label });
  // Recent is scoped to the area: sessions replace conversations while in Sessions.
  if (route === "ask" && previous === "sessions") persistentAsk?.refreshHistory();
  // Ask-only controls act on the current conversation.
  const onAsk = route === "ask";
  byId("show-repos").hidden = !onAsk || Boolean(persistentAsk);
  byId("history-toggle").hidden = !onAsk || Boolean(persistentAsk);
  if (!onAsk) {
    toggleHistory(false);
    if (dialog.open) dialog.close();
  }
  document.title = `${ROUTES[route].label} · OpenDevEngine`;
  if (route === "repositories") renderRepositories();
  if (route === "settings") {
    renderSettings();
    if (moveFocus && previous !== "settings") refreshStatus();
  }
  if (moveFocus) {
    if (route === "ask" && persistentAsk) persistentAsk.focus();
    else byId(ROUTES[route].title).focus();
  }
}

window.addEventListener("hashchange", () => showRoute(true));

// ---------------------------------------------------------------------------
// Workspace status (/api/workspace, /api/health)

async function fetchStatus(path, contractPrefix) {
  let response;
  try {
    response = await fetch(path, { headers: { Accept: "application/json" } });
  } catch {
    return { kind: "error", message: "Could not reach the OpenDevEngine server. Check that it is still running." };
  }
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  const isObject = body !== null && typeof body === "object" && !Array.isArray(body);
  // Builds without the M0 routes answer with the legacy 404 body, which has no `code`.
  if (response.status === 404 && isObject && typeof body.error === "string" && !("code" in body)) {
    return { kind: "legacy" };
  }
  if (!response.ok || !isObject) {
    const detail = isObject && typeof body.error === "string" ? body.error : `The server returned HTTP ${response.status}.`;
    return { kind: "error", message: detail };
  }
  if (typeof body.contract !== "string" || !(body.contract.startsWith(contractPrefix) || body.contract.startsWith(contractPrefix.replace("/m0.", "/m1.")))) {
    return { kind: "error", message: "The server reported status in a format this page does not support." };
  }
  return { kind: "ok", data: body };
}

async function refreshStatus(userRequested = false) {
  const sequence = ++state.status.sequence;
  state.status.loading = true;
  renderRefreshButton();
  const [workspace, health] = await Promise.all([
    fetchStatus("/api/workspace", "opendevengine.workspace/m0."),
    fetchStatus("/api/health", "opendevengine.health/m0."),
  ]);
  if (sequence !== state.status.sequence) return;
  Object.assign(state.status, { workspace, health, loading: false });
  renderRefreshButton();
  applyNavigation();
  renderWorkspaceMode();
  if (state.route === "settings") renderSettings();
  if (state.route === "repositories") renderRepositories();
  if (userRequested) {
    const failed = workspace.kind === "error" || health.kind === "error";
    announce(failed ? "Could not load all workspace status." : "Status updated.");
  }
}

function renderRefreshButton() {
  const button = byId("refresh-status");
  button.textContent = state.status.loading ? "Checking…" : "Refresh status";
  button.setAttribute("aria-disabled", String(state.status.loading));
}

byId("refresh-status").addEventListener("click", () => {
  if (!state.status.loading) refreshStatus(true);
});

function exposeSessions() {
  connectedUI.sessionsReady = true;
  shell.setNavAvailability("sessions", "available");
  applyNavigation();
}
function applyNavigation() {
  const { workspace } = state.status;
  if (workspace.kind !== "ok" || !Array.isArray(workspace.data.navigation)) return;
  const ids = workspace.data.navigation.map((entry) => entry && entry.id);
  for (const link of document.querySelectorAll(".primary-nav a[data-route]")) {
    // Ask always stays reachable; other areas follow the server's list.
    if (link.dataset.route === "sessions") {
      if (connectedUI.sessionsReady || ids.includes("sessions")) shell.setNavAvailability("sessions", "available");
      continue;
    }
    link.parentNode.hidden = link.dataset.route !== "ask" && !ids.includes(link.dataset.route);
  }
}

function workspaceMode() {
  const { workspace } = state.status;
  if (workspace.kind === "ok") return workspace.data.mode;
  if (state.reposLoaded) return state.repos.every((repo) => repo.demo) ? "demo" : "local";
  return null;
}

function renderWorkspaceMode() {
  const mode = workspaceMode();
  let label = "Loading workspace…";
  let detail = "";
  if (mode === "demo") [label, detail] = ["Sample workspace", "bundled example repositories"];
  else if (mode === "local") [label, detail] = ["Local workspace", "local Git checkouts"];
  else if (mode !== null) label = "Workspace mode unknown";
  else if (state.reposError) label = "Workspace unavailable";
  byId("workspace-mode-label").textContent = label;
  byId("workspace-mode-detail").textContent = detail ? ` — ${detail}` : "";
  byId("workspace-mode").dataset.mode = mode === "demo" || mode === "local" ? mode : "unknown";
  shell.setWorkspaceMode(mode);
}

// Status vocabulary. Every state has text and an icon so color is never the
// only signal. Values outside a field's known set render as neutral Unknown.
const STATUS_LABELS = {
  ok: ["good", "✓", "OK"],
  available: ["good", "✓", "Available"],
  configured: ["good", "✓", "Configured"],
  not_configured: ["attention", "○", "Not configured"],
  not_available: ["neutral", "—", "Not available in this version"],
  unavailable: ["attention", "⊘", "Unavailable"],
  disabled: ["neutral", "○", "Disabled"],
  degraded: ["warning", "!", "Degraded"],
};
const UNKNOWN_STATUS = ["neutral", "?", "Unknown"];

function statusOf(value, allowed) {
  return allowed.includes(value) && Object.hasOwn(STATUS_LABELS, value) ? STATUS_LABELS[value] : UNKNOWN_STATUS;
}

function chip([tone, icon, text]) {
  const element = node("span", undefined, `chip chip-${tone}`);
  const glyph = node("span", icon, "chip-icon");
  glyph.setAttribute("aria-hidden", "true");
  element.append(glyph, node("span", text));
  return element;
}

function field(object, ...path) {
  let value = object;
  for (const key of path) {
    if (value === null || typeof value !== "object") return undefined;
    value = value[key];
  }
  return value;
}

function count(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
}

function plural(n, word, many = `${word}s`) {
  return `${n} ${n === 1 ? word : many}`;
}

function card(title, status, ...lines) {
  const section = node("section", undefined, "status-card");
  const header = node("div", undefined, "status-card-header");
  header.append(node("h2", title), chip(status));
  section.append(header);
  for (const line of lines) if (line) section.append(typeof line === "string" ? node("p", line) : line);
  return section;
}

// Paragraph with inline `code` spans, e.g. command-line flags.
function hint(text, className = "next-action") {
  const paragraph = node("p", undefined, className);
  addInlineCode(paragraph, text);
  return paragraph;
}

function errorBlock(message, next) {
  // Not a live region: refreshes announce through the single polite #app-status.
  const block = node("div", undefined, "status-error");
  block.append(node("p", message, "error"));
  if (next) block.append(hint(next));
  return block;
}

function formatTime(value) {
  const date = typeof value === "string" ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime()) ? date.toLocaleString() : "an unknown time";
}

function workspaceCard(data) {
  const mode = data.mode;
  const modeStatus = mode === "demo" ? ["neutral", "◇", "Sample workspace"]
    : mode === "local" ? ["neutral", "◇", "Local workspace"] : UNKNOWN_STATUS;
  const source = field(data, "catalog", "source");
  const sourceText = source === "demo_fixtures" ? "Bundled example repositories. No GitHub or private repositories are indexed."
    : source === "local_checkouts" ? "Local Git checkouts, indexed from their committed HEAD when OpenDevEngine started."
    : "Repository source unknown.";
  const total = count(field(data, "catalog", "repository_count"));
  const limited = count(field(data, "catalog", "limited_count"));
  const totals = total === null ? "Repository count unknown." : `${plural(total, "repository", "repositories")} indexed.`;
  const limits = limited === null ? null
    : limited ? `${plural(limited, "repository", "repositories")} hit the index limit; some files were not indexed.`
    : "No repository hit the index limit.";
  return card("Workspace", modeStatus, sourceText, totals, limits);
}

function modelCard(data, health) {
  const answer = field(data, "providers", "answer") || {};
  const status = statusOf(answer.status, ["configured", "not_configured"]);
  if (answer.status === "not_configured") {
    return card("Answer model", status,
      "Search only: answers show matching source excerpts without generated text.",
      hint("To generate answers, restart OpenDevEngine with `--model NAME` to use a local Ollama model."));
  }
  if (answer.status !== "configured") return card("Answer model", status, "Model status unknown.");
  const adapter = answer.adapter === "ollama" ? "Ollama" : "Unknown adapter";
  const model = typeof answer.model === "string" ? answer.model : "unknown model";
  const destination = answer.destination === "loopback"
    ? "Destination: loopback — prompts go to Ollama on this computer."
    : answer.destination === "configured_host"
      ? "Destination: the Ollama machine set with --ollama-url — prompts and source go there."
      : "Destination unknown.";
  const probed = field(health, "components", "model", "probed") === false
    ? "Not checked yet: OpenDevEngine does not contact the model until you ask a question." : null;
  return card("Answer model", status, `${adapter} model ${model}.`, destination, probed);
}

function notAvailableCard(title, value) {
  const status = statusOf(field(value, "status"), ["not_available"]);
  const reason = typeof field(value, "reason") === "string" ? value.reason : null;
  return card(title, status, reason ? hint(reason, "") : "Status unknown.");
}

function storageCard(data, health) {
  const storage = field(data, "storage") || {};
  const status = statusOf(storage.status, ["ok", "disabled"]);
  if (storage.status === "disabled") return card("Storage", status, "Storage is disabled in this build. Nothing is saved.");
  if (storage.status !== "ok") return card("Storage", status, "Storage status unknown.");
  const schema = count(storage.schema_version);
  const expected = count(field(health, "components", "storage", "expected_schema_version"));
  const size = count(field(health, "components", "storage", "size_bytes"));
  const schemaText = schema === null ? "Schema version unknown." : `Schema ${schema}.`;
  const mismatch = schema !== null && expected !== null && schema !== expected
    ? node("p", `! Schema ${schema} does not match the expected schema ${expected}.`, "warning-text") : null;
  if (storage.persistent === true) {
    const sizeText = size === null ? null : `Database size ${(size / 1024).toFixed(1)} KiB.`;
    return card("Storage", status, `Persistent local database. ${schemaText}`, mismatch, sizeText);
  }
  if (storage.persistent === false) {
    return card("Storage", status, "Not persistent: kept in memory and discarded when OpenDevEngine stops.", mismatch);
  }
  return card("Storage", status, "Persistence unknown.", mismatch);
}

function secretsCard(data) {
  const secrets = field(data, "secrets") || {};
  const value = secrets.available === true ? "available" : secrets.available === false ? "unavailable" : null;
  const status = statusOf(value, ["available", "unavailable"]);
  const reason = typeof secrets.reason === "string" ? secrets.reason : null;
  if (value === "unavailable") {
    return card("Credentials", status, reason || "Credential setup is disabled.",
      "Unlock or configure the secure credential store before saving App keys or provider credentials.");
  }
  if (value === "available") return card("Credentials", status, "A secure credential store is available.");
  return card("Credentials", status, "Credential store status unknown.");
}

function healthCard(data) {
  const status = statusOf(data.status, ["ok", "degraded"]);
  const jobs = field(data, "components", "jobs") || {};
  const lines = [`Checked ${formatTime(data.checked_at)}.`];
  const list = node("dl", undefined, "health-list");
  const row = (name, value, allowed, text) => {
    const term = node("dt", name);
    const description = node("dd");
    description.append(chip(statusOf(value, allowed)));
    if (text) description.append(node("span", text, "health-detail"));
    list.append(term, description);
  };
  const indexCount = count(field(data, "components", "index", "repository_count"));
  row("Index", field(data, "components", "index", "status"), ["ok"],
    indexCount === null ? "" : `${plural(indexCount, "repository", "repositories")}, rebuilt at startup`);
  row("Storage", field(data, "components", "storage", "status"), ["ok", "disabled"], "");
  row("Background jobs", jobs.status, ["ok", "degraded", "disabled"], "");
  row("Credentials", field(data, "components", "secrets", "status"), ["available", "unavailable"], "");
  row("Answer model", field(data, "components", "model", "status"), ["configured", "not_configured"], "");
  lines.push(list);

  if (jobs.status === "disabled") {
    lines.push("Background jobs are disabled in this build.");
  } else if (jobs.counts && typeof jobs.counts === "object") {
    const names = [["queued", "Queued"], ["leased", "Leased"], ["succeeded", "Succeeded"], ["failed", "Failed"],
      ["cancelled", "Cancelled"], ["needs_reconciliation", "Needs reconciliation"]];
    lines.push(`Jobs: ${names.map(([key, label]) => `${label} ${count(jobs.counts[key]) ?? "unknown"}`).join(" · ")}.`);
  }
  const reconcile = count(field(jobs, "counts", "needs_reconciliation"));
  if (reconcile) {
    lines.push(node("p", `! ${plural(reconcile, "job needs", "jobs need")} reconciliation: the outcome is unknown, so ${reconcile === 1 ? "it is" : "they are"} not retried automatically.`, "warning-text"));
  }
  const expired = count(jobs.expired_leases);
  if (expired) lines.push(node("p", `! ${plural(expired, "expired lease")}.`, "warning-text"));
  if (reconcile || expired) lines.push("Tools to resolve jobs are not available in this version.");
  if (jobs.workers_running === 0 && jobs.status !== "disabled") lines.push("No background worker runs in this version.");

  const failures = Array.isArray(data.recent_failures) ? data.recent_failures : [];
  if (failures.length) {
    const failureList = node("ul", undefined, "failure-list");
    for (const failure of failures.slice(0, 5)) {
      const kind = typeof failure?.kind === "string" ? failure.kind : "unknown kind";
      const code = typeof failure?.code === "string" ? failure.code : "unknown";
      failureList.append(node("li", `${kind} failed (${code}) at ${formatTime(failure?.at)}`));
    }
    const heading = node("h3", "Recent job failures");
    lines.push(heading, failureList);
  }
  return card("Health", status, ...lines);
}

const healthHost = node("div", undefined, "health-host");
function renderLegacySettings() {
  const root = connectedSettings() ? healthHost : byId("settings-content");
  const { workspace, health } = state.status;
  const parts = [];
  if (workspace.kind === "loading") {
    root.replaceChildren(node("p", "Loading status…", "page-status"));
    return;
  }
  if (workspace.kind === "legacy") {
    parts.push(node("p", "Status unavailable in this build.", "page-status"));
    parts.push(node("p", "Ask and Repositories still work.", "page-note"));
  } else if (workspace.kind === "error") {
    parts.push(errorBlock(`Could not load workspace status. ${workspace.message}`, "Choose Refresh status to try again."));
  } else {
    const data = workspace.data;
    const healthData = health.kind === "ok" ? health.data : null;
    const grid = node("div", undefined, "status-grid");
    grid.append(
      workspaceCard(data),
      ...(connectedUI.savedAskReady ? [] : [modelCard(data, healthData)]),
      ...(connectedUI.providersReady ? [] : [notAvailableCard("Provider profiles", field(data, "providers", "profiles"))]),
      ...(connectedUI.githubReady ? [] : [notAvailableCard("GitHub", field(data, "catalog", "github"))]),
      ...(connectedUI.sessionsReady ? [] : [notAvailableCard("Implementation sessions", field(data, "execution"))]),
      storageCard(data, healthData),
      secretsCard(data),
    );
    parts.push(grid);
  }
  if (health.kind === "ok") parts.push(healthCard(health.data));
  else if (health.kind === "error") {
    parts.push(errorBlock(`Could not load health status. ${health.message}`, "Choose Refresh status to try again."));
  } else if (health.kind === "legacy" && workspace.kind !== "legacy") {
    parts.push(node("p", "Health status unavailable in this build.", "page-status"));
  }
  root.replaceChildren(...parts);
}

function renderLegacyRepositories() {
  const root = byId("repositories-content");
  if (state.reposError) {
    root.replaceChildren(errorBlock(`Could not load repositories. ${state.reposError}`, "Reload the page to try again."));
    return;
  }
  if (!state.reposLoaded) {
    root.replaceChildren(node("p", "Loading repositories…", "page-status"));
    return;
  }
  if (!state.repos.length) {
    root.replaceChildren(node("p", "No repositories are indexed.", "page-status"));
    return;
  }
  const parts = [];
  const { workspace } = state.status;
  const source = workspace.kind === "ok" ? field(workspace.data, "catalog", "source")
    : state.repos.every((repo) => repo.demo) ? "demo_fixtures" : "local_checkouts";
  parts.push(node("p", source === "demo_fixtures" ? "Bundled example repositories for the sample workspace."
    : source === "local_checkouts" ? "Local Git checkouts, indexed from their committed HEAD when OpenDevEngine started."
    : "Repository source unknown.", "page-note"));
  const github = workspace.kind === "ok" ? field(workspace.data, "catalog", "github") : null;
  if (github) {
    const line = node("p", undefined, "page-note");
    line.append(chip(statusOf(github.status, ["not_available"])), document.createTextNode(` GitHub: ${typeof github.reason === "string" ? github.reason : "status unknown."}`));
    parts.push(line);
  }

  const table = node("table", undefined, "repo-table");
  table.append(node("caption", `${plural(state.repos.length, "indexed repository", "indexed repositories")}`, "sr-only"));
  const head = node("thead");
  const headRow = node("tr");
  for (const label of ["Repository", "Revision", "Files", "Index"]) {
    const cell = node("th", label);
    cell.setAttribute("scope", "col");
    headRow.append(cell);
  }
  head.append(headRow);
  const body = node("tbody");
  for (const repo of state.repos) {
    const row = node("tr");
    const name = node("th", repo.name);
    name.setAttribute("scope", "row");
    const revision = node("td");
    const revisionText = node(repo.demo ? "span" : "code", repo.demo ? "Sample" : String(repo.revision).slice(0, 7));
    revisionText.title = repo.demo ? "Bundled sample revision" : String(repo.revision);
    revision.append(revisionText);
    const files = node("td", String(repo.files));
    const index = node("td");
    index.append(repo.limited
      ? chip(["attention", "!", "Index limit reached: some files were not indexed"])
      : chip(["good", "✓", "Indexed"]));
    index.title = `Indexed ${formatTime(repo.indexed_at)}`;
    row.append(name, revision, files, index);
    body.append(row);
  }
  table.append(head, body);
  parts.push(table);
  root.replaceChildren(...parts);
}

const connectedUI = { githubReady: false, providersReady: false, sessionsReady: false, executionReady: false, savedAskReady: false };
const githubPanel = node("section", undefined, "connected-panel");
const providerPanel = node("section", undefined, "connected-panel");
const sessionsUI = createSessionPanel({host: byId("sessions-content"), shell, onAvailable: () => {
  if (!connectedUI.sessionsReady) { exposeSessions(); if (state.route === "settings") renderSettings(); }
}});
const executionUI = createExecutionSettings({isVisible: () => state.route === "settings", onAvailable: () => { connectedUI.executionReady = true; }});
let reposPage = null;
window.addEventListener("hashchange", () => { if (state.route === "repositories" && reposPage) reposPage.refresh(); });
function connectedSettings() { return connectedUI.githubReady || connectedUI.providersReady; }
let settingsPage = null;
const modelsHost = node("div", undefined, "models-host");
const modelsSimple = node("div", undefined, "models-simple");
const modelsAdvanced = node("details", undefined, "existing-app");
modelsAdvanced.append(node("summary", "Advanced: all provider profiles"));
modelsHost.append(modelsSimple, modelsAdvanced);
// The job pickers and the profile list show the same profiles; a change in either refreshes both.
function mountSimpleModels() { mountModels(modelsSimple, { request: featureRequest, onChange: loadProviders }); }
// AI providers holds the old Models and Coding agent screens under Advanced, unchanged.
const providersUI = createProvidersSection({ request: featureRequest, isVisible: () => state.route === "settings",
  advanced: () => ({ models: modelsHost, agent: connectedUI.executionReady ? executionUI.element : null }) });
function settingsSection() {
  const match = /^#\/settings\/([a-z]+)/.exec(location.hash);
  const section = match ? match[1] : "github";
  return section === "models" || section === "agent" ? "providers" : section;
}
function renderSettings() {
  const focused = document.activeElement;
  const restore = inside(githubPanel, focused) || inside(providerPanel, focused) || inside(executionUI.element, focused);
  renderLegacySettings();
  if (!connectedSettings()) {
    byId("settings-content").append(githubPanel, providerPanel);
    if (connectedUI.executionReady) byId("settings-content").append(executionUI.element);
  } else {
    settingsPage ||= createSettingsPage({ host: byId("settings-content"), request: featureRequest,
      sections: { github: githubPanel, providers: providersUI.element, health: healthHost },
      onPrivacyChange: () => { executionUI.refresh(); providersUI.refresh(); } });
    if (providerPanel.parentNode !== modelsAdvanced) modelsAdvanced.append(providerPanel);
    settingsPage.mount();
    const code = (/[?&]github=([a-z_]{1,64})/.exec(location.hash) || [])[1] || "";
    settingsPage.show(settingsSection(), { code });
    if (settingsSection() === "providers") { providersUI.refresh(); mountSimpleModels(); }
  }
  if (restore && state.route === "settings" && focused?.isConnected) focused.focus();
}
function renderRepositories() {
  if (!connectedUI.githubReady) { renderLegacyRepositories(); return; }
  reposPage ||= createRepositoriesPage({ host: byId("repositories-content"), request: featureRequest, panel: sidePanel, shell });
  if (reposPage.element.parentNode !== byId("repositories-content")) byId("repositories-content").replaceChildren(reposPage.element);
  reposPage.refresh();
}

showRoute(false);
sessionsUI.open();
refreshStatus();

try {
  const settings = await api("/api/repos");
  state.repos = settings.repos;
  state.model = settings.model;
  state.reposLoaded = true;
  const demo = state.repos.every((repo) => repo.demo);
  state.current = newConversation(state.repos.map(({ name }) => name));
  byId("mode-label").textContent = state.model ? "Ollama" : "Search only";
  byId("mode").classList.toggle("model-ready", Boolean(state.model));
  byId("mode").title = state.model ? `Ollama model · ${state.model}` : "Ollama is optional";
  byId("examples").hidden = !demo;
  byId("workspace-label").textContent = demo ? "Sample workspace" : "Local workspace";
  byId("intro-text").textContent = demo
    ? "Explore a sample checkout flow across a web app, API, database, and worker."
    : "Select repositories, ask a question, and inspect the matching source lines.";
  renderThread();
  syncControls();
} catch (error) {
  state.reposError = error.message;
  thread.setAttribute("aria-busy", "false");
  byId("mode-label").textContent = "Unavailable";
  byId("intro-text").textContent = error.message;
  byId("composer-status").textContent = "Could not load repositories";
}
renderWorkspaceMode();
if (state.route === "repositories") renderRepositories();

// Connected settings use only the committed M1 HTTP boundary. Mutations are
// never retried automatically, including when the network outcome is unknown.
async function featureRequest(path, method = "GET", payload) {
  let response;
  try {
    response = await fetch(path, { method, cache: "no-store", headers: { "Content-Type": "application/json" },
      ...(payload === undefined ? {} : { body: JSON.stringify(payload) }) });
  } catch {
    throw new Error(method === "GET" ? "Server unreachable. Refresh to try again."
      : "Connection lost; the outcome is unknown. Refresh and inspect saved state before submitting again.");
  }
  let result;
  try { result = await response.json(); } catch { throw new Error("Unsupported server response. Refresh and inspect saved state."); }
  if (!response.ok) {
    // Never render arbitrary upstream messages or reflected secret material.
    const code = /^[a-z_]{1,64}$/.test(result?.code) ? result.code : `http_${response.status}`;
    const error = new Error(`${code}. ${response.status === 404 ? "Endpoint unavailable in this build." : "Check configuration and refresh saved state before retrying."}`); error.status = response.status; error.code = code; throw error;
  }
  return result;
}
function action(text, handler) {
  const button = node("button", text, "button"); button.type = "button";
  button.addEventListener("click", handler); return button;
}
function control(parent, label, value = "", type = "text") {
  const wrap = node("label", label, "setting-field");
  const input = node(type === "pem" ? "textarea" : "input");
  // textarea.type is read-only in browsers; only input elements accept a type.
  if (type !== "pem") input.type = type;
  input.value = String(value);
  if (type === "password" || type === "pem") { input.autocomplete = "new-password"; input.setAttribute("autocomplete", "new-password"); }
  wrap.append(input); parent.append(wrap); return input;
}
function toggle(parent, label, checked) {
  const input = control(parent, label, "", "checkbox"); input.checked = Boolean(checked); return input;
}
function choose(parent, label, entries, selected) {
  const wrap = node("label", label, "setting-field"); const select = node("select");
  for (const [value, text] of entries) { const option = node("option", text); option.value = value; select.append(option); }
  select.value = selected; wrap.append(select); parent.append(wrap); return select;
}
function submitButton(form, label) { const button = node("button", label, "button"); button.type = "submit"; form.append(button); return button; }
function notice(parent) { const out = node("p", "", "page-status"); out.setAttribute("role", "status"); parent.append(out); return out; }
async function perform(button, output, operation) {
  if (button.disabled) return;
  const focused = document.activeElement;
  const restore = focused === button || inside(button.closest("form"), focused);
  const panel = button.closest(".connected-panel");
  button.disabled = true; output.textContent = "Working…";
  try { await operation(); } catch (error) { output.textContent = error.message; }
  finally {
    button.disabled = false;
    if (restore && (document.activeElement === document.body || !document.activeElement?.isConnected)) {
      if (button.isConnected) button.focus();
      else if (panel) restorePanelFocus(panel, true);
    }
  }
}
function timeLabel(value) { return value ? new Date(value).toLocaleString() : "Never"; }

let githubLoadGeneration = 0;
let githubMutationPending = false;
function inside(root, element) {
  for (let current = element; current; current = current.parentNode) if (current === root) return true;
  return false;
}
function restorePanelFocus(panel, restore) {
  const visible = state.route === "settings";
  if (restore && visible &&
      (document.activeElement === document.body || !document.activeElement?.isConnected)) {
    const heading = panel.querySelector("h2"); heading?.setAttribute("tabindex", "-1"); heading?.focus();
  }
}
async function githubMutation(path, payload) {
  if (githubMutationPending) throw new Error("A GitHub update is pending. Wait for it to finish.");
  ++githubLoadGeneration; githubMutationPending = true;
  try { return await featureRequest(path, "POST", payload); }
  finally { githubMutationPending = false; }
}
async function loadGitHub() {
  if (githubMutationPending) {
    byId("app-status").textContent = "A GitHub update is pending. Reload status after it finishes.";
    return;
  }
  const generation = ++githubLoadGeneration;
  const restore = inside(githubPanel, document.activeElement);
  githubPanel.replaceChildren(node("h2", "GitHub connection"));
  if (/^#\/settings\?github=[a-z_]{1,64}$/.test(location.hash)) {
    githubPanel.append(node("p", "GitHub setup returned. Check the connection status below; if setup failed or expired, create a new connect link and try again.", "page-status"));
  }
  const output = notice(githubPanel); output.textContent = "Loading connections…";
  const retry = action("Reload GitHub status", loadGitHub); githubPanel.append(retry);
  try {
    const data = await featureRequest("/api/github/connections");
    if (generation !== githubLoadGeneration) return;
    if (!Array.isArray(data.connections) || !data.summary) throw new Error("Unsupported GitHub response.");
    connectedUI.githubReady = true;
    output.textContent = `${data.summary.status} · ${data.summary.installations} active installations · ${data.summary.repository_count} accessible repositories`;
    githubPanel.append(node("p", "Create a GitHub App in GitHub Developer settings. Use this server’s /api/github/callback as its setup URL; keep this server on loopback. Grant metadata/read contents for discovery and indexing; write contents/pull requests are separate optional permissions. Install only on repositories you intend to share. CLI login does not configure this App. Secure persistent storage must be available."));
    githubPanel.append(node("p", "Disconnect removes access, but does not delete retained data. New repository indexing and implementation access are independent; implementation is always explicit. Configure an execution profile separately before starting implementation. Publication authorization remains separate."));
    for (const connection of data.connections) {
      const box = node("section", undefined, "setting-box");
      box.append(node("h3", `${connection.slug} · ${connection.status}`), node("p", `App ${connection.app_id} · Last refresh ${timeLabel(connection.last_refresh_at)} · Next retry ${timeLabel(connection.next_retry_at)}${connection.last_error_code ? ` · ${connection.last_error_code}` : ""}`));
      const status = notice(box);
      for (const [label, endpoint] of [["Create connect link", "connect"], ["Refresh catalog", "refresh"], ["Disconnect", "disconnect"]]) {
        const button = action(label, () => perform(button, status, async () => {
          const result = await githubMutation(`/api/github/${endpoint}`, { connection_id: connection.id });
          if (endpoint === "connect") {
            let url;
            try { url = new URL(result.install_url); } catch { throw new Error("Invalid installation destination."); }
            if (url.origin !== "https://github.com" || url.username || url.password || !url.pathname.startsWith("/apps/")) throw new Error("Invalid installation destination.");
            const link = node("a", "Continue installation on GitHub"); link.href = url.href; link.rel = "noreferrer";
            status.replaceChildren(link, node("span", ` · Expires ${timeLabel(result.expires_at)}`));
          } else {
            status.textContent = endpoint === "refresh" ? `Discovery ${result.state}. Reload status to check progress.` : "Disconnected. Reload status to inspect access.";
            if (reposPage) await reposPage.refresh();
          }
        })); box.append(button);
      }
      githubPanel.append(box);
    }
    const form = node("form", undefined, "settings-form"); form.autocomplete = "off";
    form.append(node("h3", "Configure or rotate GitHub App credentials"));
    const appID = control(form, "App ID", "", "number"); appID.required = true; appID.min = "1";
    const slug = control(form, "App slug"); slug.required = true;
    const key = control(form, "RSA private key PEM (never redisplayed)", "", "pem"); key.required = true;
    const indexNew = toggle(form, "Index newly discovered repositories", false);
    const save = submitButton(form, "Save App configuration"); const status = notice(form);
    form.addEventListener("submit", event => {
      event.preventDefault(); if (save.disabled) return;
      const payload = { app_id: appID.value.trim(), slug: slug.value.trim(), private_key: key.value, index_new: indexNew.checked };
      key.value = "";
      perform(save, status, async () => { try { await githubMutation("/api/github/configure", payload); } finally { payload.private_key = ""; } await loadGitHub(); });
    });
    githubPanel.append(form);
  } catch (error) { if (generation === githubLoadGeneration) output.textContent = `GitHub unavailable: ${error.message}`; }
  finally { if (generation === githubLoadGeneration) restorePanelFocus(githubPanel, restore); }
}


let providerProfiles = [];
let providerSettings;
let providerEditor;
let providerLoadGeneration = 0;
const providerSummaryText = () => connectedUI.savedAskReady ? "Every saved model connection. Choose which one does each job above. Tests send synthetic content only; costs show only when pricing is set." : "Profiles configure inference, not an implementation runner. Legacy Ask keeps its startup model. Tests send synthetic content only; costs show only when pricing is set.";
const ADAPTER_NAMES = { ollama: "Ollama", openai_compatible: "OpenAI compatible", claude_cli: "Claude Code CLI" };
const CAPABILITY_NAMES = { chat: "Chat", structured_output: "Structured output", embeddings: "Embeddings" };
const hostOf = (endpoint) => { try { return new URL(endpoint).host; } catch { return String(endpoint); } };
// Short visible text; the accessible name still says which profile the button acts on.
function labelledAction(text, label, handler) { const button = action(text, handler); button.setAttribute("aria-label", label); return button; }
let providerMutationPending = false;
async function providerMutation(path, method, payload) {
  if (providerMutationPending) throw new Error("A provider update is pending. Wait for it to finish before submitting another change.");
  // A read started before this write can no longer describe current settings.
  ++providerLoadGeneration;
  providerMutationPending = true;
  try { return await featureRequest(path, method, payload); }
  finally { providerMutationPending = false; }
}
async function loadProviders() {
  // Keep the active form and its status while a write outcome is pending. The
  // successful mutation reloads after completion; errors remain on that form.
  if (providerMutationPending) {
    byId("app-status").textContent = "A provider update is pending. Reload settings after it finishes.";
    return;
  }
  const restore = inside(providerPanel, document.activeElement);
  const generation = ++providerLoadGeneration;
  const header = node("div", undefined, "panel-header");
  header.append(node("h2", "Provider profiles"), action("Reload provider settings", loadProviders));
  providerPanel.replaceChildren(header);
  const output = notice(providerPanel); output.textContent = "Loading provider settings…";
  try {
    const [profiles, settings] = await Promise.all([featureRequest("/api/provider-profiles"), featureRequest("/api/settings/model-roles")]);
    if (generation !== providerLoadGeneration) return;
    if (!Array.isArray(profiles.profiles) || typeof settings.settings?.local_only !== "boolean" || !settings.roles) throw new Error("Unsupported provider response.");
    providerProfiles = profiles.profiles; providerSettings = settings;
    connectedUI.providersReady = true;
    output.textContent = providerSummaryText();
    const list = node("div", undefined, "profile-list");
    if (!providerProfiles.length) list.append(node("p", "No provider profiles yet. Pick a model above, or create one below.", "detail-muted"));
    for (const profile of providerProfiles) {
      const box = node("article", undefined, "profile-row"); const version = profile.version;
      const info = node("div", undefined, "profile-info");
      const title = node("div", undefined, "profile-title");
      title.append(node("strong", profile.name), node("span", `Version ${version.version}`, "profile-version"));
      const capabilities = version.config.capabilities.map(cap => CAPABILITY_NAMES[cap] ?? cap).join(", ");
      info.append(title,
        node("p", `${ADAPTER_NAMES[version.adapter] ?? version.adapter} · ${version.model} · ${hostOf(version.endpoint)}`, "profile-meta"),
        node("p", `${capabilities} · ${version.credential_configured ? "Credential saved" : "No credential"} · ${version.config.allow_external_source ? "May receive repository source (workspace policy still applies)" : "Never receives repository source"}`, "profile-meta"));
      const actions = node("div", undefined, "profile-actions");
      const status = node("p", "", "page-status profile-status"); status.setAttribute("role", "status");
      actions.append(labelledAction("Edit", `Edit ${profile.name}`, () => { renderProviderEditor(profile); providerEditor.querySelector("input")?.focus(); }));
      const test = labelledAction("Test", `Test ${profile.name}`, () => perform(test, status, async () => {
        const result = await providerMutation(`/api/provider-profiles/${encodeURIComponent(profile.id)}/test`, "POST", { capabilities: version.config.capabilities });
        status.textContent = `Test ${result.status}: ${Object.entries(result.capabilities || {}).map(([cap, value]) => `${CAPABILITY_NAMES[cap] ?? cap} ${value.status}${value.error_code ? ` (${value.error_code})` : ""}`).join(" · ")}. Sent to ${result.destination?.host || "an unknown destination"} (${result.destination?.kind || "unknown"}). Synthetic content only; cost unknown.`;
      })); actions.append(test);
      const remove = labelledAction("Delete", `Delete ${profile.name}`, () => {
        const confirm = labelledAction("Delete", `Confirm delete ${profile.name}`, () => perform(confirm, status, async () => {
          await providerMutation(`/api/provider-profiles/${encodeURIComponent(profile.id)}`, "DELETE", {}); await loadProviders();
        }));
        confirm.classList.add("btn-danger");
        status.replaceChildren(node("span", `Delete ${profile.name}? Its credential is revoked and any job using it is cleared. Past history stays. `), confirm, labelledAction("Cancel", "Cancel deletion", () => { status.textContent = "Deletion cancelled."; }));
      }); actions.append(remove);
      box.append(info, actions, status); list.append(box);
    }
    providerPanel.append(list);
    if (modelsSimple.isConnected) mountSimpleModels();
    providerEditor = node("section", undefined, "setting-box"); providerPanel.append(providerEditor); renderProviderEditor();
  } catch (error) {
    if (generation === providerLoadGeneration) output.textContent = `Provider settings unavailable: ${error.message}`;
  } finally { if (generation === providerLoadGeneration) restorePanelFocus(providerPanel, restore); }
}
function renderProviderEditor(profile = null) {
  const version = profile?.version; const config = version?.config || {};
  providerEditor.replaceChildren(node("h3", profile ? `Edit ${profile.name}` : "New provider profile"));
  const form = node("form", undefined, "settings-form profile-form"); form.setAttribute("autocomplete", "off");
  const name = control(form, "Profile name", profile?.name || ""); name.required = true; name.maxLength = 100;
  const adapter = choose(form, "Adapter", [["ollama", "Ollama"], ["openai_compatible", "OpenAI compatible"], ["claude_cli", "Claude Code CLI (your login)"]], version?.adapter || "ollama");
  const endpoint = control(form, "Base URL / destination", version?.endpoint || "http://127.0.0.1:11434", "url"); endpoint.required = true;
  endpoint.parentNode.classList.add("field-wide");
  // The hint follows the adapter so people only read the rule that applies to them.
  const hint = node("p", "", "field-hint"); endpoint.parentNode.append(hint);
  const showHint = () => { hint.textContent = adapter.value === "claude_cli" ? "Claude Code CLI uses https://api.anthropic.com, a model alias such as sonnet, and no credential."
    : `${adapter.value === "openai_compatible" ? "Use the /v1 base. " : ""}HTTP only to this computer or the --ollama-url machine; HTTPS anywhere else. Requests go to this destination; redirects are rejected.`; };
  adapter.addEventListener("change", showHint); showHint();
  const model = control(form, "Model identifier", version?.model || ""); model.required = true;
  const secret = control(form, profile ? "New credential (leave blank to retain current credential)" : "Credential (optional for local Ollama)", "", "password");
  const capsGroup = node("fieldset", undefined, "field-group field-wide"); capsGroup.append(node("legend", "Capabilities"));
  const caps = {};
  for (const cap of ["chat", "structured_output", "embeddings"]) caps[cap] = toggle(capsGroup, CAPABILITY_NAMES[cap], config.capabilities ? config.capabilities.includes(cap) : cap === "chat");
  form.append(capsGroup);
  const dimensions = control(form, "Embedding dimensions (required for embeddings)", config.embedding_dimensions || "", "number"); dimensions.min = "1"; dimensions.max = "16384";
  // Disabled as well as hidden: a leftover invalid value must not block saving a chat-only profile.
  const showDimensions = () => { dimensions.parentNode.hidden = dimensions.disabled = !caps.embeddings.checked; };
  caps.embeddings.addEventListener("change", showDimensions); showDimensions();
  const limits = node("fieldset", undefined, "field-group field-wide field-columns"); limits.append(node("legend", "Limits"));
  const timeout = control(limits, "Timeout seconds", config.timeout_seconds ?? 60, "number"); timeout.min = "1"; timeout.max = "600"; timeout.required = true;
  const context = control(limits, "Context token limit", config.context_limit ?? 8192, "number"); context.min = "256"; context.max = "2000000"; context.required = true;
  const output = control(limits, "Maximum output tokens", config.max_output_tokens ?? 1024, "number"); output.min = "1"; output.max = "131072"; output.required = true;
  form.append(limits);
  const pricing = node("details", undefined, "field-wide field-details");
  pricing.append(node("summary", "Pricing (optional, shows cost estimates)"));
  const prices = node("div", undefined, "field-columns");
  const inputPrice = control(prices, "Input price USD per million tokens (blank = unknown)", config.input_price_per_million ?? "", "number");
  const outputPrice = control(prices, "Output price USD per million tokens (blank = unknown)", config.output_price_per_million ?? "", "number");
  for (const price of [inputPrice, outputPrice]) { price.min = "0"; price.max = "1000000"; price.step = "any"; }
  pricing.append(prices); if (inputPrice.value !== "" || outputPrice.value !== "") pricing.open = true;
  form.append(pricing);
  const sharing = node("fieldset", undefined, "field-group field-wide"); sharing.append(node("legend", "Repository source"));
  const external = toggle(sharing, "Allow sending repository source to this external destination", config.allow_external_source);
  sharing.append(node("p", "Off by default. Local-only mode in Privacy still blocks remote destinations.", "field-hint"));
  form.append(sharing);
  const formActions = node("div", undefined, "form-actions field-wide");
  const save = submitButton(formActions, profile ? "Save new profile version" : "Create profile"); save.classList.add("btn-primary");
  if (profile) formActions.append(action("Cancel edit / new profile", () => renderProviderEditor()));
  form.append(formActions);
  const status = notice(form);
  form.addEventListener("submit", event => {
    event.preventDefault(); if (save.disabled) return;
    const capabilities = Object.keys(caps).filter(cap => caps[cap].checked);
    const secretValue = secret.value; secret.value = "";
    if (!capabilities.length || (caps.structured_output.checked && !caps.chat.checked)) { status.textContent = "Choose at least one capability. structured_output requires chat. Credential cleared; re-enter it when ready."; return; }
    const config = { capabilities, timeout_seconds: Number(timeout.value), context_limit: Number(context.value), max_output_tokens: Number(output.value), allow_external_source: external.checked };
    if (caps.embeddings.checked) config.embedding_dimensions = Number(dimensions.value);
    if (inputPrice.value !== "") config.input_price_per_million = Number(inputPrice.value);
    if (outputPrice.value !== "") config.output_price_per_million = Number(outputPrice.value);
    const payload = { name: name.value.trim(), adapter: adapter.value, endpoint: endpoint.value.trim(), model: model.value.trim(), config };
    if (secretValue) payload.secret_value = secretValue;
    perform(save, status, async () => {
      try { await providerMutation(profile ? `/api/provider-profiles/${encodeURIComponent(profile.id)}` : "/api/provider-profiles", profile ? "PATCH" : "POST", payload); }
      finally { delete payload.secret_value; }
      await loadProviders();
    });
  }); providerEditor.append(form);
}

await Promise.all([loadGitHub(), loadProviders()]);
if (state.route === "repositories") renderRepositories();
// Execution discovery never holds the saved Ask workspace behind an unrelated read.
executionUI.refresh().then(() => { if (state.route === "settings") renderSettings(); });
// Successful list discovery makes Sessions reachable even when the legacy status
// projection has not yet learned the modern route.
featureRequest("/api/sessions?limit=1").then(available => { if (Array.isArray(available.items)) { exposeSessions(); if (state.route === "settings") renderSettings(); } }).catch(error => {
  // A definitively missing API preserves the legacy navigation contract.
  if (error.status === 404) {
    shell.setNavAvailability("sessions", "unavailable");
    if (location.hash === "#/sessions") { history.replaceState(null, "", "#/ask"); sessionsUI.close(); showRoute(false); announce("That page is not available in this version. Showing Ask."); }
  }
});
if (state.route === "settings") renderSettings();
if (state.route === "repositories") renderRepositories();

persistentAsk = await createAskPanel({host: byId("view-ask"), shell, sidePanel, onMount: handle => {
  // Retire legacy content before the first saved-page/history read can wait or fail.
  persistentAsk = handle;
  connectedUI.savedAskReady = true;
  if (connectedUI.providersReady) providerPanel.querySelector('[role="status"]').textContent = providerSummaryText();
  byId("mode-label").textContent = "Ask & plans";
  byId("mode").title = "Model providers and destinations are configured in Settings";
  byId("mode").classList.remove("model-ready");
  for (const child of [...byId("view-ask").children]) if (child !== handle.panel) child.hidden = true;
  byId("show-repos").hidden = true;
  byId("history-toggle").hidden = true;
}});

if (connectedUI.savedAskReady && state.route === "settings") renderSettings();
