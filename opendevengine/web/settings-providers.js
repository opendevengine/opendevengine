// Settings → AI providers: the Claude, Codex and Local providers you have, then which one does
// each job. Coding setup (container, limits, test run) happens on the server when you pick it.
import { button, el } from "./ui.js";

const KINDS = [["claude", "Claude"], ["codex", "Codex (OpenAI)"], ["local", "Local models"]];
const COMPANY = { claude: "Anthropic", codex: "OpenAI" };
const JOBS = [["plans", "Plans & answers"], ["coding", "Coding"], ["search", "Code search"]];
const ADVANCED_JOBS = [["summary", "Repository summaries"]];
// Model names prefilled when you pick a provider; you can type any other.
const DEFAULT_MODEL = { claude: { api_key: "claude-sonnet-5-5", cli_login: "sonnet" }, codex: "gpt-5-codex", local: "" };
const CODING_MODEL = "sonnet";
const STATUS = {
  ready: "✓ ready", passed: "✓ tested", untested: "Not tested yet", queued: "Testing…", running: "Testing…",
  failed: "Test failed. Check the key or model, then test again.", interrupted: "Test interrupted. Test again.",
  blocked: "Blocked by privacy setting", unassigned: "Not set", credential_unavailable: "Key unavailable. Replace the key.",
  unsupported: "This model can't do this job.",
};
const LOCAL_NOTE = "Use localhost or a private LAN/VPN IP address. Treated as your own machine: code is sent here without asking.";

const PROVIDER_ERRORS = {
  local_endpoint_required: "Use localhost or a private LAN/VPN IP address, not a public IP or hostname.",
  coding_check_in_progress: "Wait for the current coding test to finish before changing or removing this provider.",
  execution_test_reconciliation_required: "Resolve the previous coding test under Advanced before continuing.",
};

function labelled(tag, label, type) {
  const control = el(tag);
  if (type) control.type = type;
  control.setAttribute("aria-label", label);
  const wrap = el("label", label, "modal-field");
  wrap.append(control);
  return { wrap, control };
}

function option(value, text, { disabled = false } = {}) {
  const node = el("option", text);
  node.value = value;
  if (disabled) node.disabled = true;
  return node;
}

function describe(provider) {
  if (provider.kind === "claude") return provider.auth === "api_key" ? "API key saved" : "Uses your claude login";
  return [provider.endpoint, provider.has_key ? "API key saved" : null].filter(Boolean).join(" · ");
}

function defaultModel(job, provider) {
  if (!provider) return "";
  if (job === "coding") return CODING_MODEL;
  const value = DEFAULT_MODEL[provider.kind];
  return typeof value === "object" ? value[provider.auth] ?? "" : value;
}

export function createProvidersSection({ request, advanced = () => ({}), pollMs = 2000, isVisible = () => true }) {
  const element = el("div", undefined, "providers-section");
  const list = el("div");
  const formHost = el("div");
  const jobsHost = el("div");
  const message = el("p", "", "composer-error");
  message.setAttribute("aria-live", "polite");
  let providers = [];
  let jobs = {};
  let timer = null;

  const providerCard = el("section", undefined, "setting-card");
  const jobsCard = el("section", undefined, "setting-card");
  providerCard.append(el("h2", "AI providers"), el("p", "Add the providers you use, then choose which one does each job.", "detail-muted"), list, formHost);
  jobsCard.append(el("h2", "What each job uses"), jobsHost);
  const more = el("details", undefined, "existing-app");
  element.append(providerCard, jobsCard, message, more);

  const fail = (error) => {
    message.textContent = Object.hasOwn(PROVIDER_ERRORS, error?.code)
      ? PROVIDER_ERRORS[error.code]
      : (error?.message ?? String(error));
  };

  function renderProviders() {
    list.replaceChildren();
    for (const provider of providers) {
      const row = el("div", undefined, "source-row");
      row.append(el("strong", provider.name), el("span", ` ${describe(provider)} `, "detail-muted"));
      if (provider.auth === "api_key") button(row, "Replace key", () => replaceKey(provider, row));
      button(row, "Remove", async () => {
        try { await request(`/api/ai-providers/${encodeURIComponent(provider.id)}`, "DELETE"); await refresh(); } catch (error) { fail(error); }
      });
      list.append(row);
    }
    if (!providers.length) list.append(el("p", "No providers yet.", "detail-muted"));
    formHost.replaceChildren();
    button(formHost, "Add provider", () => openForm());
  }

  function replaceKey(provider, row) {
    const key = labelled("input", "New API key", "password");
    row.append(key.wrap);
    button(row, "Save key", async () => {
      const value = key.control.value;
      key.control.value = "";
      try { await request(`/api/ai-providers/${encodeURIComponent(provider.id)}`, "PATCH", { api_key: value }); await refresh(); } catch (error) { fail(error); }
    });
  }

  function openForm() {
    const form = el("div", undefined, "provider-form");
    const kind = labelled("select", "Provider");
    for (const [value, text] of KINDS) kind.control.append(option(value, text));
    kind.control.value = "claude";
    const fields = el("div");
    const note = el("p", "", "modal-note");
    form.append(kind.wrap, fields, note);
    let current = {};

    function build() {
      const value = kind.control.value;
      fields.replaceChildren();
      note.textContent = "";
      current = { kind: value };
      if (value === "claude") {
        const how = labelled("select", "Sign in with");
        how.control.append(option("api_key", "An Anthropic API key"), option("login", "My claude login (Pro or Max)"));
        how.control.value = "api_key";
        fields.append(how.wrap);
        const keyHost = el("div");
        fields.append(keyHost);
        const showKey = () => {
          keyHost.replaceChildren();
          current.key = null;
          if (how.control.value !== "login") { current.key = labelled("input", "API key", "password"); keyHost.append(current.key.wrap); }
        };
        how.control.addEventListener("change", showKey);
        current.how = how.control;
        showKey();
      } else if (value === "codex") {
        current.key = labelled("input", "API key", "password");
        current.url = labelled("input", "API URL");
        current.url.control.placeholder = "https://api.openai.com/v1";
        fields.append(current.key.wrap, current.url.wrap);
      } else {
        current.url = labelled("input", "API URL");
        current.url.control.value = "http://127.0.0.1:11434";
        current.server = labelled("select", "Server type");
        current.server.control.append(option("ollama", "Ollama"), option("openai_compatible", "OpenAI-compatible (LM Studio, vLLM, llama.cpp)"));
        current.server.control.value = "ollama";
        fields.append(current.url.wrap, current.server.wrap);
        note.textContent = LOCAL_NOTE;
      }
      if (COMPANY[value]) {
        const consent = el("label", undefined, "modal-check");
        current.consent = el("input");
        current.consent.type = "checkbox";
        current.consent.setAttribute("aria-label", "I agree");
        consent.append(current.consent, el("span", ` This lets OpenDevEngine send parts of your code to ${COMPANY[value]}. I agree.`));
        fields.append(consent);
      }
    }

    kind.control.addEventListener("change", build);
    build();
    const actions = el("div", undefined, "modal-actions");
    button(actions, "Cancel", () => renderProviders());
    button(actions, "Save provider", async () => {
      const body = { kind: current.kind };
      if (current.kind === "claude" && current.how.value === "login") body.use_login = true;
      else if (current.key) body.api_key = current.key.control.value;
      if (current.url && current.url.control.value.trim()) body.endpoint = current.url.control.value.trim();
      if (current.server) body.adapter = current.server.control.value;
      if (current.consent) {
        if (!current.consent.checked) { message.textContent = "Confirm that code may be sent to this provider first."; return; }
        body.consent = true;
      }
      if (current.key) current.key.control.value = "";
      message.textContent = "";
      try { await request("/api/ai-providers", "POST", body); await refresh(); } catch (error) { fail(error); }
    }, { variant: "primary" });
    form.append(actions);
    formHost.replaceChildren(form);
  }

  async function choose(job, providerId, model) {
    message.textContent = "";
    try {
      jobs = await request(`/api/ai-jobs/${job}`, "PATCH", providerId ? { provider_id: providerId, model } : { provider_id: null });
      renderJobs();
      if (providerId && job !== "coding" && model) {
        jobs = await request(`/api/ai-jobs/${job}/test`, "POST");
        renderJobs();
      }
      schedule();
    } catch (error) { fail(error); }
  }

  function jobRow(job, label) {
    const state = jobs[job] ?? { status: "unassigned" };
    const row = el("div", undefined, "job-row");
    const which = labelled("select", `${label} provider`);
    which.control.append(option("", "Not set"));
    for (const provider of providers) {
      if (job === "coding" && provider.kind === "local") continue;
      if (job === "search" && !(provider.kind === "local" && provider.adapter === "ollama")) continue;
      const later = job === "coding" && provider.kind === "codex";
      which.control.append(option(provider.id, later ? `${provider.name} · Coding — coming later` : provider.name, { disabled: later }));
    }
    which.control.value = state.provider_id ?? "";
    const model = labelled("input", `${label} model`);
    model.control.value = state.model ?? "";
    const picked = () => providers.find((p) => p.id === which.control.value);
    which.control.addEventListener("change", () => {
      if (!which.control.value) { choose(job, null); return; }
      model.control.value = model.control.value && state.provider_id === which.control.value ? model.control.value : defaultModel(job, picked());
      if (model.control.value.trim()) choose(job, which.control.value, model.control.value.trim());
    });
    model.control.addEventListener("change", () => {
      if (which.control.value && model.control.value.trim()) choose(job, which.control.value, model.control.value.trim());
    });
    const status = el("span", STATUS[state.status] ?? state.status, "detail-muted");
    row.append(which.wrap, model.wrap, status);
    if (state.provider_id) button(row, "Test again", async () => {
      try { jobs = await request(`/api/ai-jobs/${job}/test`, "POST"); renderJobs(); schedule(); } catch (error) { fail(error); }
    });
    return row;
  }

  function renderJobs() {
    jobsHost.replaceChildren(...JOBS.map(([job, label]) => jobRow(job, label)));
    const extra = el("details");
    extra.append(el("summary", "More jobs"), ...ADVANCED_JOBS.map(([job, label]) => jobRow(job, label)));
    jobsHost.append(extra);
  }

  function renderAdvanced() {
    const parts = advanced() || {};
    more.replaceChildren(el("summary", "Advanced: other models and the coding agent"));
    for (const part of [parts.models, parts.agent]) if (part) more.append(part);
  }

  function schedule() {
    clearTimeout(timer);
    const testing = Object.values(jobs).some((j) => j && (j.status === "queued" || j.status === "running"));
    if (testing && isVisible()) timer = setTimeout(() => { refreshJobs(); }, pollMs);
  }

  async function refreshJobs() {
    try { jobs = await request("/api/ai-jobs"); renderJobs(); schedule(); } catch (error) { fail(error); }
  }

  async function refresh() {
    try {
      const data = await request("/api/ai-providers");
      providers = data.providers ?? [];
      jobs = await request("/api/ai-jobs");
    } catch (error) { fail(error); }
    renderProviders();
    renderJobs();
    renderAdvanced();
    schedule();
  }

  return { element, refresh };
}
