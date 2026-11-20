// Repository picker modal opened from the composer's + button.
import { badge, button, createModal, el } from "./ui.js";
import { recentRepositoryIds, searchRepositories } from "./repo-source.js";

const enc = encodeURIComponent;

// selected: Array<string | {id, name}>; resolves Array<{id, name}> or null when dismissed.
export function openRepoPicker({ request, selected = [], trigger }) {
  const modal = createModal({ title: "Choose repositories" });
  const initial = new Map(selected.map((item) => typeof item === "string" ? [item, item] : [item.id, item.name]));
  const chosen = new Map(initial);
  const search = el("input");
  search.type = "search";
  search.placeholder = "Search repositories…";
  search.setAttribute("aria-label", "Search repositories");
  search.autocomplete = "off";
  const list = el("div", undefined, "picker-list");
  list.setAttribute("role", "group");
  list.setAttribute("aria-label", "Repositories");
  const status = el("p", "", "picker-status");
  status.setAttribute("aria-live", "polite");
  modal.body.append(search, list, status);
  const count = el("span", "", "picker-count");
  const more = button(null, "Load more", () => load(true));
  more.hidden = true;
  const cancel = button(null, "Cancel", () => modal.close(null));
  const add = button(null, "Add 0", () => confirm(), { variant: "primary" });
  modal.footer.append(count, more, cancel, add);

  let items = [];
  let cursor = null;
  let sequence = 0;
  let timer = null;

  const changed = () => chosen.size !== initial.size || [...chosen.keys()].some((id) => !initial.has(id));
  function sync() {
    count.textContent = `${chosen.size} selected`;
    add.textContent = `Add ${chosen.size}`;
    add.disabled = !changed();
  }
  function confirm() {
    if (!changed()) return;
    modal.close([...chosen].map(([id, name]) => ({ id, name })));
  }

  function row(item) {
    const wrap = el("label", undefined, "picker-row");
    const box = el("input");
    box.type = "checkbox";
    box.checked = chosen.has(item.id);
    box.disabled = !item.selectable;
    box.addEventListener("change", () => {
      if (box.checked) chosen.set(item.id, item.name);
      else chosen.delete(item.id);
      sync();
    });
    const text = el("span", undefined, "picker-name");
    text.append(el("strong", item.name), el("small", [item.owner, item.language].filter(Boolean).join(" · ")));
    wrap.append(box, text, badge(item.status.tone, item.status.label));
    if (item.status.label === "Not indexed") {
      button(wrap, "Index now", async (event) => {
        event.preventDefault();
        try {
          await request(`/api/repositories/${enc(item.id)}/index`, "POST", {});
          status.textContent = `Indexing ${item.name} started.`;
          load(false);
        } catch (error) {
          status.textContent = `Could not start indexing: ${error.message}`;
        }
      });
    }
    return wrap;
  }

  function render() {
    list.replaceChildren();
    const query = search.value.trim();
    if (!items.length) list.append(el("p", query ? `No repositories match "${query}".` : "No repositories match. Connect GitHub or index a repository first.", "picker-empty"));
    if (query) {
      for (const item of items) list.append(row(item));
      return;
    }
    const recentIds = recentRepositoryIds();
    const recent = items.filter((item) => recentIds.includes(item.id));
    const rest = items.filter((item) => !recentIds.includes(item.id));
    if (recent.length) {
      list.append(el("h3", "Recently used", "picker-group"));
      for (const item of recent) list.append(row(item));
    }
    if (rest.length) {
      list.append(el("h3", `All repositories · ${rest.length}`, "picker-group"));
      for (const item of rest) list.append(row(item));
    }
  }

  async function load(append) {
    const token = ++sequence;
    status.textContent = append ? "Loading more…" : "";
    try {
      const page = await searchRepositories(request, { query: search.value.trim(), cursor: append ? cursor : null });
      if (token !== sequence) return;
      items = append ? [...items, ...page.items] : page.items;
      cursor = page.nextCursor;
      more.hidden = !cursor;
      status.textContent = "";
      render();
    } catch (error) {
      if (token === sequence) status.textContent = `Could not load repositories: ${error.message}`;
    }
  }

  search.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => load(false), 200);
  });
  modal.root.addEventListener("keydown", (event) => {
    const boxes = [...list.querySelectorAll('input[type="checkbox"]')].filter((box) => !box.disabled);
    const index = boxes.indexOf(document.activeElement);
    if (event.key === "ArrowDown" && boxes.length) {
      event.preventDefault();
      boxes[Math.min(boxes.length - 1, index + 1)].focus();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (index <= 0) search.focus();
      else boxes[index - 1].focus();
    }
  });

  sync();
  modal.open(trigger);
  search.focus();
  load(false);
  modal.closed.then(() => clearTimeout(timer));
  return modal.closed;
}
