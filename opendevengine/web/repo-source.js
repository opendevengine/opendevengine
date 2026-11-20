// Repository search for pickers and lists: index state from /api/context/repositories,
// enriched with GitHub catalog metadata, mapped to one plain status label.

const ACTIVE_RUN = new Set(["queued", "running", "paused"]);

function clock(ms) {
  const date = new Date(ms);
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

export function repoStatus(item, catalogItem) {
  if (catalogItem && catalogItem.access_state === "inaccessible") return { tone: "off", label: "No access" };
  if (item.status === "excluded" || item.eligible === false) return { tone: "off", label: "Off" };
  const run = item.active_run;
  if (!item.active_snapshot && run) {
    if (run.state === "waiting") return { tone: "info", label: run.retry_at ? `Waiting until ${clock(run.retry_at)}` : "Waiting" };
    if (ACTIVE_RUN.has(run.state)) {
      const { files_done: done, files_total: total } = run.progress || {};
      const percent = Number.isFinite(done) && Number.isFinite(total) && total > 0 ? Math.min(99, Math.floor((done / total) * 100)) : null;
      return { tone: "info", label: percent === null ? "Indexing…" : `Indexing ${percent}%` };
    }
  }
  if (item.status === "failed") return { tone: "danger", label: "Failed" };
  if (item.active_snapshot) {
    const mode = item.active_snapshot.mode ?? item.active_snapshot.coverage?.mode;
    return mode === "lexical_only" ? { tone: "warn", label: "Indexed · keyword only" } : { tone: "ok", label: "Indexed" };
  }
  if (item.status === "indexing") return { tone: "info", label: "Indexing…" };
  return { tone: "off", label: "Not indexed" };
}

const UNPICKABLE = new Set(["Not indexed", "No access", "Off"]);

function splitName(full) {
  const index = full.indexOf("/");
  return index === -1 ? { owner: null, name: full } : { owner: full.slice(0, index), name: full.slice(index + 1) };
}

function query(params) {
  return Object.entries(params).filter(([, value]) => value !== null && value !== undefined && value !== "")
    .map(([key, value]) => `${key}=${encodeURIComponent(value)}`).join("&");
}

export async function searchRepositories(request, { query: text = "", cursor = null, limit = 25 } = {}) {
  const page = await request(`/api/context/repositories?${query({ limit, query: text, cursor })}`);
  let catalog = new Map();
  try {
    const result = await request(`/api/repositories?${query({ limit: 100, query: text })}`);
    catalog = new Map((result.items || []).map((item) => [item.id, item]));
  } catch {
    // Catalog metadata is optional; index state alone still lists repositories.
  }
  const items = (page.items || []).map((item) => {
    const meta = catalog.get(item.repository_id);
    const parts = splitName(item.name || item.repository_id);
    const status = repoStatus(item, meta);
    return {
      id: item.repository_id,
      name: meta?.name ?? parts.name,
      owner: meta?.owner ?? parts.owner,
      language: meta?.language ?? null,
      status,
      selectable: item.eligible !== false && !UNPICKABLE.has(status.label),
    };
  });
  return { items, nextCursor: page.next_cursor ?? null };
}

const RECENT_KEY = "od.recentRepos";

export function recentRepositoryIds() {
  try {
    const value = JSON.parse(globalThis.localStorage?.getItem(RECENT_KEY) ?? "[]");
    return Array.isArray(value) ? value.filter((id) => typeof id === "string").slice(0, 8) : [];
  } catch {
    return [];
  }
}

export function rememberRepositories(ids) {
  try {
    const merged = [...new Set([...ids, ...recentRepositoryIds()])].slice(0, 8);
    globalThis.localStorage?.setItem(RECENT_KEY, JSON.stringify(merged));
  } catch {
    // Recent repositories are a convenience; storage can be unavailable.
  }
}

// Traverse each cursor exactly once; consumers never infer whole-catalog state
// from the first page. Duplicate rows are merged by durable repository identity.
export async function readRepositoryPages(request, path) {
  const rows = new Map(), seen = new Set(); let cursor = null, pages = 0;
  do {
    const page = await request(`${path}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`);
    if (!Array.isArray(page?.items)) throw new Error('Repository page unavailable. Reload to try again.');
    for (const item of page.items) rows.set(item.repository_id || item.id, item);
    ++pages; cursor = page.next_cursor || null;
    if (cursor && seen.has(cursor)) throw new Error('Repository paging did not advance. Reload to inspect the full catalog.');
    if (cursor) seen.add(cursor);
  } while (cursor);
  return {items:[...rows.values()],pages};
}
