// @ mentions: typing "@name" in a textarea offers matching repositories inline.
import { el } from "./ui.js";
import { searchRepositories } from "./repo-source.js";

const TRIGGER = /(^|\s)@([^\s@]{0,40})$/;
let instances = 0;

export function attachMentions(textarea, { request, onPick, exclude = () => [] }) {
  const prefix = `mention-${++instances}`;
  const menu = el("ul", undefined, "mention-menu");
  menu.id = `${prefix}-list`;
  menu.setAttribute("role", "listbox");
  menu.setAttribute("aria-label", "Repositories");
  menu.hidden = true;
  textarea.parentNode.append(menu);
  textarea.setAttribute("aria-controls", menu.id);
  textarea.setAttribute("aria-autocomplete", "list");
  textarea.setAttribute("aria-haspopup", "listbox");
  textarea.setAttribute("role", "combobox");
  textarea.setAttribute("aria-expanded", "false");

  let options = [];
  let active = 0;
  let mention = null; // {start, end}
  let timer = null;
  let sequence = 0;

  const caret = () => (Number.isInteger(textarea.selectionStart) ? textarea.selectionStart : textarea.value.length);

  function close() {
    clearTimeout(timer);
    sequence += 1;
    menu.hidden = true;
    menu.replaceChildren();
    options = [];
    mention = null;
    textarea.removeAttribute("aria-activedescendant");
    textarea.setAttribute("aria-expanded", "false");
  }

  function highlight(index) {
    active = index;
    menu.querySelectorAll('[role="option"]').forEach((item, i) => item.setAttribute("aria-selected", String(i === index)));
    if (options[index]) textarea.setAttribute("aria-activedescendant", `${prefix}-opt-${index}`);
  }

  function pick(index) {
    const repo = options[index];
    if (!repo || !mention) return;
    const { start, end } = mention;
    textarea.value = textarea.value.slice(0, start) + textarea.value.slice(end);
    if (typeof textarea.setSelectionRange === "function") textarea.setSelectionRange(start, start);
    close();
    onPick({ id: repo.id, name: repo.name });
  }

  function render(items) {
    const taken = new Set(exclude());
    options = items.filter((item) => item.selectable && !taken.has(item.id)).slice(0, 8);
    menu.replaceChildren();
    if (!options.length) {
      const empty = el("li", "No indexed repositories match. Use + to see all.", "mention-empty");
      empty.setAttribute("aria-disabled", "true");
      menu.append(empty);
      menu.hidden = false;
      textarea.setAttribute("aria-expanded", "true");
      textarea.removeAttribute("aria-activedescendant");
      return;
    }
    options.forEach((repo, i) => {
      const item = el("li", undefined, "mention-option");
      item.id = `${prefix}-opt-${i}`;
      item.setAttribute("role", "option");
      item.append(el("strong", repo.name), el("small", ` · ${repo.status.label}`));
      item.addEventListener("mousedown", (event) => event.preventDefault());
      item.addEventListener("click", () => pick(i));
      menu.append(item);
    });
    menu.hidden = false;
    textarea.setAttribute("aria-expanded", "true");
    highlight(0);
  }

  function onInput() {
    const end = caret();
    const match = TRIGGER.exec(textarea.value.slice(0, end));
    if (!match) return close();
    mention = { start: end - match[2].length - 1, end };
    const query = match[2];
    clearTimeout(timer);
    const token = ++sequence;
    timer = setTimeout(async () => {
      try {
        const page = await searchRepositories(request, { query, limit: 50 });
        if (token === sequence && mention) render(page.items);
      } catch {
        if (token === sequence) close();
      }
    }, 150);
  }

  function onKeydown(event) {
    if (menu.hidden) return;
    if (!options.length) {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
      return;
    }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      highlight((active + step + options.length) % options.length);
    } else if (event.key === "Enter" || event.key === "Tab") {
      event.preventDefault();
      event.stopPropagation();
      pick(active);
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close();
    }
  }

  textarea.addEventListener("input", onInput);
  textarea.addEventListener("keydown", onKeydown);
  textarea.addEventListener("blur", () => setTimeout(close, 150));
  return {
    detach() {
      close();
      textarea.removeEventListener("input", onInput);
      textarea.removeEventListener("keydown", onKeydown);
      menu.remove();
    },
  };
}
