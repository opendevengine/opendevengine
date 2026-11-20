// Models section: discovered Ollama models, one dropdown per job, CPU warning, short provider form.
import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createBrowser, input, install, settle, visibleText } from "./dom.mjs";

const url = pathToFileURL(fileURLToPath(new URL("../../opendevengine/web/settings-models.js", import.meta.url))).href;
let loads = 0;
const DISCOVERED = { reachable: true, error_code: null, models: [
  { name: "llama3.2:latest", embedding: false, gpu: false, loaded: true, context_length: 8192 },
  { name: "qwen2.5:14b", embedding: false, gpu: null, loaded: false, context_length: null },
  { name: "nomic-embed-text:latest", embedding: true, gpu: null, loaded: false, context_length: null }] };
const PROFILE = { id: "prof_1", name: "CLI local Ollama", version: { adapter: "ollama", endpoint: "http://127.0.0.1:11434", model: "llama3.2:latest", config: { capabilities: ["chat", "structured_output"] } } };

async function setup({ discover = DISCOVERED, failRole = false, tests = [] } = {}) {
  const b = createBrowser("<!doctype html><html><body><main id=host></main></body></html>", { respond: () => ({ status: 404, body: {} }) });
  install(b);
  loads += 1;
  const mod = await import(`${url}?m=${loads}`);
  const calls = [];
  let profiles = [PROFILE];
  const roles = { ask: { status: "untested", profile_id: "prof_1", model: "llama3.2:latest" }, planning: { status: "unassigned", profile_id: null }, summary: { status: "unassigned", profile_id: null }, embedding: { status: "unassigned", profile_id: null } };
  const request = async (path, method = "GET", body) => {
    calls.push([method, path, body]);
    if (path.startsWith("/api/provider-profiles/discover/dimensions")) return { dimensions: 768 };
    if (path.startsWith("/api/provider-profiles/discover")) return discover;
    if (path === "/api/provider-profiles" && method === "GET") return { profiles };
    if (path === "/api/provider-profiles" && method === "POST") { const p = { id: `prof_${profiles.length + 1}`, name: body.name, version: { adapter: body.adapter, endpoint: body.endpoint, model: body.model, config: body.config } }; profiles = [...profiles, p]; return p; }
    if (path.endsWith("/test") && method === "POST") return tests.shift() ?? { status: "passed", error_code: null };
    if (path.startsWith("/api/provider-profiles/") && method === "PATCH") { const p = profiles.find((x) => path.endsWith(x.id)); p.name = body.name; p.version = { ...p.version, model: body.model, config: body.config }; return p; }
    if (path === "/api/settings/model-roles" && method === "GET") return { roles, settings: { local_only: true } };
    if (path === "/api/settings/model-roles" && method === "PATCH") { if (failRole) throw Object.assign(new Error("role_unavailable. Check configuration."), { status: 409 }); return { roles }; }
    return {};
  };
  const host = b.document.getElementById("host");
  await mod.mountModels(host, { request });
  await settle();
  return { b, mod, host, calls };
}

const select = (host, label) => host.querySelectorAll("select").find((s) => s.getAttribute("aria-label") === label);
const optionTexts = (sel) => sel.querySelectorAll("option").map((o) => o.textContent);
const change = (sel, value) => { sel.value = value; sel.dispatchEvent(new (sel.ownerDocument.defaultView.Event ?? class { constructor(t) { this.type = t; this.bubbles = true; } })("change")); };

test("Ollama card shows discovered models and each job has one dropdown", async () => {
  const { host } = await setup();
  assert.match(visibleText(host), /Ollama.*Connected · 3 models/);
  assert.match(visibleText(host), /Runs models on this computer\./);
  assert.deepEqual(host.querySelectorAll("select").map((s) => s.getAttribute("aria-label")), ["Answering questions", "Writing plans", "Smarter code search", "Repository summaries"]);
  assert.deepEqual(optionTexts(select(host, "Smarter code search")), ["Off", "nomic-embed-text:latest"]);
  assert.ok(!optionTexts(select(host, "Answering questions")).includes("nomic-embed-text:latest"));
  assert.equal(select(host, "Answering questions").value, "p:prof_1");
  assert.equal(host.querySelectorAll("button").filter((x) => /^Save/.test(x.textContent)).length, 0, "no save buttons");
});

test("choosing a discovered embedding model creates a profile with probed dimensions, then assigns the role", async () => {
  const { host, calls } = await setup();
  const sel = select(host, "Smarter code search");
  sel.value = "m:nomic-embed-text:latest";
  sel.dispatchEvent({ type: "change", bubbles: true });
  await settle(20);
  const post = calls.find(([m, p]) => m === "POST" && p === "/api/provider-profiles");
  assert.deepEqual(post[2], { name: "nomic-embed-text:latest", adapter: "ollama", endpoint: "http://127.0.0.1:11434", model: "nomic-embed-text:latest",
    config: { capabilities: ["embeddings"], embedding_dimensions: 768, timeout_seconds: 180 } });
  assert.deepEqual(calls.filter(([m]) => m === "PATCH").at(-1), ["PATCH", "/api/settings/model-roles", { assignments: { embedding: "prof_2" } }]);
});

test("a configured Ollama on another machine is named, and its models are set up at that address", async () => {
  const { host, calls } = await setup({ discover: { ...DISCOVERED, base_url: "http://100.102.234.29:11434" } });
  assert.match(visibleText(host), /Runs models on 100\.102\.234\.29:11434\./);
  const sel = select(host, "Smarter code search");
  sel.value = "m:nomic-embed-text:latest";
  sel.dispatchEvent({ type: "change", bubbles: true });
  await settle(20);
  const post = calls.find(([m, p]) => m === "POST" && p === "/api/provider-profiles");
  assert.equal(post[2].endpoint, "http://100.102.234.29:11434");
});

test("choosing a discovered chat model uses the 600s local timeout, context length and 1024 output tokens", async () => {
  const { host, calls } = await setup();
  const sel = select(host, "Writing plans");
  sel.value = "m:qwen2.5:14b";
  sel.dispatchEvent({ type: "change", bubbles: true });
  await settle(20);
  const post = calls.find(([m, p]) => m === "POST" && p === "/api/provider-profiles");
  assert.deepEqual(post[2].config, { capabilities: ["chat", "structured_output"], timeout_seconds: 600, context_limit: 8192, max_output_tokens: 1024 });
});

test("a failed role save reverts the dropdown and explains why", async () => {
  const { host } = await setup({ failRole: true });
  const sel = select(host, "Answering questions");
  sel.value = "";
  sel.dispatchEvent({ type: "change", bubbles: true });
  await settle(20);
  assert.equal(sel.value, "p:prof_1");
  assert.match(visibleText(host), /role_unavailable/);
});

test("CPU-only models get a warning", async () => {
  const { host } = await setup();
  assert.match(visibleText(host), /llama3\.2:latest runs on the CPU here; long answers can take over a minute\./);
});

test("unreachable Ollama says how to fix it", async () => {
  const { host } = await setup({ discover: { reachable: false, models: [], error_code: "provider_unreachable" } });
  assert.match(visibleText(host), /Not running\. Start Ollama, then Retry\./);
  assert.ok(host.querySelectorAll("button").find((x) => x.textContent === "Retry"));
});

test("Add OpenAI-compatible provider keeps advanced fields folded and posts the short form", async () => {
  const { b, host, calls } = await setup();
  host.querySelectorAll("button").find((x) => x.getAttribute("aria-label") === "Add OpenAI-compatible provider").click();
  await settle();
  const dialog = b.document.querySelector("dialog.modal");
  const field = (label) => dialog.querySelectorAll("input").find((i) => i.getAttribute("aria-label") === label);
  assert.ok(dialog.querySelector("details"), "advanced settings are folded");
  field("Name").value = "LM Studio";
  input(field("Base URL"), "http://127.0.0.1:1234/v1");
  field("Model").value = "qwen-coder";
  assert.ok(field("Send repository source to this provider").closest(".modal-remote").hidden, "no source opt-in for loopback");
  dialog.querySelectorAll("button").find((x) => x.textContent === "Add provider").click();
  await settle();
  const post = calls.find(([m, p]) => m === "POST" && p === "/api/provider-profiles");
  assert.deepEqual(post[2], { name: "LM Studio", adapter: "openai_compatible", endpoint: "http://127.0.0.1:1234/v1", model: "qwen-coder",
    config: { capabilities: ["chat", "structured_output"], timeout_seconds: 180, context_limit: 8192, max_output_tokens: 1024 } });
});

test("a remote provider offers the source-sharing opt-in and sends it with structured output", async () => {
  const { b, host, calls } = await setup();
  host.querySelectorAll("button").find((x) => x.getAttribute("aria-label") === "Add OpenAI-compatible provider").click();
  await settle();
  const dialog = b.document.querySelector("dialog.modal");
  const field = (label) => dialog.querySelectorAll("input").find((i) => i.getAttribute("aria-label") === label);
  field("Name").value = "Groq";
  input(field("Base URL"), "https://api.groq.com/openai/v1");
  field("Model").value = "openai/gpt-oss-120b";
  const share = field("Send repository source to this provider");
  assert.equal(share.closest(".modal-remote").hidden, false);
  assert.equal(share.checked, false, "sharing is never on by default");
  assert.match(visibleText(dialog), /Local-only mode in Settings → Privacy must also be off\./);
  share.click();
  dialog.querySelectorAll("button").find((x) => x.textContent === "Add provider").click();
  await settle();
  const post = calls.find(([m, p]) => m === "POST" && p === "/api/provider-profiles");
  assert.deepEqual(post[2].config, { capabilities: ["chat", "structured_output"], timeout_seconds: 180, context_limit: 8192,
    max_output_tokens: 1024, allow_external_source: true });
});

test("source-sharing consent is cleared when a remote destination changes", async () => {
  const { b, host, calls } = await setup();
  host.querySelectorAll("button").find((x) => x.getAttribute("aria-label") === "Add OpenAI-compatible provider").click();
  await settle();
  const dialog = b.document.querySelector("dialog.modal");
  const field = (label) => dialog.querySelectorAll("input").find((i) => i.getAttribute("aria-label") === label);
  field("Name").value = "Changed remote";
  input(field("Base URL"), "https://provider-a.example/v1");
  field("Model").value = "model";
  const share = field("Send repository source to this provider");
  share.click();
  assert.equal(share.checked, true);
  input(field("Base URL"), "https://provider-b.example/v1");
  assert.equal(share.checked, false, "consent for provider A cannot apply to provider B");
  dialog.querySelectorAll("button").find((x) => x.textContent === "Add provider").click();
  await settle();
  const post = calls.find(([m, p]) => m === "POST" && p === "/api/provider-profiles");
  assert.equal(post[2].config.allow_external_source, false);
});

test("hiding source consent clears it before a later remote destination", async () => {
  const { b, host, calls } = await setup();
  host.querySelectorAll("button").find((x) => x.getAttribute("aria-label") === "Add OpenAI-compatible provider").click();
  await settle();
  const dialog = b.document.querySelector("dialog.modal");
  const field = (label) => dialog.querySelectorAll("input").find((i) => i.getAttribute("aria-label") === label);
  field("Name").value = "Remote after local";
  input(field("Base URL"), "https://provider-a.example/v1");
  field("Model").value = "model";
  const share = field("Send repository source to this provider");
  share.click();
  input(field("Base URL"), "http://127.0.0.1:1234/v1");
  assert.equal(share.closest(".modal-remote").hidden, true);
  assert.equal(share.checked, false, "hidden consent is cleared");
  input(field("Base URL"), "https://provider-b.example/v1");
  assert.equal(share.closest(".modal-remote").hidden, false);
  assert.equal(share.checked, false, "returning to remote requires a fresh opt-in");
  dialog.querySelectorAll("button").find((x) => x.textContent === "Add provider").click();
  await settle();
  const post = calls.find(([m, p]) => m === "POST" && p === "/api/provider-profiles");
  assert.equal(post[2].config.allow_external_source, false);
});

test("unchecking structured output keeps a chat-only profile", async () => {
  const { b, host, calls } = await setup();
  host.querySelectorAll("button").find((x) => x.getAttribute("aria-label") === "Add OpenAI-compatible provider").click();
  await settle();
  const dialog = b.document.querySelector("dialog.modal");
  const field = (label) => dialog.querySelectorAll("input").find((i) => i.getAttribute("aria-label") === label);
  field("Name").value = "Old server";
  input(field("Base URL"), "https://llm.example.com/v1");
  field("Model").value = "m";
  field("Supports structured output (JSON schema)").click();
  dialog.querySelectorAll("button").find((x) => x.textContent === "Add provider").click();
  await settle();
  const post = calls.find(([m, p]) => m === "POST" && p === "/api/provider-profiles");
  assert.deepEqual(post[2].config.capabilities, ["chat"]);
  assert.equal(post[2].config.allow_external_source, false);
});

test("Claude subscription setup creates a Sonnet claude_cli profile, tests it, and never sends a credential", async () => {
  const { b, host, calls } = await setup();
  assert.match(visibleText(host), /Claude subscription/);
  host.querySelectorAll("button").find((x) => x.textContent === "Set up").click();
  await settle();
  const dialog = b.document.querySelector("dialog.modal");
  const model = dialog.querySelectorAll("select").find((s) => s.getAttribute("aria-label") === "Model");
  assert.equal(model.value, "sonnet", "Sonnet is the default");
  const share = dialog.querySelectorAll("input").find((i) => i.getAttribute("aria-label") === "Send repository source to Anthropic");
  assert.equal(share.checked, false, "sharing is never on by default");
  share.click();
  dialog.querySelectorAll("button").find((x) => x.textContent === "Set up and test").click();
  await settle(20);
  const post = calls.find(([m, p]) => m === "POST" && p === "/api/provider-profiles");
  assert.deepEqual(post[2], { name: "Claude subscription · Sonnet", model: "sonnet", adapter: "claude_cli", endpoint: "https://api.anthropic.com",
    config: { capabilities: ["chat", "structured_output"], timeout_seconds: 300, context_limit: 32768, max_output_tokens: 8192, allow_external_source: true } });
  assert.ok(!("secret_value" in post[2]));
  assert.ok(calls.some(([m, p]) => m === "POST" && p === "/api/provider-profiles/prof_2/test"));
  assert.match(visibleText(host), /Set up · sonnet/);
  assert.ok(select(host, "Writing plans").querySelectorAll("option").some((o) => o.value === "p:prof_2"), "the profile can be chosen for plans");
});

test("a failed Claude test explains the fix and retesting updates the same profile", async () => {
  const { b, host, calls } = await setup({ tests: [{ status: "failed", error_code: "provider_auth" }] });
  host.querySelectorAll("button").find((x) => x.textContent === "Set up").click();
  await settle();
  const dialog = b.document.querySelector("dialog.modal");
  const go = () => dialog.querySelectorAll("button").find((x) => x.textContent === "Set up and test").click();
  go();
  await settle(20);
  assert.match(visibleText(dialog), /isn't logged in with a personal Pro or Max account/);
  go();
  await settle(20);
  assert.equal(calls.filter(([m, p]) => m === "POST" && p === "/api/provider-profiles").length, 1);
  assert.ok(calls.some(([m, p]) => m === "PATCH" && p === "/api/provider-profiles/prof_2"));
});

test("every model source is a row in one providers card, and every job picker has the same width", async () => {
  const { host } = await setup();
  const cards = host.querySelectorAll(".setting-card");
  assert.equal(cards.length, 2, "providers card and jobs card, nothing floating outside");
  assert.deepEqual(cards[0].querySelectorAll(".source-name strong").map((n) => n.textContent), ["Ollama", "Claude subscription", "OpenAI-compatible server"]);
  assert.ok(cards[0].querySelector(".status-pill.is-good"), "Ollama status is a pill, not inline text");
  const css = readFileSync(new URL("../../opendevengine/web/styles.css", import.meta.url), "utf8");
  assert.match(css, /\.job-row select \{ flex: none; width: min\(320px, 55%\); \}/);
});

test("an older Models load that finishes last does not replace the newer one", async () => {
  const b = createBrowser("<!doctype html><html><body><main id=host></main></body></html>", { respond: () => ({ status: 404, body: {} }) });
  install(b);
  const mod = await import(`${url}?m=race`);
  let release;
  const slow = new Promise((done) => { release = done; });
  let discoveries = 0;
  const profiles = [[], [PROFILE]];
  let reads = 0;
  const request = async (path, method = "GET") => {
    if (path.startsWith("/api/provider-profiles/discover")) return ++discoveries === 1 ? slow : DISCOVERED;
    if (path === "/api/provider-profiles" && method === "GET") return { profiles: profiles[Math.min(reads++, 1)] };
    if (path === "/api/settings/model-roles") return { roles: { ask: { profile_id: reads > 1 ? "prof_1" : null } }, settings: { local_only: true } };
    return {};
  };
  const host = b.document.getElementById("host");
  const stale = mod.mountModels(host, { request });
  await mod.mountModels(host, { request });
  await settle();
  assert.equal(select(host, "Answering questions").value, "p:prof_1");
  release(DISCOVERED);
  await stale;
  await settle();
  assert.equal(select(host, "Answering questions").value, "p:prof_1", "the stale load rendered nothing");
});
