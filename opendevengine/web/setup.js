// First-run checklist shown on the Ask landing page until GitHub, repositories and a model are set.
import { button, el } from "./ui.js";
import { STORAGE_NEEDED, startInstall, startManifest } from "./settings.js";
import { readRepositoryPages } from "./repo-source.js";
import { remoteOllamaHost, useDiscoveredModel } from "./settings-models.js";

const enc = encodeURIComponent;

export function needsSetup({ githubConnected, anyIndexed, askAssigned }) {
  return !(githubConnected && anyIndexed && askAssigned);
}

function step(number, title, done) {
  const item = el("li", undefined, `setup-step${done ? " done" : ""}`);
  item.append(el("span", done ? "✓" : String(number), "setup-num"));
  const body = el("div", undefined, "setup-body");
  body.append(el("strong", title));
  item.append(body);
  return { item, body };
}

export async function renderSetup(host, { request, onDone = () => {}, onRoles = () => {}, submitForm, navigate = (href) => location.assign(href) } = {}) {
  let connections, catalog, roles, discovered;
  try {
    [connections, catalog, roles, discovered] = await Promise.all([
      request("/api/github/connections"),
      readRepositoryPages(request, "/api/repositories?limit=100"),
      request("/api/settings/model-roles"),
      request("/api/provider-profiles/discover").catch(() => ({ reachable: false, models: [] })),
    ]);
  } catch {
    // Setup needs the connected services; without them the landing page stays as it is.
    host.replaceChildren();
    return;
  }
  // Every render rereads roles, so the page learns when a step here assigns models.
  onRoles(roles);
  const repos = catalog.items || [];
  const state = {
    githubConnected: (connections.connections || []).some((item) => item.status === "connected"),
    anyIndexed: repos.some((item) => item.index_enabled),
    askAssigned: Boolean(roles.roles?.ask?.profile_id),
  };
  if (!needsSetup(state)) {
    host.replaceChildren();
    onDone();
    return;
  }
  const again = () => renderSetup(host, { request, onDone, onRoles, submitForm, navigate });
  const box = el("section", undefined, "setup");
  const ollamaHost = remoteOllamaHost(discovered.base_url);
  const where = ollamaHost ? `Everything stays on this computer and your Ollama machine (${ollamaHost}).` : "Everything stays on this computer.";
  box.append(el("h2", "Set up OpenDevEngine"), el("p", `${where} About 3 minutes.`, "detail-muted"));
  const list = el("ol", undefined, "setup-steps");

  const github = step(1, "Connect GitHub", state.githubConnected);
  const created = (connections.connections || []).find((item) => item.status !== "connected" && item.status !== "disconnected");
  if (!state.githubConnected && created) {
    const output = el("p", "", "detail-muted");
    github.body.append(el("p", `${created.slug ?? "Your GitHub App"} is created. Install it on your repositories.`, "detail-muted"));
    button(github.body, "Install on your repositories", async () => {
      try { await startInstall(request, created.id, navigate); } catch (error) { output.textContent = error.message; }
    }, { variant: "primary" });
    github.body.append(output);
  } else if (!state.githubConnected && connections.can_configure === false) {
    github.body.append(el("p", STORAGE_NEEDED, "detail-muted"));
  } else if (!state.githubConnected) {
    const output = el("p", "", "detail-muted");
    button(github.body, "Connect GitHub", async () => {
      try { await startManifest(request, {}, submitForm); } catch (error) { output.textContent = `Could not start GitHub setup: ${error.message}`; }
    }, { variant: "primary" });
    github.body.append(output);
  }

  const reposStep = step(2, "Choose repositories to search", state.anyIndexed);
  if (!state.anyIndexed && state.githubConnected) {
    const chosen = new Set();
    const picks = el("div", undefined, "setup-picks");
    let go = null;
    const label = () => { go.textContent = `Index ${chosen.size} repositories`; go.disabled = chosen.size === 0; };
    for (const repo of repos) {
      const row = el("label", undefined, "setup-pick");
      const box = el("input");
      box.type = "checkbox";
      box.addEventListener("change", () => { if (box.checked) chosen.add(repo.id); else chosen.delete(repo.id); label(); });
      row.append(box, el("span", repo.name));
      picks.append(row);
    }
    const output = el("p", "You can keep going while this runs.", "detail-muted");
    go = button(null, "Index 0 repositories", async () => {
      go.disabled = true;
      try {
        for (const repo of repos.filter((item) => chosen.has(item.id))) {
          await request(`/api/repositories/${enc(repo.id)}`, "PATCH", { index_enabled: true, implementation_enabled: repo.implementation_enabled });
        }
        again();
      } catch (error) {
        output.textContent = `Could not start indexing: ${error.message}`;
        go.disabled = false;
      }
    }, { variant: "primary" });
    label();
    reposStep.body.append(picks, go, output);
  }

  if (!state.anyIndexed && !state.githubConnected) reposStep.body.append(el("p", "Connect GitHub first; your repositories appear here.", "detail-muted"));

  const models = step(3, "Pick models", state.askAssigned);
  if (!state.askAssigned) {
    const chat = (discovered.models || []).find((m) => !m.embedding);
    const embed = (discovered.models || []).find((m) => m.embedding);
    const choose = el("a", "Choose…");
    choose.setAttribute("href", "#/settings/providers");
    if (chat) {
      const found = ollamaHost ? `Found Ollama on ${ollamaHost}` : "Found Ollama";
      models.body.append(el("p", `${found} with ${[chat.name, embed?.name].filter(Boolean).join(" and ")}. Use them?`, "detail-muted"));
      const output = el("p", "", "detail-muted");
      button(models.body, "Use these", async (event, control) => {
        control.disabled = true;
        try {
          await useDiscoveredModel(request, "ask", chat, discovered.base_url);
          await useDiscoveredModel(request, "planning", chat, discovered.base_url);
          if (embed) await useDiscoveredModel(request, "embedding", embed, discovered.base_url);
          again();
        } catch (error) {
          output.textContent = `Could not set models: ${error.message}`;
          control.disabled = false;
        }
      }, { variant: "primary" });
      models.body.append(choose, output);
    } else {
      const missing = !ollamaHost ? "No local Ollama models found. Install Ollama and pull a model, or add a provider."
        : discovered.reachable ? `Ollama on ${ollamaHost} has no models yet. Pull one there, or add a provider.`
          : `Can't reach Ollama on ${ollamaHost}. Check it is running and listening on that address, then reload, or add a provider.`;
      models.body.append(el("p", missing, "detail-muted"), choose);
    }
  }

  list.append(github.item, reposStep.item, models.item);
  box.append(list);
  host.replaceChildren(box);
}
