// Models section: local Ollama discovery, the Claude subscription card and one dropdown per job.
// Choosing a discovered model creates its profile with sensible local defaults, so most people
// never see the profile form.
import { button, createModal, el } from "./ui.js";

const OLLAMA = "http://127.0.0.1:11434";
const JOBS = [["ask", "Answering questions", "Ask conversations"], ["planning", "Writing plans", null],
  ["embedding", "Smarter code search", "embeddings · recommended"], ["summary", "Repository summaries", null]];
const CHAT_CAPS = ["chat", "structured_output"];

const wantsEmbedding = (role) => role === "embedding";
const capabilities = (profile) => profile.version?.config?.capabilities ?? [];
// Mirrors the server's loopback rule (localhost, 127.0.0.0/8, ::1); anything else is remote.
const isRemote = (value) => {
  let host;
  try { host = new URL(value).hostname; } catch { return false; }
  return !(host === "localhost" || host === "[::1]" || /^127(\.\d{1,3}){3}$/.test(host));
};

// The host:port of a configured Ollama on another machine (--ollama-url), or null when it is this computer.
export const remoteOllamaHost = (base) => (base && isRemote(base) ? new URL(base).host : null);

// Creates (or reuses) an Ollama profile for a discovered model and assigns it to a role. `endpoint` is
// the address discovery reported (the server's configured Ollama).
export async function useDiscoveredModel(request, role, model, endpoint = OLLAMA) {
  const listed = await request("/api/provider-profiles");
  let profile = (listed.profiles || []).find((p) => p.version?.adapter === "ollama" && p.version?.model === model.name && p.version?.endpoint === endpoint);
  if (!profile) {
    let config;
    if (wantsEmbedding(role)) {
      const probe = await request("/api/provider-profiles/discover/dimensions", "POST", { model: model.name });
      config = { capabilities: ["embeddings"], embedding_dimensions: probe.dimensions, timeout_seconds: 180 };
    } else {
      // Local inference can be slow on CPU-only machines; use the longest allowed limit, as `--model` does.
      config = { capabilities: CHAT_CAPS, timeout_seconds: 600, context_limit: model.context_length ?? 8192, max_output_tokens: 1024 };
    }
    profile = await request("/api/provider-profiles", "POST", { name: model.name, adapter: "ollama", endpoint, model: model.name, config });
  }
  await request("/api/settings/model-roles", "PATCH", { assignments: { [role]: profile.id } });
  return profile;
}

function addProviderForm(request, onDone, trigger) {
  const modal = createModal({ title: "Add OpenAI-compatible provider" });
  const field = (label, placeholder = "", type = "text") => {
    const wrap = el("label", label, "modal-field");
    const input = el("input");
    input.type = type;
    input.placeholder = placeholder;
    input.setAttribute("aria-label", label);
    wrap.append(input);
    return { wrap, input };
  };
  const check = (label, checked) => {
    const wrap = el("label", undefined, "modal-check");
    const input = el("input");
    input.type = "checkbox";
    input.checked = checked;
    input.setAttribute("aria-label", label);
    wrap.append(input, el("span", label));
    return { wrap, input };
  };
  const name = field("Name", "LM Studio");
  const base = field("Base URL", "http://127.0.0.1:1234/v1");
  const model = field("Model", "model id");
  const credential = field("Credential", "optional", "password");
  // Plans use the JSON-schema path only when the profile declares it; Test connection verifies it.
  const structured = check("Supports structured output (JSON schema)", true);
  // A remote destination receives no repository source without this per-profile opt-in.
  const remote = el("div", undefined, "modal-remote");
  const share = check("Send repository source to this provider", false);
  remote.append(share.wrap, el("p", "Ask and plans send retrieved code snippets to this provider. "
    + "Local-only mode in Settings → Privacy must also be off.", "modal-note"));
  let consentEndpoint = null;
  share.input.addEventListener("change", () => {
    consentEndpoint = share.input.checked ? base.input.value.trim() : null;
  });
  const showRemote = () => {
    const endpoint = base.input.value.trim();
    remote.hidden = !isRemote(endpoint);
    // Source consent is destination-specific. Editing or hiding the destination
    // requires a fresh visible opt-in, even if the user later types the old URL.
    if (remote.hidden || (share.input.checked && consentEndpoint !== endpoint)) {
      share.input.checked = false;
      consentEndpoint = null;
    }
  };
  base.input.addEventListener("input", showRemote);
  showRemote();
  const advanced = el("details", undefined, "modal-advanced");
  const timeout = field("Timeout seconds", "180", "number");
  const context = field("Context token limit", "8192", "number");
  const output = field("Maximum output tokens", "1024", "number");
  const limits = el("div", undefined, "modal-grid");
  limits.append(timeout.wrap, context.wrap, output.wrap);
  advanced.append(el("summary", "Timeouts and token limits"), limits);
  const error = el("p", "", "composer-error");
  modal.body.append(name.wrap, base.wrap, model.wrap, credential.wrap, structured.wrap, remote, advanced, error);
  button(modal.footer, "Cancel", () => modal.close(null));
  button(modal.footer, "Add provider", async (event, control) => {
    control.disabled = true;
    error.textContent = "";
    const number = (input, fallback) => (input.value.trim() ? Number(input.value) : fallback);
    const endpoint = base.input.value.trim();
    const payload = { name: name.input.value.trim(), adapter: "openai_compatible", endpoint, model: model.input.value.trim(),
      config: { capabilities: structured.input.checked ? CHAT_CAPS : ["chat"], timeout_seconds: number(timeout.input, 180), context_limit: number(context.input, 8192), max_output_tokens: number(output.input, 1024) } };
    if (isRemote(endpoint)) payload.config.allow_external_source = share.input.checked;
    if (credential.input.value) payload.secret_value = credential.input.value;
    try {
      await request("/api/provider-profiles", "POST", payload);
      credential.input.value = "";
      modal.close(true);
      onDone();
    } catch (failure) {
      credential.input.value = "";
      error.textContent = failure.message;
    } finally {
      control.disabled = false;
    }
  }, { variant: "primary" });
  modal.open(trigger);
}

const CLAUDE_MODELS = [["sonnet", "Sonnet", "recommended: fast, strong plans"], ["opus", "Opus", "slower, deepest"],
  ["haiku", "Haiku", "fastest"]];
const CLAUDE_TEST_ERRORS = {
  provider_auth: "Claude Code isn't logged in with a personal Pro or Max account. Run `claude` in a terminal, log in, then test again.",
  provider_unavailable: "Claude Code isn't installed or can't start. Install it, then test again.",
  destination_not_allowed: "Local-only mode is on. Turn it off in Settings → Privacy first.",
  provider_rate_limited: "Your Claude usage limit is reached. Try again later.",
};

// Uses the Claude Code CLI already logged in on this computer: no API key is stored.
function claudeSubscriptionForm(request, onDone, trigger) {
  const modal = createModal({ title: "Use your Claude subscription" });
  const modelWrap = el("label", "Model", "modal-field");
  const model = el("select");
  model.setAttribute("aria-label", "Model");
  for (const [value, name, hint] of CLAUDE_MODELS) { const o = el("option", `${name} (${hint})`); o.value = value; model.append(o); }
  model.value = "sonnet";
  modelWrap.append(model);
  const shareWrap = el("label", undefined, "modal-check");
  const share = el("input");
  share.type = "checkbox";
  share.setAttribute("aria-label", "Send repository source to Anthropic");
  shareWrap.append(share, el("span", "Send repository source to Anthropic"));
  const status = el("p", "", "composer-error");
  status.setAttribute("aria-live", "polite");
  modal.body.append(
    el("p", "Runs Claude Code on this computer with your own login and all of its tools turned off. "
      + "It only writes answers and plans. Usage counts against your Claude plan.", "modal-note"),
    modelWrap, shareWrap,
    el("p", "Ask and plans send retrieved code snippets to Claude. Local-only mode in Settings → Privacy must also be off.", "modal-note"),
    status);
  button(modal.footer, "Cancel", () => modal.close(null));
  let created = null;  // a failed test keeps its profile; testing again updates it instead of duplicating
  button(modal.footer, "Set up and test", async (event, control) => {
    control.disabled = true;
    status.textContent = "Testing Claude Code…";
    try {
      const name = CLAUDE_MODELS.find(([value]) => value === model.value)[1];
      const spec = { name: `Claude subscription · ${name}`, model: model.value,
        config: { capabilities: CHAT_CAPS, timeout_seconds: 300, context_limit: 32768, max_output_tokens: 8192,
          allow_external_source: share.checked } };
      const profile = created
        ? await request(`/api/provider-profiles/${encodeURIComponent(created.id)}`, "PATCH", spec)
        : await request("/api/provider-profiles", "POST", { ...spec, adapter: "claude_cli", endpoint: "https://api.anthropic.com" });
      created = profile;
      const result = await request(`/api/provider-profiles/${encodeURIComponent(profile.id)}/test`, "POST", { capabilities: CHAT_CAPS });
      if (result.status !== "passed") {
        status.textContent = CLAUDE_TEST_ERRORS[result.error_code] ?? `Test failed: ${result.error_code ?? "unknown error"}.`;
        return;
      }
      modal.close(true);
      onDone();
    } catch (failure) {
      status.textContent = failure.message;
    } finally {
      control.disabled = false;
    }
  }, { variant: "primary" });
  modal.open(trigger);
}

// onChange: called instead of a local re-render after a change saves profiles, so the page can
// refresh everything that lists them (it re-mounts this view too).
const mounts = new WeakMap();
export async function mountModels(host, { request, onChange = null }) {
  const refresh = () => (onChange ? onChange() : mountModels(host, { request, onChange }));
  // Only the newest mount may render; an older one finishing last would show stale profiles.
  const generation = (mounts.get(host) ?? 0) + 1;
  mounts.set(host, generation);
  const current = () => mounts.get(host) === generation;
  host.replaceChildren(el("p", "Loading models…", "detail-muted"));
  let discovered, listed, roleData;
  try {
    [discovered, listed, roleData] = await Promise.all([
      request("/api/provider-profiles/discover").catch(() => ({ reachable: false, models: [], error_code: "provider_unreachable" })),
      request("/api/provider-profiles"),
      request("/api/settings/model-roles"),
    ]);
  } catch (error) {
    if (current()) host.replaceChildren(el("p", `Model settings unavailable: ${error.message}`));
    return;
  }
  if (!current()) return;
  const profiles = listed.profiles || [];
  const roles = roleData.roles || {};
  const models = discovered.models || [];

  // One card lists every place a model can come from; each row is name, what it is, status, action.
  const sources = el("section", undefined, "setting-card model-sources");
  sources.append(el("h2", "Model providers"));
  const sourceRow = (name, description, status) => {
    const row = el("div", undefined, "setting-row source-row");
    const text = el("div", undefined, "source-text");
    const heading = el("div", undefined, "source-name");
    heading.append(el("strong", name));
    if (status) heading.append(el("span", status.text, `status-pill is-${status.tone}`));
    text.append(heading, el("p", description, "detail-muted"));
    row.append(text);
    sources.append(row);
    return row;
  };
  const ollamaBase = discovered.base_url ?? OLLAMA;
  const ollamaWhere = remoteOllamaHost(ollamaBase) ? `Runs models on ${remoteOllamaHost(ollamaBase)}.` : "Runs models on this computer.";
  const ollamaRow = sourceRow("Ollama", discovered.reachable ? ollamaWhere : "Not running. Start Ollama, then Retry.",
    discovered.reachable ? { text: `Connected · ${models.length} ${models.length === 1 ? "model" : "models"}`, tone: "good" } : { text: "Not running", tone: "warn" });
  button(ollamaRow, discovered.reachable ? "Refresh" : "Retry", () => mountModels(host, { request, onChange }));

  const claudeProfiles = profiles.filter((p) => p.version?.adapter === "claude_cli");
  const claudeRow = sourceRow("Claude subscription", "Claude Code on this computer with your Pro or Max login. No API key.",
    claudeProfiles.length ? { text: `Set up · ${claudeProfiles.map((p) => p.version.model).join(", ")}`, tone: "good" } : null);
  const setup = button(claudeRow, claudeProfiles.length ? "Add another model" : "Set up",
    () => claudeSubscriptionForm(request, refresh, setup));

  const otherRow = sourceRow("OpenAI-compatible server", "LM Studio, vLLM, llama.cpp or any server with an OpenAI-style API.", null);
  const add = button(otherRow, "Add provider", () => addProviderForm(request, refresh, add));
  add.setAttribute("aria-label", "Add OpenAI-compatible provider");

  const jobs = el("section", undefined, "setting-card");
  jobs.append(el("h2", "Which model does what"),
    el("p", "Pick a model for each job. Choosing an Ollama model sets it up for you.", "detail-muted"));
  const warnings = el("div");
  for (const [role, label, hint] of JOBS) {
    const row = el("div", undefined, "setting-row job-row");
    const text = el("div");
    text.append(el("span", label, "job-label"));
    if (hint) text.append(el("small", hint, "detail-muted model-hint"));
    const select = el("select");
    select.setAttribute("aria-label", label);
    const option = (value, textValue) => { const o = el("option", textValue); o.value = value; select.append(o); };
    option("", "Off");
    const seen = new Set();
    for (const profile of profiles) {
      const caps = capabilities(profile);
      if (wantsEmbedding(role) ? !caps.includes("embeddings") : !caps.includes("chat")) continue;
      option(`p:${profile.id}`, profile.name === profile.version?.model ? profile.name : `${profile.name} · ${profile.version?.model}`);
      if (profile.version?.adapter === "ollama") seen.add(profile.version.model);
    }
    for (const model of models) {
      if (seen.has(model.name) || model.embedding !== wantsEmbedding(role)) continue;
      option(`m:${model.name}`, model.name);
    }
    const current = roles[role]?.profile_id ? `p:${roles[role].profile_id}` : "";
    select.value = current;
    let saved = current;
    const error = el("p", "", "composer-error");
    select.addEventListener("change", async () => {
      const value = select.value;
      select.disabled = true;
      error.textContent = "";
      try {
        if (value.startsWith("m:")) await useDiscoveredModel(request, role, models.find((m) => m.name === value.slice(2)), ollamaBase);
        else await request("/api/settings/model-roles", "PATCH", { assignments: { [role]: value ? value.slice(2) : null } });
        saved = value;
        if (value.startsWith("m:")) refresh();
      } catch (failure) {
        select.value = saved;
        error.textContent = failure.message;
      } finally {
        select.disabled = false;
      }
    });
    row.append(text, select);
    jobs.append(row, error);
    const model = models.find((m) => m.name === roles[role]?.model);
    if (model && model.gpu === false && !warnings.textContent.includes(model.name)) {
      warnings.append(el("p", `${model.name} runs on the CPU here; long answers can take over a minute.`, "model-warning"));
    }
  }
  if (warnings.childNodes.length) jobs.append(warnings);
  host.replaceChildren(sources, jobs);
}
