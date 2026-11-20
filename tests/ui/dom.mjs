// Minimal, dependency-free browser stand-in for running the real
// opendevengine/web/index.html + app.js under Node's test runner.
//
// It models only what the app uses: element tree, attributes, text, events
// with bubbling, focus, hash navigation, <dialog>, forms and fetch. It does not
// model layout, CSS, real tab order or assistive technology; those need a
// real browser.

const VOID = new Set(["meta", "link", "input", "br", "img", "hr", "source", "area", "base", "col", "wbr"]);
const RAW_TEXT = new Set(["script", "style"]);

function decode(text) {
  return text
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'").replace(/&amp;/g, "&");
}

class FakeEvent {
  constructor(type, init = {}) {
    this.type = type;
    this.bubbles = Boolean(init.bubbles);
    this.cancelable = Boolean(init.cancelable);
    this.defaultPrevented = false;
    this.target = null;
    this.currentTarget = null;
    Object.assign(this, init.props || {});
  }
  preventDefault() {
    if (this.cancelable) this.defaultPrevented = true;
  }
  stopPropagation() {
    this.stopped = true;
  }
}

class Listeners {
  constructor() {
    this.map = new Map();
  }
  addEventListener(type, fn) {
    if (!this.map.has(type)) this.map.set(type, []);
    this.map.get(type).push(fn);
  }
  removeEventListener(type, fn) {
    const list = this.map.get(type);
    if (list) this.map.set(type, list.filter((item) => item !== fn));
  }
  invoke(event, target) {
    event.currentTarget = target;
    for (const fn of [...(this.map.get(event.type) || [])]) fn.call(target, event);
  }
}

class FakeNode {
  constructor(doc) {
    this.ownerDocument = doc;
    this.parentNode = null;
    this.childNodes = [];
  }
  get isConnected() {
    let current = this;
    while (current.parentNode) current = current.parentNode;
    return current === this.ownerDocument;
  }
  get firstChild() {
    return this.childNodes[0] ?? null;
  }
  remove() {
    if (!this.parentNode) return;
    const siblings = this.parentNode.childNodes;
    siblings.splice(siblings.indexOf(this), 1);
    this.parentNode = null;
  }
}

class FakeText extends FakeNode {
  constructor(doc, data) {
    super(doc);
    this.nodeType = 3;
    this.data = String(data);
  }
  get textContent() {
    return this.data;
  }
  set textContent(value) {
    this.data = String(value);
  }
}

// Supports compound selectors (tag, *, #id, .class, [attr], [attr="v"]) joined
// by descendant (space) or child (>) combinators. Anything else throws.
function parseSelector(selector) {
  const chain = [];
  let combinator = " ";
  for (const compound of selector.trim().replace(/\s*>\s*/g, " > ").split(/\s+/)) {
    if (compound === ">") {
      combinator = ">";
      continue;
    }
    const parts = parseCompound(compound, selector);
    parts.combinator = chain.length ? combinator : null;
    chain.push(parts);
    combinator = " ";
  }
  return chain;
}

function parseCompound(compound, selector) {
  const parts = { tag: null, id: null, classes: [], attrs: [] };
  const body = compound.startsWith("*") ? compound.slice(1) : compound;
  const pattern = /([a-zA-Z][a-zA-Z0-9-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:="([^"]*)")?\]/g;
  let match;
  let consumed = 0;
  while ((match = pattern.exec(body))) {
    if (match.index !== consumed) break;
    consumed += match[0].length;
    if (match[1]) parts.tag = match[1].toUpperCase();
    else if (match[2]) parts.id = match[2];
    else if (match[3]) parts.classes.push(match[3]);
    else parts.attrs.push([match[4], match[5]]);
  }
  if (consumed !== body.length) throw new Error(`Unsupported selector in DOM shim: ${selector}`);
  return parts;
}

function matchesCompound(element, parts) {
  if (parts.tag && element.tagName !== parts.tag) return false;
  if (parts.id && element.id !== parts.id) return false;
  if (parts.classes.some((name) => !element.classList.contains(name))) return false;
  return parts.attrs.every(([name, value]) => element.hasAttribute(name) && (value === undefined || element.getAttribute(name) === value));
}

function matchesChain(element, chain, end = chain.length - 1) {
  if (!element || element.nodeType !== 1 || !matchesCompound(element, chain[end])) return false;
  if (end === 0) return true;
  if (chain[end].combinator === ">") return matchesChain(element.parentNode, chain, end - 1);
  for (let ancestor = element.parentNode; ancestor && ancestor.nodeType === 1; ancestor = ancestor.parentNode) {
    if (matchesChain(ancestor, chain, end - 1)) return true;
  }
  return false;
}

// Does `element` match a simple selector list (see parseSelector)?
export function matches(element, selector) {
  return selector.split(",").some((part) => matchesChain(element, parseSelector(part)));
}

class FakeElement extends FakeNode {
  constructor(doc, tag) {
    super(doc);
    this.nodeType = 1;
    this.tagName = tag.toUpperCase();
    this.attributes = new Map();
    this.listeners = new Listeners();
    this._value = null;
    this.checked = false;
    const element = this;
    this.classList = {
      list: () => (element.getAttribute("class") || "").split(/\s+/).filter(Boolean),
      contains: (name) => element.classList.list().includes(name),
      add: (...names) => element.setAttribute("class", [...new Set([...element.classList.list(), ...names])].join(" ")),
      remove: (...names) => element.setAttribute("class", element.classList.list().filter((n) => !names.includes(n)).join(" ")),
      toggle: (name, force) => {
        const on = force === undefined ? !element.classList.contains(name) : Boolean(force);
        if (on) element.classList.add(name);
        else element.classList.remove(name);
        return on;
      },
    };
    this.dataset = new Proxy({}, {
      get: (_, key) => element.getAttribute(`data-${String(key).replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`) ?? undefined,
      set: (_, key, value) => {
        element.setAttribute(`data-${String(key).replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`, value);
        return true;
      },
    });
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }
  removeAttribute(name) {
    this.attributes.delete(name);
  }
  hasAttribute(name) {
    return this.attributes.has(name);
  }

  get id() { return this.getAttribute("id") || ""; }
  set id(value) { this.setAttribute("id", value); }
  get className() { return this.getAttribute("class") || ""; }
  set className(value) { this.setAttribute("class", value); }
  get title() { return this.getAttribute("title") || ""; }
  set title(value) { this.setAttribute("title", value); }
  get type() {
    return this.getAttribute("type") || (this.tagName === "BUTTON" ? "submit" : this.tagName === "INPUT" ? "text" : "");
  }
  set type(value) { this.setAttribute("type", value); }
  get hidden() { return this.hasAttribute("hidden"); }
  set hidden(value) { if (value) this.setAttribute("hidden", ""); else this.removeAttribute("hidden"); }
  get disabled() { return this.hasAttribute("disabled"); }
  set disabled(value) {
    if (value) {
      this.setAttribute("disabled", "");
      if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = this.ownerDocument.body;
    } else this.removeAttribute("disabled");
  }
  get open() { return this.hasAttribute("open"); }
  get value() {
    if (this._value !== null) return this._value;
    return this.tagName === "TEXTAREA" ? this.textContent : this.getAttribute("value") || "";
  }
  set value(value) { this._value = String(value); }

  get children() { return this.childNodes.filter((child) => child.nodeType === 1); }
  get lastElementChild() { return this.children.at(-1) ?? null; }
  get childElementCount() { return this.children.length; }

  get textContent() { return this.childNodes.map((child) => child.textContent).join(""); }
  set textContent(value) {
    this.replaceChildren();
    if (value !== "" && value !== null && value !== undefined) this.append(String(value));
  }

  append(...items) {
    for (const item of items) {
      const child = typeof item === "object" && item !== null ? item : new FakeText(this.ownerDocument, item);
      child.remove();
      child.parentNode = this;
      this.childNodes.push(child);
    }
  }
  replaceChildren(...items) {
    for (const child of [...this.childNodes]) child.remove();
    this.append(...items);
  }

  *descendants() {
    for (const child of this.childNodes) {
      if (child.nodeType !== 1) continue;
      yield child;
      yield* child.descendants();
    }
  }
  querySelectorAll(selector) {
    const chains = selector.split(",").map(parseSelector);
    return [...this.descendants()].filter((element) => chains.some((chain) => matchesChain(element, chain)));
  }
  querySelector(selector) {
    return this.querySelectorAll(selector)[0] ?? null;
  }
  closest(selector) {
    const chain = parseSelector(selector);
    for (let current = this; current && current.nodeType === 1; current = current.parentNode) {
      if (matchesChain(current, chain)) return current;
    }
    return null;
  }

  addEventListener(type, fn) { this.listeners.addEventListener(type, fn); }
  removeEventListener(type, fn) { this.listeners.removeEventListener(type, fn); }
  dispatchEvent(event) {
    event.target = this;
    const path = [];
    for (let current = this; current; current = current.parentNode) path.push(current);
    for (const target of path) {
      target.listeners.invoke(event, target);
      if (!event.bubbles || event.stopped) break;
    }
    if (event.bubbles && !event.stopped && path.at(-1) === this.ownerDocument) {
      this.ownerDocument.defaultView.listeners.invoke(event, this.ownerDocument.defaultView);
    }
    return !event.defaultPrevented;
  }

  get inHiddenSubtree() {
    for (let current = this; current && current.nodeType === 1; current = current.parentNode) {
      if (current.hidden) return true;
    }
    return false;
  }
  get focusable() {
    if (!this.isConnected || this.inHiddenSubtree || this.disabled) return false;
    if (this.hasAttribute("tabindex")) return true;
    if (this.tagName === "A") return this.hasAttribute("href");
    return ["BUTTON", "INPUT", "TEXTAREA", "SELECT"].includes(this.tagName);
  }
  focus() {
    if (this.focusable) this.ownerDocument.activeElement = this;
  }
  blur() {
    if (this.ownerDocument.activeElement === this) this.ownerDocument.activeElement = this.ownerDocument.body;
  }
  scrollIntoView() {}

  click() {
    if (this.disabled || this.inHiddenSubtree) return;
    if (this.tagName === "INPUT" && this.type === "checkbox") this.checked = !this.checked;
    const proceed = this.dispatchEvent(new FakeEvent("click", { bubbles: true, cancelable: true }));
    if (this.tagName === "INPUT" && this.type === "checkbox") {
      this.dispatchEvent(new FakeEvent("change", { bubbles: true }));
      return;
    }
    if (!proceed) return;
    const link = this.closest("a[href]");
    if (link) {
      const href = link.getAttribute("href");
      if (href.startsWith("#")) this.ownerDocument.defaultView.location.hash = href;
      return;
    }
    if (this.tagName === "BUTTON" && this.type === "submit") this.closest("form")?.requestSubmit();
  }
  requestSubmit() {
    this.dispatchEvent(new FakeEvent("submit", { bubbles: true, cancelable: true }));
  }
  showModal() {
    this.setAttribute("open", "");
  }
  close() {
    if (!this.open) return;
    this.removeAttribute("open");
    this.dispatchEvent(new FakeEvent("close"));
  }
}

// HTMLTextAreaElement.type is a getter-only property in browsers. Keep this
// distinct from writable HTMLInputElement.type so field helpers cannot silently
// assign a made-up input type to a textarea.
class FakeTextAreaElement extends FakeElement {
  get type() { return "textarea"; }
}

class FakeDocument extends FakeElement {
  constructor() {
    super(null, "#document");
    this.ownerDocument = this;
    this.nodeType = 9;
    this.title = "";
    this.activeElement = null;
  }
  get isConnected() { return true; }
  get documentElement() { return this.children[0]; }
  get body() { return this.querySelector("body"); }
  get head() { return this.querySelector("head"); }
  getElementById(id) {
    for (const element of this.descendants()) if (element.id === id) return element;
    return null;
  }
  createElement(tag) {
    return tag.toLowerCase() === "textarea" ? new FakeTextAreaElement(this, tag) : new FakeElement(this, tag);
  }
  createTextNode(text) { return new FakeText(this, text); }
}

export function parseHTML(html) {
  const doc = new FakeDocument();
  const stack = [doc];
  const token = /<!--[\s\S]*?-->|<!doctype[^>]*>|<\/([a-zA-Z0-9]+)\s*>|<([a-zA-Z][a-zA-Z0-9]*)((?:\s+[^\s=>/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*\/?>|[^<]+/gi;
  let match;
  while ((match = token.exec(html))) {
    const top = stack.at(-1);
    if (match[1]) {
      const tag = match[1].toUpperCase();
      const index = stack.findLastIndex((item) => item.tagName === tag);
      if (index > 0) stack.length = index;
    } else if (match[2]) {
      const element = doc.createElement(match[2]);
      const attrs = /([^\s=>/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
      let attr;
      while ((attr = attrs.exec(match[3] || ""))) {
        element.setAttribute(attr[1].toLowerCase(), decode(attr[2] ?? attr[3] ?? attr[4] ?? ""));
      }
      top.append(element);
      const name = match[2].toLowerCase();
      if (RAW_TEXT.has(name)) {
        const end = html.toLowerCase().indexOf(`</${name}`, token.lastIndex);
        token.lastIndex = end === -1 ? html.length : end;
        stack.push(element);
      } else if (!VOID.has(name) && !match[0].endsWith("/>")) {
        stack.push(element);
      }
    } else if (!match[0].startsWith("<")) {
      if (stack.length > 1) top.append(doc.createTextNode(decode(match[0])));
    }
  }
  return doc;
}

// Text a sighted user could read: skips subtrees marked `hidden`.
export function visibleText(element) {
  if (element.nodeType === 3) return element.data;
  if (element.hidden) return "";
  return element.childNodes.map(visibleText).join("").replace(/\s+/g, " ");
}

// Build a fake browser around `html`. `respond(method, url, options)` returns
// {status, body}, a body object (status 200), a promise of either, or throws
// to simulate a network error. Promises allow controlled out-of-order responses.
export function createBrowser(html, { hash = "", respond }) {
  const doc = parseHTML(html);
  doc.activeElement = doc.body;
  const view = { listeners: new Listeners() };
  doc.defaultView = view;
  let currentHash = hash;
  const location = {
    get hash() { return currentHash; },
    set hash(value) {
      const next = value.startsWith("#") ? value : `#${value}`;
      if (next === currentHash) return;
      currentHash = next;
      setTimeout(() => view.listeners.invoke(new FakeEvent("hashchange"), view), 0);
    },
  };
  const requests = [];
  const fetch = async (url, options = {}) => {
    const method = (options.method || "GET").toUpperCase();
    requests.push({ method, url: String(url), body: options.body });
    if (options.signal?.aborted) throw new DOMException("Aborted", "AbortError");
    await new Promise((resolve) => setTimeout(resolve, 0));
    const result = await respond(method, String(url), options);
    const { status, body } = result && "status" in result && "body" in result ? result : { status: 200, body: result };
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => {
        if (typeof body === "string") return JSON.parse(body);
        return structuredClone(body);
      },
    };
  };
  Object.assign(view, {
    document: doc,
    location,
    history: { replaceState(_state, _title, url) { currentHash = url.startsWith("#") ? url : currentHash; } },
    addEventListener: (type, fn) => view.listeners.addEventListener(type, fn),
    removeEventListener: (type, fn) => view.listeners.removeEventListener(type, fn),
    matchMedia: () => ({ matches: false }),
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    fetch,
  });
  return { window: view, document: doc, requests };
}

// Expose a fake browser as the globals app.js expects.
export function install(browser) {
  const { window } = browser;
  for (const name of ["document", "location", "history", "matchMedia", "requestAnimationFrame", "fetch"]) {
    globalThis[name] = window[name];
  }
  globalThis.window = window;
}

export function keydown(target, key, props = {}) {
  const event = new FakeEvent("keydown", { bubbles: true, cancelable: true, props: { key, ...props } });
  target.dispatchEvent(event);
  return event;
}

export function input(target, value) {
  target.value = value;
  target.dispatchEvent(new FakeEvent("input", { bubbles: true }));
}

export async function settle(rounds = 12) {
  for (let index = 0; index < rounds; index += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}
