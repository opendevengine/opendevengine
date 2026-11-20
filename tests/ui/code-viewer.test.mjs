// Code viewer: citations open a file tab in the side panel with the cited lines highlighted.
import assert from "node:assert/strict";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createBrowser, install, keydown, settle, visibleText } from "./dom.mjs";

const viewerUrl = pathToFileURL(fileURLToPath(new URL("../../opendevengine/web/code-viewer.js", import.meta.url))).href;
const panelUrl = pathToFileURL(fileURLToPath(new URL("../../opendevengine/web/panel.js", import.meta.url))).href;
let loads = 0;
const cite = (over = {}) => ({ repository_id: "gh:1", snapshot_id: "snap_1", revision: "d8f5918f985503cd5a2fd0c0956abd32b4800f3c", path: "src/lib/api.ts", start_line: 14, end_line: 16, ...over });

async function setup() {
  const b = createBrowser("<!doctype html><html><body><button id=chip>api.ts:14–16</button><aside id=side hidden></aside></body></html>", { respond: () => ({ status: 404, body: {} }) });
  install(b);
  loads += 1;
  const { openSource } = await import(`${viewerUrl}?v=${loads}`);
  const { createSidePanel } = await import(`${panelUrl}?v=${loads}`);
  const calls = [];
  const request = async (path) => {
    calls.push(path);
    const q = new URL(path, "http://x").searchParams;
    const start = Number(q.get("start_line")), end = Number(q.get("end_line")), ctx = Number(q.get("context_lines") ?? 0);
    const first = Math.max(1, start - ctx), last = Math.min(1200, end + ctx);
    return { repository_id: "gh:1", snapshot_id: "snap_1", revision: "d8f5918f985503cd5a2fd0c0956abd32b4800f3c", path: q.get("path"), first_line: first,
      lines: Array.from({ length: last - first + 1 }, (_, i) => `line ${first + i}`), total_lines: 1200 };
  };
  const panel = createSidePanel(b.document.getElementById("side"));
  return { b, openSource, panel, request, calls, host: b.document.getElementById("side") };
}

test("a citation opens a file tab with 50 lines of context and the cited lines highlighted", async () => {
  const { b, openSource, panel, request, calls, host } = await setup();
  b.document.getElementById("chip").focus();
  openSource(panel, cite(), { request, repoName: "acme-dev/storefront-app", branch: "main", githubUrl: "https://github.com/acme-dev/storefront-app" });
  await settle();
  const query = new URL(calls[0], "http://x").searchParams;
  assert.equal(query.get("context_lines"), "50");
  assert.equal(query.get("start_line"), "14");
  assert.equal(query.get("snapshot_id"), "snap_1");
  assert.match(visibleText(host), /acme-dev\/storefront-app · main @ d8f5918/);
  assert.deepEqual(host.querySelectorAll('[role="tab"]').map((t) => t.textContent), ["api.ts"]);
  const highlighted = host.querySelectorAll(".code-line.hl").map((row) => row.querySelector(".ln").textContent);
  assert.deepEqual(highlighted, ["14", "15", "16"]);
  const link = host.querySelectorAll("a").find((a) => a.textContent.includes("GitHub"));
  assert.equal(link.getAttribute("href"), "https://github.com/acme-dev/storefront-app/blob/d8f5918f985503cd5a2fd0c0956abd32b4800f3c/src/lib/api.ts#L14-L16");
  assert.equal(link.getAttribute("rel"), "noreferrer");
});

test("Show more loads the next window of up to 500 lines, Show earlier the previous one", async () => {
  const { openSource, panel, request, calls, host } = await setup();
  openSource(panel, cite({ start_line: 600, end_line: 602 }), { request });
  await settle();
  host.querySelectorAll("button").find((x) => x.textContent === "Show more").click();
  await settle();
  let q = new URL(calls.at(-1), "http://x").searchParams;
  assert.deepEqual([q.get("start_line"), q.get("end_line"), q.get("context_lines")], ["653", "1152", "0"]);
  host.querySelectorAll("button").find((x) => x.textContent === "Show earlier").click();
  await settle();
  q = new URL(calls.at(-1), "http://x").searchParams;
  assert.deepEqual([q.get("start_line"), q.get("end_line")], ["50", "549"]);
  assert.equal(host.querySelectorAll(".code-line").length, 1152 - 50 + 1);
});

test("a second citation in the same file reuses its tab; Escape closes and restores focus", async () => {
  const { b, openSource, panel, request, host } = await setup();
  const chip = b.document.getElementById("chip");
  chip.focus();
  openSource(panel, cite(), { request });
  openSource(panel, cite({ path: "src/routes.ts" }), { request });
  openSource(panel, cite({ start_line: 40, end_line: 41 }), { request });
  await settle();
  assert.deepEqual(host.querySelectorAll('[role="tab"]').map((t) => t.textContent), ["api.ts", "routes.ts"]);
  assert.deepEqual(host.querySelectorAll(".code-line.hl").map((row) => row.querySelector(".ln").textContent), ["40", "41"]);
  keydown(host, "Escape");
  assert.equal(host.hidden, true);
  assert.equal(b.document.activeElement, chip);
});

test("source that is no longer available says so", async () => {
  const { openSource, panel, host } = await setup();
  openSource(panel, cite(), { request: async () => { const e = new Error("Source unavailable."); e.status = 404; throw e; } });
  await settle();
  assert.match(visibleText(host), /This source is no longer available: Source unavailable\./);
});
