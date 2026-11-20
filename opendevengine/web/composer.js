// Chat composer: message box, repository chips (+ picker and @ mentions), send/cancel, drafts.
import { button, el } from "./ui.js";
import { openRepoPicker } from "./picker.js";
import { attachMentions } from "./mentions.js";

const drafts = () => globalThis.window?.sessionStorage ?? globalThis.sessionStorage;
function readDraft(key) {
  try { return drafts()?.getItem(key) ?? ""; } catch { return ""; }
}
function writeDraft(key, value) {
  try {
    if (value) drafts()?.setItem(key, value);
    else drafts()?.removeItem(key);
  } catch {
    // Drafts are a convenience; storage may be unavailable.
  }
}

let noticeIds = 0;

export function createComposer({ request, placeholder = "Ask about your code, or describe a change…", onSend, onScopeChange, onCancel, draftKey, chips: withChips = true,
  actions = null, label = null, initialText = null, onInput = () => {} }) {
  const root = el("form", undefined, "composer-v2");
  root.noValidate = true;
  const field = el(label ? "label" : "div", undefined, "composer-field");
  if (label) field.append(el("span", label, "sr-only"));
  const textarea = el("textarea");
  textarea.rows = 2;
  textarea.maxLength = 4000;
  textarea.setAttribute("placeholder", placeholder);
  textarea.setAttribute("aria-label", "Message");
  field.append(textarea);
  const row = el("div", undefined, "composer-row");
  const chipList = el("div", undefined, "composer-chips");
  const error = el("p", "", "composer-error");
  error.setAttribute("aria-live", "polite");
  const send = button(null, "↑", () => (busy ? cancel() : submit()), { variant: "primary" });
  send.classList.add("composer-send");
  send.setAttribute("aria-label", "Send");
  const actionButtons = (actions || []).map((action) => button(null, action.label, () => submit(action.intent), { variant: action.variant }));
  if (actions) {
    send.textContent = "Cancel";
    send.setAttribute("aria-label", "Cancel");
    send.classList.remove("btn-primary");
    send.hidden = true;
  }
  // Says why an action is unavailable (for example, no model assigned) before anyone sends.
  const notice = el("p", undefined, "composer-notice");
  notice.id = `composer-notice-${++noticeIds}`;
  notice.hidden = true;
  // Announces outcomes that another visible element already explains.
  const announcer = el("p", "", "sr-only");
  announcer.setAttribute("aria-live", "polite");
  root.append(field, row, notice, error, announcer);

  let chips = [];
  let busy = false;
  let sending = false;
  let plus = null;
  let unavailable = new Set();

  function refreshActions() {
    (actions || []).forEach((action, index) => {
      const control = actionButtons[index];
      control.disabled = busy || unavailable.has(action.intent);
      if (unavailable.has(action.intent)) control.setAttribute("aria-describedby", notice.id);
      else control.removeAttribute("aria-describedby");
    });
  }

  function renderChips() {
    chipList.replaceChildren();
    for (const chip of chips) {
      const item = el("span", undefined, "chip-repo");
      item.append(el("span", chip.name, "chip-label"));
      const remove = button(item, "×", () => change(chips.filter((other) => other.id !== chip.id)), { variant: "ghost" });
      remove.setAttribute("aria-label", `Remove ${chip.name}`);
      chipList.append(item);
    }
  }

  async function change(next) {
    const previous = chips;
    chips = next;
    renderChips();
    error.textContent = "";
    try {
      await onScopeChange(next.map((chip) => chip.id));
    } catch (failure) {
      chips = previous;
      renderChips();
      error.textContent = failure?.message || "Could not update repositories.";
    }
  }

  function addRepositories(repos) {
    const merged = [...chips];
    for (const repo of repos) if (!merged.some((chip) => chip.id === repo.id)) merged.push(repo);
    if (merged.length !== chips.length) change(merged);
  }

  if (withChips) {
    plus = button(null, "+", async () => {
      const result = await openRepoPicker({ request, selected: chips, trigger: plus });
      if (result === null) return;
      const keep = new Set(result.map((repo) => repo.id));
      const same = keep.size === chips.length && chips.every((chip) => keep.has(chip.id));
      if (!same) change([...chips.filter((chip) => keep.has(chip.id)), ...result.filter((repo) => !chips.some((chip) => chip.id === repo.id))]);
    }, { variant: "ghost" });
    plus.classList.add("composer-plus");
    plus.setAttribute("aria-label", "Add repositories");
    plus.setAttribute("title", "Add repositories (or type @)");
    attachMentions(textarea, { request, onPick: (repo) => addRepositories([repo]), exclude: () => chips.map((chip) => chip.id) });
    row.append(plus, chipList, ...actionButtons, send);
  } else {
    row.append(el("span", undefined, "composer-spacer"), ...actionButtons, send);
  }

  async function submit(intent = actions ? actions[0].intent : undefined) {
    const text = textarea.value.trim();
    if (!text || busy || sending || unavailable.has(intent)) return;
    sending = true;
    error.textContent = "";
    announcer.textContent = "";
    try {
      const payload = { text, repositoryIds: chips.map((chip) => chip.id) };
      if (intent !== undefined) payload.intent = intent;
      await onSend(payload);
      // Keep anything typed while sending; only the sent text is cleared.
      if (textarea.value.trim() === text) {
        textarea.value = "";
        writeDraft(draftKey, "");
      }
    } catch (failure) {
      if (failure?.silent) announcer.textContent = failure.message;
      else error.textContent = failure?.message || "Could not send.";
    } finally {
      sending = false;
    }
  }
  async function cancel() {
    try { await onCancel(); } catch (failure) { error.textContent = failure?.message || "Could not cancel."; }
  }

  textarea.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.defaultPrevented) return;
    event.preventDefault();
    submit();
  });
  textarea.addEventListener("input", () => { writeDraft(draftKey, textarea.value); onInput(textarea.value); });
  root.addEventListener("submit", (event) => { event.preventDefault(); submit(); });
  textarea.value = initialText ?? readDraft(draftKey);

  return {
    root,
    setChips(next) { chips = [...next]; renderChips(); },
    setBusy(next) {
      busy = Boolean(next);
      refreshActions();
      if (actions) {
        send.hidden = !busy;
        return;
      }
      send.textContent = busy ? "Cancel" : "↑";
      send.setAttribute("aria-label", busy ? "Cancel" : "Send");
      send.classList.toggle("btn-primary", !busy);
    },
    // intents: action intents to disable; message and optional link explain why. Empty list clears it.
    setUnavailable(intents, { message = "", link = null } = {}) {
      unavailable = new Set(intents);
      notice.hidden = unavailable.size === 0;
      notice.replaceChildren();
      if (unavailable.size) {
        notice.append(message);
        if (link) {
          const a = el("a", link.text);
          a.setAttribute("href", link.href);
          notice.append(" ", a);
        }
      }
      refreshActions();
    },
    announce(text) { announcer.textContent = text; },
    focus() { textarea.focus(); },
    get value() { return textarea.value; },
    setValue(text) { textarea.value = text; writeDraft(draftKey, text); },
  };
}
