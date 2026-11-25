// Repositories page: one list of GitHub catalog entries merged with index state.
import { badge, button, createModal, el, relativeTime, toggleSwitch } from "./ui.js";
import { repoStatus, readRepositoryPages } from "./repo-source.js";

const enc = encodeURIComponent;
const FILTERS = ["All", "Used in Ask", "Changes allowed", "Needs attention"];
const SKIP_WORDS = { generated: "generated files", sensitive: "files that look like secrets", binary_or_sensitive: "binary or secret-like files" };
const CONFIRMED_KEY = "od.allowConfirmed";

function confirmedIds() {
  try { return new Set(JSON.parse(globalThis.localStorage?.getItem(CONFIRMED_KEY) ?? "[]")); } catch { return new Set(); }
}
function rememberConfirmed(id) {
  try { globalThis.localStorage?.setItem(CONFIRMED_KEY, JSON.stringify([...confirmedIds(), id])); } catch { /* convenience only */ }
}
function silent() { const error = new Error(""); error.silent = true; return error; }

function confirmAllow(name, trigger) {
  const modal = createModal({ title: "Allow changes?" });
  modal.body.append(el("p", `Allow OpenDevEngine to push branches and open PRs in ${name}? You can turn this off any time.`));
  button(modal.footer, "Cancel", () => modal.close(false));
  button(modal.footer, "Allow", () => modal.close(true), { variant: "primary" });
  modal.open(trigger);
  return modal.closed;
}

export function skippedText(skipped = {}) {
  return Object.entries(skipped).filter(([, count]) => count > 0).map(([reason, count]) => `${count} ${SKIP_WORDS[reason] ?? reason.replaceAll("_", " ")}`).join(", ");
}

export function createRepositoriesPage({ host, request, panel, shell }) {
  const root = el("section", undefined, "repos-page");
  const top = el("div", undefined, "repos-top");
  const search = el("input");
  search.type = "search";
  search.placeholder = "Search repositories…";
  search.setAttribute("aria-label", "Search repositories");
  const chips = el("div", undefined, "filter-chips");
  const sync = el("span", undefined, "repos-sync");
  const filters = el("details",undefined,"repo-filters"); filters.append(el("summary","More filters"));
  const owner = el("input"); owner.setAttribute("aria-label","Exact repository owner"); owner.placeholder="Owner";
  const language = el("input"); language.setAttribute("aria-label","Repository language"); language.placeholder="Language";
  const selectFilter = (label,entries) => { const wrap=el("label",label), select=el("select"); for(const [value,text] of entries){const opt=el("option",text);opt.value=value;select.append(opt);}select.value='';wrap.append(select);filters.append(wrap);return select; };
  const archived=selectFilter("Archive state",[['','All'],['true','Archived'],['false','Active']]);
  const fork=selectFilter("Fork state",[['','All'],['true','Forks'],['false','Originals']]);
  filters.append(owner,language,el("p","These filters apply to all loaded catalog pages.","detail-muted"));
  top.append(search, chips, filters, sync);
  const banner = el("div", undefined, "repos-banner");
  const status = el("p", "", "page-status");
  status.setAttribute("role", "status");
  const notice = el("p", "", "repos-notice");
  notice.setAttribute("aria-live", "polite");
  const table = el("table", undefined, "repos-table");
  const more = el("p", "", "detail-muted");
  // The table scrolls inside its own box on narrow screens; the page never scrolls sideways.
  const tableBox = el("div", undefined, "repos-table-box");
  tableBox.setAttribute("tabindex", "0");
  tableBox.setAttribute("role", "region");
  tableBox.setAttribute("aria-label", "Repository catalog");
  tableBox.append(table);
  root.append(top, banner, notice, status, tableBox, more);
  host.replaceChildren(root);

  let filter = "All";
  let rows = [];
  let pendingSaves = 0;
  let embeddingVersionId = null;
  let timer = null;
  let sequence = 0;

  for (const name of FILTERS) {
    const chip = button(chips, name, () => { filter = name; draw(); }, { variant: "ghost" });
    chip.classList.add("filter-chip");
  }
  for(const control of [owner,language,archived,fork]){control.addEventListener("input",draw);control.addEventListener("change",draw);}
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => refresh(), 200); });

  const matches = (row) => filter === "All"
    || (filter === "Used in Ask" && row.inAsk)
    || (filter === "Changes allowed" && row.canChange)
    || (filter === "Needs attention" && (row.status.tone === "danger" || row.status.label === "No access" || row.status.label.startsWith("Waiting")));

  // Saves on one row run in order; each sends the row's latest intended state so switches never undo each other.
  function save(row, patch) {
    row.target = { index_enabled: row.inAsk, implementation_enabled: row.canChange, ...(row.target ?? {}), ...patch };
    pendingSaves += 1;
    const desired = Object.freeze({ ...row.target });
    const intent = row.intent = (row.intent || 0) + 1;
    const run = (row.queue ?? Promise.resolve()).catch(() => {}).then(() => saveNow(row, patch, desired, intent));
    row.queue = run;
    return run;
  }

  async function saveNow(row, patch, desired, intent) {
    notice.textContent = "";
    let unknown = false;
    try {
      if (row.github) {
        const body = { ...desired };
        const saved = await request(`/api/repositories/${enc(row.id)}`, "PATCH", body);
        row.inAsk = typeof saved?.index_enabled === "boolean" ? saved.index_enabled : body.index_enabled;
        row.canChange = typeof saved?.implementation_enabled === "boolean" ? saved.implementation_enabled : body.implementation_enabled;
        row.controls?.ask.set(row.inAsk); row.controls?.change?.set(row.canChange);
      } else {
        await request(`/api/repositories/${enc(row.id)}/context`, "PATCH", { index_enabled: patch.index_enabled });
        row.inAsk = patch.index_enabled;
      }
    } catch (error) {
      if (row.intent === intent) row.target = { index_enabled: row.inAsk, implementation_enabled: row.canChange };
      // A lost response may still have been applied: say so and show what the server has.
      unknown = error.status === undefined;
      if (unknown) notice.textContent = error.message;
      throw unknown ? Object.assign(new Error(""), { silent: true }) : error;
    } finally {
      pendingSaves -= 1;
      if (unknown) refresh();
    }
  }

  function openDetails(row, message = "") {
    panel.openTab({ id: `repo:${row.id}`, title: row.name, render: (body) => {
      const c = row.context;
      const snapshot = c?.active_snapshot;
      const items = [el("h3", row.fullName, "detail-title"),
        el("p", [row.fullName, row.meta?.visibility, snapshot ? `${snapshot.branch ?? row.meta?.default_branch ?? "default"} @ ${String(snapshot.revision).slice(0, 7)}` : null].filter(Boolean).join(" · "), "detail-meta")];
      if (snapshot?.coverage) {
        const skipped = snapshot.coverage.skipped ?? {};
        const total = Object.values(skipped).reduce((sum, n) => sum + n, 0);
        items.push(el("p", `${snapshot.coverage.files ?? 0} files indexed · ${total} skipped`));
        if (total) items.push(el("p", skippedText(skipped), "detail-muted"));
      } else {
        items.push(el("p", "Not indexed yet.", "detail-muted"));
      }
      if (row.github) {
        const link = el("a", "↗ View on GitHub", "detail-link");
        link.setAttribute("href", `https://github.com/${row.fullName}`);
        link.setAttribute("rel", "noreferrer");
        link.setAttribute("target", "_blank");
        items.push(link);
      }
      body.replaceChildren(...items);
      detailControls(body, row, message);
    } });
  }

  // Index, ignored paths and local-index deletion for one repository. One change runs at a time.
  function detailControls(body, row, message) {
    const c = row.context, id = enc(row.id), output = el("p", message, "page-status");
    output.setAttribute("role", "status");
    let busy = false;
    async function change(control, operation, done) {
      if (busy) { output.textContent = "Another change for this repository is still saving."; return; }
      busy = true; control.disabled = true; output.textContent = "Saving…";
      try {
        await operation();
        const fresh = await request(`/api/repositories/${id}/context`);
        if (fresh?.repository_id === row.id) { row.context = fresh; row.status = repoStatus(fresh, row.meta); }
        if (body.isConnected) openDetails(row, done); else output.textContent = done;
        refresh();
      } catch (error) {
        output.textContent = error.status === undefined ? `${error.message} The outcome is unknown; the saved state is reloading.` : error.message;
        if (error.status === undefined) refresh();
      } finally { busy = false; if (control.isConnected) control.disabled = false; }
    }
    if (!c) { body.append(el("p", "Turn on Use in Ask to index this repository.", "detail-muted")); return; }

    const index = el("section", undefined, "detail-section");
    index.append(el("h4", "Index"));
    const run = c.active_run;
    if (run) index.append(el("p", `Indexing · ${String(run.phase ?? run.state).replaceAll("_", " ")} · ${run.progress?.files ?? 0} files${run.retry_at ? ` · retry ${relativeTime(run.retry_at)}` : ""}${run.error_code ? ` · ${run.error_code.replaceAll("_", " ")}` : ""}`));
    if (run?.error_code === "disk_limit") index.append(el("p", "Indexing paused because the local index reached its size limit or the disk has less than 1 GB free. Free some disk space or use Delete local index on repositories you no longer need, then resume.", "detail-muted"));
    const actions = el("div", undefined, "detail-actions"); index.append(actions);
    const reindex = button(actions, c.active_snapshot ? "Reindex now" : "Index now", (event, control) => change(control,
      () => request(`/api/repositories/${id}/index`, "POST", embeddingVersionId ? { embedding_version_id: embeddingVersionId } : {}), "Indexing started. The current snapshot stays usable until the new one is ready."));
    reindex.disabled = !c.eligible || Boolean(run);
    if (!c.eligible) index.append(el("p", "Turn on Use in Ask to index this repository.", "detail-muted"));
    if (run) {
      const resume = run.state === "paused";
      button(actions, resume ? "Resume" : "Pause", (event, control) => change(control,
        () => request(`/api/index-jobs/${enc(run.id)}/${resume ? "resume" : "pause"}`, "POST", {}), resume ? "Indexing resumed." : "Indexing paused."));
    }

    const ignored = el("section", undefined, "detail-section");
    ignored.append(el("h4", "Ignored paths"), el("p", "Ignored paths are left out of search and also hide earlier citations from them.", "detail-muted"));
    const chips = el("div", undefined, "chip-row"), adder = el("div"); ignored.append(chips, adder);
    chips.append(el("span", "Loading…", "detail-muted"));
    const save = (control, next, done) => change(control, () => request(`/api/repositories/${id}/exclusions`, "PATCH", { exclusions: next }), done);
    request(`/api/repositories/${id}/exclusions`).then((settings) => {
      if (!body.isConnected) return;
      if (!Array.isArray(settings?.exclusions)) throw new Error("Ignored paths are unavailable.");
      const list = settings.exclusions;
      chips.replaceChildren();
      for (const pattern of list) {
        const chip = el("span", undefined, "chip");
        chip.append(el("span", pattern, "chip-label"));
        const remove = button(chip, "×", (event, control) => save(control, list.filter((p) => p !== pattern), `Stopped ignoring ${pattern}.`), { variant: "ghost" });
        remove.setAttribute("aria-label", `Stop ignoring ${pattern}`);
        chips.append(chip);
      }
      if (!list.length) chips.append(el("span", "Nothing ignored.", "detail-muted"));
      const add = button(chips, "＋ Add", () => {
        const form = el("form", undefined, "detail-add");
        const label = el("label", "Path or glob to ignore");
        const field = el("input"); field.placeholder = "dist/**"; label.append(field);
        const submit = el("button", "Ignore", "btn btn-primary"); submit.type = "submit";
        form.append(label, submit);
        button(form, "Cancel", () => { adder.replaceChildren(); add.hidden = false; add.focus(); }, { variant: "ghost" });
        form.addEventListener("submit", (event) => {
          event.preventDefault();
          const value = field.value.trim();
          if (!value) { output.textContent = "Enter a path or glob."; return; }
          if (list.includes(value)) { output.textContent = `${value} is already ignored.`; return; }
          save(submit, [...list, value], `Now ignoring ${value}.`);
        });
        add.hidden = true; adder.replaceChildren(form); field.focus();
      }, { variant: "ghost" });
    }).catch((error) => { if (body.isConnected) chips.replaceChildren(el("span", `Ignored paths unavailable: ${error.message}`, "detail-muted")); });

    const danger = el("section", undefined, "detail-section danger-zone");
    danger.append(el("h4", "Danger zone"));
    button(danger, "Delete local index…", async (event, control) => {
      const modal = createModal({ title: "Delete local index?" });
      modal.body.append(el("p", `This deletes the cached source, summaries and search vectors for ${row.fullName} on this computer. Earlier citations to it become unavailable. GitHub is not changed.`));
      button(modal.footer, "Cancel", () => modal.close(false));
      button(modal.footer, "Delete local index", () => modal.close(true), { variant: "danger" });
      modal.open(control);
      if (await modal.closed) change(control, () => request(`/api/repositories/${id}/purge`, "POST", {}), "Local index deleted.");
    });
    body.append(index, ignored, danger, output);
  }

  function draw() {
    for (const chip of chips.querySelectorAll("button")) chip.setAttribute("aria-pressed", String(chip.textContent === filter));
    const head = el("thead");
    const headRow = el("tr");
    for (const label of ["Repository", "Status", "Use in Ask", "Allow changes"]) headRow.append(el("th", label));
    head.append(headRow);
    const bodyRows = el("tbody");
    const visible = rows.filter(row=>matches(row)
      && (!owner.value.trim() || (row.meta?.owner || row.fullName.split('/')[0]).toLowerCase()===owner.value.trim().toLowerCase())
      && (!language.value.trim() || (row.meta?.language || '').toLowerCase()===language.value.trim().toLowerCase())
      && (!archived.value || String(row.meta?.archived)===archived.value)
      && (!fork.value || String(row.meta?.fork)===fork.value));
    for (const row of visible) {
      const tr = el("tr");
      const name = el("th");
      name.setAttribute("scope", "row");
      name.dataset.label = "Repository";
      button(name, row.name, () => openDetails(row), { variant: "ghost" }).classList.add("repo-name");
      name.append(el("small", [row.meta?.language, row.meta?.pushed_at ? `pushed ${relativeTime(Date.parse(row.meta.pushed_at))}` : null].filter(Boolean).join(" · ")));
      const statusCell = el("td");
      statusCell.dataset.label = "Status";
      statusCell.append(badge(row.status.tone, row.status.label));
      const askCell = el("td");
      askCell.dataset.label = "Used in Ask";
      const ask = toggleSwitch({ label: `Use in Ask for ${row.name}`, checked: row.inAsk, onChange: (next) => save(row, { index_enabled: next }) });
      row.controls = { ask }; askCell.append(ask.root);
      const changeCell = el("td");
      changeCell.dataset.label = "Changes allowed";
      if (row.github) {
        const allow = toggleSwitch({ label: `Allow changes in ${row.name}`, checked: row.canChange, onChange: async (next) => {
          if (next && !confirmedIds().has(row.id)) {
            const ok = await confirmAllow(row.name, changeCell.querySelector('[role="switch"]'));
            if (!ok) throw silent();
            rememberConfirmed(row.id);
          }
          await save(row, { implementation_enabled: next });
        } });
        row.controls.change = allow; changeCell.append(allow.root);
      } else {
        changeCell.append(el("span", "—", "detail-muted"));
      }
      tr.append(name, statusCell, askCell, changeCell);
      bodyRows.append(tr);
    }
    table.replaceChildren(head, bodyRows);
    status.replaceChildren();
    if (!visible.length && rows.length) status.append("No repositories match these filters.");
    else if (!rows.length) {
      status.append("No repositories yet. ");
      const connect = el("a", "Connect GitHub in Settings");
      connect.setAttribute("href", "#/settings/github");
      status.append(connect, ".");
    }
  }

  async function refresh() {
    if (pendingSaves) return;
    const token = ++sequence;
    const query = search.value.trim();
    const q = query ? `&query=${enc(query)}` : "";
    try {
      const [catalog, context, roles, connections] = await Promise.all([
        readRepositoryPages(request, `/api/repositories?limit=100${q}`).catch(error => { if(error.status===404)return {items:[],pages:0}; throw error; }),
        readRepositoryPages(request, `/api/context/repositories?limit=100${q}`),
        request("/api/settings/model-roles").catch(() => ({})),
        request("/api/github/connections").catch(() => ({ connections: [] })),
      ]);
      if (token !== sequence || pendingSaves) return;
      const contexts = new Map((context.items || []).map((item) => [item.repository_id, item]));
      const github = (catalog.items || []).map((meta) => {
        const c = contexts.get(meta.id);
        contexts.delete(meta.id);
        return { id: meta.id, name: meta.name, fullName: meta.full_name, github: true, meta, context: c, inAsk: meta.index_enabled, canChange: meta.implementation_enabled,
          status: c ? repoStatus(c, meta) : meta.access_state === "inaccessible" ? { tone: "off", label: "No access" } : { tone: "off", label: "Not indexed" } };
      });
      const others = [...contexts.values()].map((c) => ({ id: c.repository_id, name: c.name, fullName: c.name, github: false, meta: null, context: c,
        inAsk: c.eligible, canChange: false, status: repoStatus(c) }));
      rows = [...github, ...others];
      const pages = catalog.pages || context.pages || 1;
      more.textContent = `${rows.length} ${rows.length === 1 ? "repository" : "repositories"} loaded${pages > 1 ? ` from ${pages} pages` : ""}.`;
      const embedding = roles.roles?.embedding;
      embeddingVersionId = embedding?.version_id ?? null;
      const keywordOnly = rows.some((row) => row.context?.active_snapshot && (row.context.active_snapshot.mode ?? row.context.active_snapshot.coverage?.mode) === "lexical_only");
      banner.replaceChildren();
      if (keywordOnly && !embedding?.version_id) {
        banner.append("Answers use keyword search only. ");
        const link = el("a", "Turn on smarter search");
        link.setAttribute("href", "#/settings/providers");
        banner.append(link);
      }
      const connection = (connections.connections || []).find((item) => item.status === "connected");
      sync.replaceChildren();
      if (connection) {
        sync.append(el("span", connection.last_refresh_at ? `synced ${relativeTime(connection.last_refresh_at)}` : "not synced yet", "detail-muted"));
        button(sync, "↻ Sync", async (event, control) => {
          control.disabled = true;
          try { await request("/api/github/refresh", "POST", { connection_id: connection.id }); status.textContent = "Sync started."; }
          catch (error) { status.textContent = `Sync failed: ${error.message}`; }
          finally { control.disabled = false; }
        });
      }
      shell?.setHeader({ title: "Repositories", pills: [{ text: `${rows.length} repositories` }] });
      draw();
    } catch (error) {
      if (token === sequence) status.textContent = `Could not load repositories. ${error.message}`;
    }
  }

  return { element: root, refresh };
}
