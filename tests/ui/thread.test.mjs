// Chat thread rendering: messages, work steps, citations, routing, plans and errors.
import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createBrowser, install, visibleText } from "./dom.mjs";

const url = pathToFileURL(fileURLToPath(new URL("../../opendevengine/web/thread.js", import.meta.url))).href;
let loads = 0;
const NOW = 1794721200000;

async function setup() {
  const b = createBrowser("<!doctype html><html><body><div id=host></div></body></html>", { respond: () => ({ status: 404, body: {} }) });
  install(b);
  loads += 1;
  const { renderThread } = await import(`${url}?t=${loads}`);
  const calls = [];
  const handlers = Object.fromEntries(["onCitation", "onViewPlan", "onStartSession", "onRegenerate", "onRetry", "onPickRoute", "onChooseRepos"]
    .map((name) => [name, (...args) => calls.push([name, ...args])]));
  const host = b.document.getElementById("host");
  return { b, host, calls, render: (view) => renderThread(host, { now: NOW, messages: [], runs: [], plans: [], ...view }, handlers) };
}

const user = (over = {}) => ({ id: "msg_u1", role: "user", content: "What does storefront-app do?", state: "complete", created_at: NOW - 60000, citations: [], ...over });
const cite = { repository_id: "gh:1", snapshot_id: "snap_1", revision: "d8f5918f985503cd5a2fd0c0956abd32b4800f3c", path: "src/lib/api.ts", start_line: 14, end_line: 58 };
const answer = (over = {}) => ({ id: "msg_a1", role: "assistant", content: "It wraps fetch in `api()` with token refresh.", state: "complete", created_at: NOW - 50000,
  model: "llama3.2:latest", destination: { kind: "local", host: "127.0.0.1:11434" }, usage: { input_tokens: 812, output_tokens: 140 }, citations: [cite], ...over });

test("user and assistant messages render with citation chips and a details button", async () => {
  const { host, calls, render } = await setup();
  render({ messages: [user(), answer()] });
  assert.equal(visibleText(host.querySelector(".msg-user")), "What does storefront-app do?");
  const reply = host.querySelector(".msg-assistant");
  assert.match(visibleText(reply), /It wraps fetch in api\(\) with token refresh\./);
  assert.equal(reply.querySelector("code").textContent, "api()");
  const chip = reply.querySelector("button.cite");
  assert.equal(chip.textContent, "api.ts:14–58");
  assert.equal(chip.getAttribute("title"), "src/lib/api.ts lines 14–58");
  chip.click();
  assert.equal(calls[0][0], "onCitation");
  assert.deepEqual(calls[0][1], cite);
  assert.equal(calls[0][2], chip);
  const info = reply.querySelectorAll("button").find((x) => x.getAttribute("aria-label") === "Answer details");
  info.click();
  assert.match(visibleText(reply), /llama3\.2:latest · local 127\.0\.0\.1:11434 · 812 in \/ 140 out tokens · cost unknown/);
});

test("pending answers show waiting copy with elapsed time and never internal ids", async () => {
  const { host, render } = await setup();
  render({ messages: [user(), answer({ state: "pending", content: null, citations: [], created_at: NOW - 12000 })],
    runs: [{ id: "answer_7e6a156e789b922662ba65b5", assistant_message_id: "msg_a1", state: "queued" }] });
  assert.match(visibleText(host), /Waiting for the model… 12 s/);
  assert.doesNotMatch(visibleText(host), /answer_|conv_|msg_/);
});

test("failed answers explain the cause in words and offer Retry", async () => {
  const { host, calls, render } = await setup();
  render({ messages: [user(), answer({ state: "failed", content: null, citations: [], error_code: "provider_timeout" })],
    runs: [{ id: "answer_1", assistant_message_id: "msg_a1", state: "failed", error_code: "provider_timeout" }] });
  assert.match(visibleText(host), /Couldn't answer: the model took too long\. A smaller model, or one running on a GPU, may respond in time; choose it in Settings → Models\./);
  host.querySelectorAll("button").find((x) => x.textContent === "Retry").click();
  assert.deepEqual(calls, [["onRetry", "answer_1"]]);
});

test("a failed plan request says the plan could not be made", async () => {
  const { host, render } = await setup();
  render({ messages: [user(), answer({ state: "failed", content: null, citations: [], error_code: "provider_timeout" })],
    runs: [{ id: "answer_2", assistant_message_id: "msg_a1", state: "failed", error_code: "provider_timeout", intent: "plan" }] });
  assert.match(visibleText(host), /Couldn't make the plan: the model took too long\./);
});

test("resolved routing shows a work step with change", async () => {
  const { host, calls, render } = await setup();
  render({ messages: [user()], routing: { state: "resolved", candidates: [{ repository_id: "gh:1", name: "acme-dev/storefront-app", reason: "the name matches" }] } });
  const step = host.querySelector(".thread-step");
  assert.match(visibleText(step), /Looked in storefront-app because the name matches · change/);
  step.querySelector("button").click();
  assert.deepEqual(calls, [["onChooseRepos"]]);
});

test("needs_selection routing asks which repository", async () => {
  const { host, calls, render } = await setup();
  render({ messages: [user()], routing: { state: "needs_selection", candidates: [
    { repository_id: "gh:1", name: "acme-dev/storefront-app", reason: "README mentions API" },
    { repository_id: "gh:2", name: "acme-dev/deploy-tools", reason: "deploy config" }] } });
  const card = host.querySelector(".thread-card");
  assert.match(visibleText(card), /Which repository\?/);
  assert.match(visibleText(card), /README mentions API/, "each choice shows why it was suggested");
  render({ messages: [user()], routing: { state: "needs_selection", candidates: [
    { repository_id: "gh:3", name: "o/worker", reason: "Catalog evidence: name: worker, profile: and, order" }] } });
  assert.match(visibleText(host.querySelector(".thread-card")), /Matches worker, order/, "legacy comma-separated reasons read cleanly");
  render({ messages: [user()], routing: { state: "needs_selection", candidates: [
    { repository_id: "gh:1", name: "acme-dev/storefront-app", reason: "README mentions API" },
    { repository_id: "gh:2", name: "acme-dev/deploy-tools", reason: "deploy config" }] } });
  card.querySelectorAll(".route-choice").find((x) => x.querySelector(".route-choice-name").textContent === "deploy-tools").click();
  card.querySelectorAll("button").find((x) => x.textContent === "Choose…").click();
  assert.deepEqual(calls, [["onPickRoute", "gh:2"], ["onChooseRepos"]]);
});

test("plan cards show steps and version; Start is disabled with the reason when not ready", async () => {
  const { host, calls, render } = await setup();
  render({ messages: [user(), answer()], plans: [{ id: "plan_1", title: "Add request retry to the API client", version: 2, steps: 4, ready: false, reason: "No execution profile selected", created_at: NOW - 1000 }] });
  const card = host.querySelector(".plan-card");
  assert.match(visibleText(card), /Add request retry to the API client · 4 steps · v2/);
  const start = card.querySelectorAll("button").find((x) => x.textContent === "Start session");
  assert.equal(start.disabled, true);
  assert.equal(start.getAttribute("title"), "No execution profile selected");
  card.querySelectorAll("button").find((x) => x.textContent === "View plan").click();
  assert.deepEqual(calls, [["onViewPlan", "plan_1"]]);
});

test("a plan made before the scope changed offers Regenerate", async () => {
  const { host, calls, render } = await setup();
  render({ plans: [{ id: "plan_1", title: "Doc", version: 1, steps: 2, ready: true, reason: "", created_at: NOW - 9000 }],
    staleScopeSincePlan: { planId: "plan_1", addedNames: ["cube-workbench"] } });
  const card = host.querySelector(".plan-card");
  assert.match(visibleText(card), /Made before cube-workbench was added · Regenerate/);
  card.querySelectorAll("button").find((x) => x.textContent === "Regenerate").click();
  assert.deepEqual(calls, [["onRegenerate", "plan_1"]]);
});

test("removing a repository also stales the plan and blocks Start until it is regenerated", async () => {
  const { host, render } = await setup();
  render({ plans: [{ id: "plan_1", title: "Doc", version: 1, steps: 2, ready: true, reason: "", created_at: NOW - 9000 }],
    staleScopeSincePlan: { planId: "plan_1", addedNames: [], removedNames: ["edgemesh"] } });
  const card = host.querySelector(".plan-card");
  assert.match(visibleText(card), /Made before edgemesh was removed · Regenerate/);
  assert.equal(card.querySelectorAll("button").find((x) => x.textContent === "Start session").disabled, true);
});

test("scope events appear in time order between messages", async () => {
  const { host, render } = await setup();
  render({ messages: [user({ created_at: NOW - 30000 }), answer({ created_at: NOW - 20000 })], scopeEvents: [{ kind: "added", name: "cube-workbench", at: NOW - 25000 }] });
  const order = host.querySelectorAll(".msg-user, .msg-assistant, .thread-event").map((n) => n.className.split(" ")[0]);
  assert.deepEqual(order, ["msg-user", "thread-event", "msg-assistant"]);
  assert.match(visibleText(host.querySelector(".thread-event")), /Added cube-workbench/);
});

test("provider limits and refusals read as plain sentences", async () => {
  const mod = await import(`${url}?errors=1`);
  assert.match(mod.errorWords("context_overflow"), /too large for this model's limits\. Lower its context token limit in Settings → Models/);
  assert.match(mod.errorWords("provider_rate_limited"), /rate limit was reached/);
  assert.match(mod.errorWords("invalid_output"), /didn't pass validation/);
  assert.equal(mod.errorWords("some_new_code"), "some new code");
});

test("the server's 'Plan saved' receipt is hidden when its plan card is shown", async () => {
  const { host, render } = await setup();
  const plan = { id: "plan_abc", title: "Add retries", steps: 2, version: 1, ready: false, created_at: NOW - 40000 };
  render({ messages: [user(), answer({ content: "Plan saved: plan_abc", citations: [] })], plans: [plan] });
  assert.doesNotMatch(visibleText(host), /Plan saved/);
  assert.match(visibleText(host), /Add retries · 2 steps · v1/);
  render({ messages: [user(), answer({ content: "Plan saved: plan_other", citations: [] })], plans: [plan] });
  assert.match(visibleText(host), /Plan saved: plan_other/, "an unmatched receipt still shows");
});

test("unconfigured execution has a visible explanation and editor fix link", async () => {
  const {host, calls, render} = await setup();
  render({plans:[{id:'p',latest:'v',ready:false,reason:'execution_not_configured'}]});
  assert.match(visibleText(host.querySelector('.plan-card-reason')), /Choose a tested execution profile/);
  const fix = host.querySelector('.plan-card-reason a');
  assert.equal(fix.getAttribute('href'), '#/plans/p/v');
  assert.match(fix.textContent, /Execution and publication/);
  fix.click();
  assert.deepEqual(calls, [['onStartSession','p',{fix:true}]]);
});
