// File viewer tabs for the side panel: opens a citation's exact snapshot with the cited lines highlighted.
import { button, el } from "./ui.js";

const WINDOW = 500;
const CONTEXT = 50;
const tabsByPanel = new WeakMap();
const enc = encodeURIComponent;

function query(citation, start, end, context) {
  return `/api/context/source?repository_id=${enc(citation.repository_id)}&snapshot_id=${enc(citation.snapshot_id)}`
    + `&path=${enc(citation.path)}&start_line=${start}&end_line=${end}&context_lines=${context}`;
}

export function openSource(panel, citation, { request, repoName = null, branch = null, githubUrl = null }) {
  if (!tabsByPanel.has(panel)) tabsByPanel.set(panel, new Map());
  const tabs = tabsByPanel.get(panel);
  const id = `${citation.repository_id}:${citation.path}`;
  const state = { citation, lines: new Map(), first: null, last: null, total: null, error: null, token: (tabs.get(id)?.token ?? 0) + 1, body: null };
  tabs.set(id, state);

  function draw() {
    const body = state.body;
    if (!body || !body.isConnected) return;
    const { citation: c } = state;
    const short = String(c.revision ?? "").slice(0, 7);
    const header = el("p", `${repoName ?? c.repository_id}${branch ? ` · ${branch}` : ""} @ ${short}`, "code-head");
    const children = [header, el("p", c.path, "code-path")];
    if (githubUrl && /^[0-9a-f]{40}$/.test(c.revision ?? "")) {
      const link = el("a", "↗ View on GitHub", "code-github");
      link.setAttribute("href", `${githubUrl}/blob/${c.revision}/${c.path.split("/").map(enc).join("/")}#L${c.start_line}-L${c.end_line}`);
      link.setAttribute("rel", "noreferrer");
      link.setAttribute("target", "_blank");
      children.push(link);
    }
    if (state.error) {
      children.push(el("p", `This source is no longer available: ${state.error}`, "code-error"));
      body.replaceChildren(...children);
      return;
    }
    if (state.first === null) {
      children.push(el("p", "Loading source…", "code-status"));
      body.replaceChildren(...children);
      return;
    }
    if (state.first > 1) {
      const earlier = button(null, "Show earlier", () => load(Math.max(1, state.first - WINDOW), state.first - 1, 0));
      children.push(earlier);
    }
    const pre = el("div", undefined, "code-view");
    pre.setAttribute("role", "region");
    pre.setAttribute("aria-label", `${c.path} lines ${state.first}–${state.last}`);
    for (let number = state.first; number <= state.last; number += 1) {
      const row = el("div", undefined, `code-line${number >= c.start_line && number <= c.end_line ? " hl" : ""}`);
      row.append(el("span", String(number), "ln"), el("code", state.lines.get(number) ?? ""));
      pre.append(row);
    }
    children.push(pre);
    if (state.total !== null && state.last < state.total) {
      children.push(button(null, "Show more", () => load(state.last + 1, Math.min(state.total, state.last + WINDOW), 0)));
    }
    body.replaceChildren(...children);
  }

  async function load(start, end, context) {
    const token = state.token;
    try {
      const source = await request(query(state.citation, start, end, context));
      if (tabs.get(id) !== state || token !== state.token) return;
      source.lines.forEach((line, index) => state.lines.set(source.first_line + index, line));
      const lastLine = source.first_line + source.lines.length - 1;
      state.first = state.first === null ? source.first_line : Math.min(state.first, source.first_line);
      state.last = state.last === null ? lastLine : Math.max(state.last, lastLine);
      state.total = Number.isFinite(source.total_lines) ? source.total_lines : state.last;
    } catch (error) {
      if (tabs.get(id) !== state) return;
      state.error = error.message;
    }
    draw();
  }

  panel.openTab({ id, title: citation.path.split("/").pop(), render: (body) => { state.body = body; draw(); } });
  load(citation.start_line, citation.end_line, CONTEXT);
}
