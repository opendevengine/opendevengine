// Saved plan document: readable sections, citation chips, plain readiness, folded identifiers.
import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createBrowser, install, visibleText } from "./dom.mjs";

const url = pathToFileURL(fileURLToPath(new URL("../../opendevengine/web/plan-view.js", import.meta.url))).href;
const citation = { id: "e1", repository_id: "gh:748959122", snapshot_id: "snap_8003", revision: "7aa13d2992d79b52df0291554c54138d2697904b",
  path: "src/pages/user/home-page.tsx", start_line: 79, end_line: 138 };
const version = (over = {}) => ({
  id: "pv_c41f", version: 1, content_hash: "dd69204ae489", created_at: Date.UTC(2026, 10, 16),
  generation: { model: "claude-sonnet-5-5", usage: { input_tokens: 4209, output_tokens: 1536 } },
  readiness: { state: "needs_input", blocking_reasons: ["blocking_questions", "execution_not_configured", "implementation_not_permitted"], warnings: ["source_stale"] },
  content: {
    goal: "Integrate a payment gateway into the Laundry Store app", user_request: "we need to integrate payment gateway",
    repositories: [{ repository_id: "gh:748959122", role: "write", snapshot_id: "snap_8003", revision: citation.revision, branch: "main" }],
    citations: [citation],
    findings: [{ text: "Checkout posts the cart directly to the orders API.", citation_ids: ["e1"] }],
    requirements: ["Support credit-card and digital-wallet payments"],
    steps: [{ title: "Add a payment service", description: "Wrap the gateway SDK.", paths: ["src/services/payment.ts"], citation_ids: ["e1"] }],
    validation: [{ command: "npm test", expected: "All tests pass", source: "proposed", citation_ids: [] }],
    risks: ["Incorrect handling of payment tokens"], assumptions: ["Stripe is acceptable"],
    open_questions: [{ id: "q1", text: "Which gateway should we use?", blocking: true, answer: null },
      { id: "q2", text: "Support refunds now?", blocking: false, answer: null }],
    execution_context: { execution_profile_id: null, skills: [], plugins: [], memory: [], base_choices: {} }, publication: { policy: "manual_draft" },
  }, ...over });

async function render(v) {
  const b = createBrowser("<!doctype html><html><body><div id=host></div></body></html>", { respond: () => ({ status: 404, body: {} }) });
  install(b);
  const { renderPlanDocument } = await import(`${url}?t=${Math.random()}`);
  const opened = [];
  const doc = renderPlanDocument(v, { nameFor: (id) => (id === "gh:748959122" ? "laundro_frontend_v2" : id), onCitation: (c) => opened.push(c) });
  b.document.getElementById("host").append(doc);
  return { doc, opened, text: visibleText(doc) };
}

test("a saved plan reads as a document with names, not identifiers", async () => {
  const { text } = await render(version());
  assert.match(text, /Integrate a payment gateway into the Laundry Store app/);
  assert.match(text, /Version 1 · laundro_frontend_v2 \(changes\) · claude-sonnet-5-5/);
  for (const heading of ["Needs your answer", "What you asked", "Requirements", "What the code does today", "Steps", "How to check it", "Risks", "Assumptions", "Other questions", "Sources"]) {
    assert.match(text, new RegExp(heading), heading);
  }
  assert.match(text, /Which gateway should we use\?/);
  assert.match(text, /src\/services\/payment\.ts/);
  assert.match(text, /Not approved — this check will be skipped\./);
});

test("readiness codes become sentences and identifiers are folded into Version details", async () => {
  const { doc, text } = await render(version());
  assert.match(text, /Choose a tested execution profile: Edit plan → Execution and publication/);
  assert.match(text, /Turn on "Allow changes" in Repositories/);
  assert.match(text, /The repository has changed since it was indexed/);
  assert.doesNotMatch(text, /execution_not_configured|implementation_not_permitted|blocking_questions/);
  const details = doc.querySelector("details");
  assert.equal(details.querySelector("summary").textContent, "Version details");
  assert.match(details.textContent, /pv_c41f/);
  assert.match(details.textContent, /dd69204ae489/);
  assert.match(details.textContent, /7aa13d2992d79b52df0291554c54138d2697904b/);
});

test("citation chips open the cited source", async () => {
  const { doc, opened } = await render(version());
  const chips = doc.querySelectorAll("button.cite");
  assert.ok(chips.some((chip) => chip.textContent === "home-page.tsx:79–138"));
  chips[0].click();
  assert.equal(opened[0].path, "src/pages/user/home-page.tsx");
});

test("redacted content shows a plain notice", async () => {
  const { text } = await render(version({ content: null }));
  assert.match(text, /unavailable or was removed/);
});

test("each check says whether it will run, and unapproved checks are a readiness warning", async () => {
  const content = { ...version().content, validation: [
    { command: "npm test", expected: "All tests pass", source: "proposed", citation_ids: [], approved: true },
    { command: "Run the app and check it.", expected: "", source: "proposed", citation_ids: [] }] };
  const { text } = await render(version({ content, readiness: { state: "ready", blocking_reasons: [], warnings: ["validation_unapproved"] } }));
  assert.match(text, /Approved — runs as a shell command in the session container\./);
  assert.match(text, /Not approved — this check will be skipped\. Approve it by editing the plan\./);
  assert.match(text, /Some checks are not approved and will be skipped\./);
  assert.doesNotMatch(text, /Proposed, not run yet\./);
});
