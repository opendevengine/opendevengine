// Shared UI primitives: switch with optimistic save, focus-trapping modal, relative time.
import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createBrowser, install, keydown, settle, visibleText } from "./dom.mjs";

const uiUrl = pathToFileURL(fileURLToPath(new URL("../../opendevengine/web/ui.js", import.meta.url))).href;
let loads = 0;

async function setup() {
  const b = createBrowser("<!doctype html><html><body></body></html>", { respond: () => ({ status: 404, body: {} }) });
  install(b);
  loads += 1;
  return { b, ui: await import(`${uiUrl}?u=${loads}`) };
}

test("el, button and badge build labelled elements", async () => {
  const { b, ui } = await setup();
  const parent = ui.el("div");
  let clicked = 0;
  const primary = ui.button(parent, "Add 2", () => { clicked += 1; }, { variant: "primary", title: "Adds two" });
  primary.click();
  assert.equal(clicked, 1);
  assert.equal(primary.type, "button");
  assert.ok(primary.classList.contains("btn-primary"));
  assert.equal(primary.getAttribute("title"), "Adds two");
  const badge = ui.badge("warn", "Indexed · keyword only");
  assert.ok(badge.classList.contains("badge-warn"));
  assert.equal(badge.textContent, "Indexed · keyword only");
  assert.equal(ui.el("p", "hi", "x").className, "x");
  assert.ok(b.document);
});

test("toggleSwitch saves optimistically, reverts and announces on failure, ignores double click", async () => {
  const { ui } = await setup();
  let calls = 0;
  const s = ui.toggleSwitch({ label: "Use in Ask", checked: false, onChange: () => { calls += 1; return Promise.reject(new Error("Network down")); } });
  const control = s.root.querySelector('[role="switch"]');
  assert.equal(control.getAttribute("aria-label"), "Use in Ask");
  control.click();
  assert.equal(control.getAttribute("aria-checked"), "true", "flips before the save finishes");
  control.click();
  await settle();
  assert.equal(calls, 1, "second click while saving is ignored");
  assert.equal(control.getAttribute("aria-checked"), "false");
  assert.match(visibleText(s.root), /Network down/);
  assert.equal(s.root.querySelector('[aria-live="polite"]') !== null, true);
});

test("toggleSwitch keeps the new value on success and set() updates it", async () => {
  const { ui } = await setup();
  const seen = [];
  const s = ui.toggleSwitch({ label: "Allow changes", checked: false, onChange: async (next) => { seen.push(next); } });
  const control = s.root.querySelector('[role="switch"]');
  control.click();
  await settle();
  assert.deepEqual(seen, [true]);
  assert.equal(control.getAttribute("aria-checked"), "true");
  s.set(false);
  assert.equal(control.getAttribute("aria-checked"), "false");
});

test("modal traps focus, closes on Escape with null and restores focus", async () => {
  const { b, ui } = await setup();
  const trigger = ui.button(b.document.body, "+", () => {});
  trigger.focus();
  const m = ui.createModal({ title: "Choose repositories" });
  const a = ui.button(m.body, "A", () => {});
  const z = ui.button(m.footer, "Z", () => {});
  m.open(trigger);
  assert.ok(m.root.hasAttribute("open"));
  assert.match(visibleText(m.root), /Choose repositories/);
  z.focus();
  keydown(z, "Tab");
  assert.equal(b.document.activeElement, m.root.querySelector(".modal-close"), "Tab from last wraps to first");
  keydown(b.document.activeElement, "Tab", { shiftKey: true });
  assert.equal(b.document.activeElement, z, "Shift+Tab from first wraps to last");
  assert.ok(a);
  keydown(m.root, "Escape");
  assert.equal(await m.closed, null);
  assert.equal(m.root.hasAttribute("open"), false);
  assert.equal(b.document.activeElement, trigger);
});

test("modal close(result) resolves with the result", async () => {
  const { b, ui } = await setup();
  const trigger = ui.button(b.document.body, "+", () => {});
  const m = ui.createModal({ title: "Pick" });
  m.open(trigger);
  m.close(["gh:1"]);
  assert.deepEqual(await m.closed, ["gh:1"]);
});

test("relativeTime buckets", async () => {
  const { ui } = await setup();
  const now = Date.UTC(2026, 10, 15, 12);
  assert.equal(ui.relativeTime(now - 30_000, now), "now");
  assert.equal(ui.relativeTime(now - 5 * 60_000, now), "5 min ago");
  assert.equal(ui.relativeTime(now - 3 * 3_600_000, now), "3 h ago");
  assert.equal(ui.relativeTime(now - 26 * 3_600_000, now), "yesterday");
  assert.equal(ui.relativeTime(Date.UTC(2026, 9, 2, 12), now), "2 Oct");
});
