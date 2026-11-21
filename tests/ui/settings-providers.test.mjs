// Settings → AI providers: add Claude, Codex or Local; choose a provider per job.
import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createBrowser, input, install, settle, visibleText } from "./dom.mjs";

const url = pathToFileURL(fileURLToPath(new URL("../../opendevengine/web/settings-providers.js", import.meta.url))).href;
let loads = 0;
const LOCAL = { id: "aip_local", kind: "local", name: "Local", endpoint: "http://100.102.234.29:11434", adapter: "ollama", auth: "none", has_key: false };
const CLAUDE = { id: "aip_claude", kind: "claude", name: "Claude", endpoint: null, adapter: null, auth: "api_key", has_key: true };
const CODEX = { id: "aip_codex", kind: "codex", name: "Codex", endpoint: "https://api.openai.com/v1", adapter: null, auth: "api_key", has_key: true };
const UNSET = { provider_id: null, model: null, status: "unassigned" };

async function setup({ providers = [LOCAL, CLAUDE], jobs = {}, jobSequence = [], advanced } = {}) {
  const b = createBrowser("<!doctype html><html><body><main id=host></main></body></html>", { respond: () => ({ status: 404, body: {} }) });
  install(b);
  loads += 1;
  const mod = await import(`${url}?p=${loads}`);
  const calls = [];
  let current = { plans: UNSET, coding: UNSET, search: UNSET, summary: UNSET, ...jobs };
  const request = async (path, method = "GET", body) => {
    calls.push([method, path, body]);
    if (path === "/api/ai-providers" && method === "GET") return { providers };
    if (path === "/api/ai-providers" && method === "POST") return { id: "aip_new", ...body };
    if (path === "/api/ai-jobs" && method === "GET") { if (jobSequence.length) current = { ...current, ...jobSequence.shift() }; return current; }
    if (path.startsWith("/api/ai-jobs/") && method === "PATCH") return current;
    if (path.startsWith("/api/provider-profiles/discover")) return { reachable: true, models: [{ name: "qwen3:8b", embedding: false }, { name: "nomic-embed-text", embedding: true }] };
    return current;
  };
  const host = b.document.getElementById("host");
  const section = mod.createProvidersSection({ request, advanced, pollMs: 1 });
  host.append(section.element);
  await section.refresh();
  await settle();
  return { b, host, calls, section };
}

const select = (host, label) => host.querySelectorAll("select").find((s) => s.getAttribute("aria-label") === label);
const field = (host, label) => host.querySelectorAll("input").find((s) => s.getAttribute("aria-label") === label);
const buttonNamed = (host, text) => host.querySelectorAll("button").find((x) => x.textContent === text);
const change = (el, value) => { el.value = value; el.dispatchEvent(new (el.ownerDocument.defaultView.Event ?? class { constructor(t) { this.type = t; this.bubbles = true; } })("change")); };
const posts = (calls) => calls.filter(([m, p]) => m === "POST" && p === "/api/ai-providers");

test("lists providers without keys", async () => {
  const { host } = await setup();
  const text = visibleText(host);
  assert.match(text, /Claude.*API key saved/);
  assert.match(text, /Local.*http:\/\/100\.102\.234\.29:11434/);
  assert.ok(host.querySelectorAll("input").every((i) => !i.value || !/sk-/.test(i.value)));
});

test("add Claude asks consent with Anthropic copy", async () => {
  const { host, calls } = await setup({ providers: [] });
  buttonNamed(host, "Add provider").click();
  change(select(host, "Provider"), "claude");
  await settle();
  assert.match(visibleText(host), /This lets OpenDevEngine send parts of your code to Anthropic\./);
  input(field(host, "API key"), "sk-ant-typed");
  buttonNamed(host, "Save provider").click();
  await settle();
  assert.equal(posts(calls).length, 0, "nothing saved before consent");
  assert.match(visibleText(host), /Confirm that code may be sent/);
  buttonNamed(host, "Cancel").click();
  await settle();
  assert.equal(posts(calls).length, 0);
  assert.equal(field(host, "API key"), undefined, "form closed, key gone");
});

test("add Claude with consent posts once and clears the key", async () => {
  const { host, calls } = await setup({ providers: [] });
  buttonNamed(host, "Add provider").click();
  change(select(host, "Provider"), "claude");
  await settle();
  input(field(host, "API key"), "sk-ant-typed");
  field(host, "I agree").click();
  buttonNamed(host, "Save provider").click();
  await settle();
  assert.deepEqual(posts(calls).map(([, , body]) => body), [{ kind: "claude", api_key: "sk-ant-typed", consent: true }]);
});

test("Claude login option sends use_login instead of a key", async () => {
  const { host, calls } = await setup({ providers: [] });
  buttonNamed(host, "Add provider").click();
  change(select(host, "Provider"), "claude");
  await settle();
  change(select(host, "Sign in with"), "login");
  await settle();
  assert.equal(field(host, "API key"), undefined);
  field(host, "I agree").click();
  buttonNamed(host, "Save provider").click();
  await settle();
  assert.deepEqual(posts(calls)[0][2], { kind: "claude", use_login: true, consent: true });
});

test("Codex names OpenAI in its consent", async () => {
  const { host } = await setup({ providers: [] });
  buttonNamed(host, "Add provider").click();
  change(select(host, "Provider"), "codex");
  await settle();
  assert.match(visibleText(host), /This lets OpenDevEngine send parts of your code to OpenAI\./);
});

test("add Local shows trust note, no consent", async () => {
  const { host, calls } = await setup({ providers: [] });
  buttonNamed(host, "Add provider").click();
  change(select(host, "Provider"), "local");
  await settle();
  assert.match(visibleText(host), /Treated as your own machine: code is sent here without asking\./);
  assert.equal(field(host, "I agree"), undefined);
  input(field(host, "API URL"), "http://100.102.234.29:11434");
  buttonNamed(host, "Save provider").click();
  await settle();
  assert.deepEqual(posts(calls)[0][2], { kind: "local", endpoint: "http://100.102.234.29:11434", adapter: "ollama" });
});

test("Plans & answers sends one PATCH", async () => {
  const { host, calls } = await setup();
  change(select(host, "Plans & answers provider"), "aip_local");
  await settle();
  change(field(host, "Plans & answers model"), "qwen3:8b");
  await settle();
  const puts = calls.filter(([m, p]) => m === "PATCH" && p === "/api/ai-jobs/plans");
  assert.deepEqual(puts.at(-1)[2], { provider_id: "aip_local", model: "qwen3:8b" });
  assert.ok(calls.some(([m, p]) => m === "POST" && p === "/api/ai-jobs/plans/test"), "connection test runs after choosing");
});

test("Coding offers Claude only; Codex disabled", async () => {
  const { host } = await setup({ providers: [LOCAL, CLAUDE, CODEX] });
  const options = select(host, "Coding provider").querySelectorAll("option");
  const codex = options.find((o) => o.value === "aip_codex");
  assert.ok(codex.disabled);
  assert.match(codex.textContent, /Coding — coming later/);
  assert.equal(options.find((o) => o.value === "aip_local"), undefined);
  assert.ok(!options.find((o) => o.value === "aip_claude").disabled);
});

test("coding status polls until passed", async () => {
  const { host } = await setup({ jobs: { coding: { provider_id: "aip_claude", model: "sonnet", status: "running" } },
    jobSequence: [{}, { coding: { provider_id: "aip_claude", model: "sonnet", status: "passed" } }] });
  await new Promise((r) => setTimeout(r, 20));
  await settle();
  assert.match(visibleText(host), /✓ tested/);
});

test("blocked job shows privacy text", async () => {
  const { host } = await setup({ jobs: { plans: { provider_id: "aip_claude", model: "claude-sonnet-5-5", status: "blocked" } } });
  assert.match(visibleText(host), /Blocked by privacy setting/);
});

test("old models and agent UIs live under Advanced", async () => {
  const make = (text) => { const node = globalThis.document.createElement("section"); node.append(text); return node; };
  const { host } = await setup({ advanced: () => ({ models: make("Old models UI"), agent: make("Old agent UI") }) });
  const details = host.querySelector("details.existing-app");
  assert.ok(details);
  assert.match(visibleText(details), /Old models UI/);
  assert.match(visibleText(details), /Old agent UI/);
});
