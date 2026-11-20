// Renders an Ask conversation as a chat thread. Pure view: all actions go through handlers.
import { readinessSentences } from "./plan-view.js";
import { button, el } from "./ui.js";

const ERROR_WORDS = {
  provider_timeout: "the model took too long. A smaller model, or one running on a GPU, may respond in time; choose it in Settings → Models",
  provider_unavailable: "the model isn't reachable",
  provider_unreachable: "the model isn't reachable",
  context_overflow: "the question and sources were too large for this model's limits. Lower its context token limit in Settings → Models",
  provider_rate_limited: "the model provider's rate limit was reached. Try again in a minute",
  provider_rejected: "the model provider refused the request",
  invalid_output: "the model's reply didn't pass validation twice. Try again or rephrase",
  role_unassigned: "no model is set for answering questions",
  source_unavailable: "the repository source is no longer available",
  cancelled: "it was cancelled",
};
const DONE = new Set(["complete", "completed", "failed", "cancelled"]);

export function errorWords(code) {
  return ERROR_WORDS[code] ?? (code ? String(code).replaceAll("_", " ") : "something went wrong");
}

function shortName(name) {
  return String(name ?? "").split("/").pop();
}

// `text` with `inline code` spans.
function richText(text) {
  const paragraph = el("p", undefined, "msg-text");
  String(text ?? "").split(/(`[^`\n]+`)/).forEach((part) => {
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) paragraph.append(el("code", part.slice(1, -1)));
    else if (part) paragraph.append(part);
  });
  return paragraph;
}

function details(message) {
  const destination = message.destination ? `${message.destination.kind ?? ""} ${message.destination.host ?? ""}`.trim() : "destination unknown";
  const usage = message.usage || {};
  const tokens = Number.isFinite(usage.input_tokens) ? `${usage.input_tokens} in / ${usage.output_tokens ?? "?"} out tokens` : "usage unknown";
  const cost = Number.isFinite(usage.estimated_cost) ? `${usage.cost_status || "estimated"} ${usage.estimated_cost} USD` : "cost unknown";
  return `${message.model ?? "model unknown"} · ${destination} · ${tokens} · ${cost}`;
}

function assistant(message, view, handlers) {
  const box = el("article", undefined, "msg-assistant");
  if (message.state === "complete" || message.state === "completed") {
    box.append(richText(message.content));
    if (message.citations?.length) {
      const list = el("div", undefined, "cite-list");
      for (const citation of message.citations) {
        const chip = button(list, `${citation.path.split("/").pop()}:${citation.start_line}–${citation.end_line}`, (event, control) => handlers.onCitation(citation, control));
        chip.className = "cite";
        chip.setAttribute("title", `${citation.path} lines ${citation.start_line}–${citation.end_line}`);
      }
      box.append(list);
    }
    const detail = el("p", "", "msg-details");
    detail.hidden = true;
    const info = button(box, "ⓘ", () => { detail.hidden = !detail.hidden; detail.textContent = details(message); }, { variant: "ghost" });
    info.setAttribute("aria-label", "Answer details");
    box.append(detail);
  } else if (message.state === "failed") {
    const run = view.runs.find((item) => item.assistant_message_id === message.id);
    const failed = run?.intent === "plan" ? "Couldn't make the plan" : "Couldn't answer";
    box.append(el("p", `${failed}: ${errorWords(message.error_code ?? run?.error_code)}.`, "msg-error"));
    if (run) button(box, "Retry", () => handlers.onRetry(run.id));
  } else if (message.state === "cancelled") {
    box.append(el("p", "Cancelled.", "msg-muted"));
    const run = view.runs.find((item) => item.assistant_message_id === message.id);
    if (run) button(box, "Retry", () => handlers.onRetry(run.id));
  } else {
    const seconds = Math.max(0, Math.round(((view.now ?? Date.now()) - message.created_at) / 1000));
    box.append(el("p", `Waiting for the model… ${seconds} s`, "msg-pending"));
  }
  return box;
}

const FILLER = new Set(["a", "an", "and", "the", "or", "of", "to", "in", "on", "for", "with"]);
// "Catalog evidence: name: worker; profile: order, checkout" -> "Matches worker, order, checkout".
function routeReason(reason) {
  if (!reason) return "";
  if (!reason.startsWith("Catalog evidence: ")) return reason;
  // Older decisions separate fields with commas too; drop "field:" labels and filler words either way.
  const terms = reason.slice("Catalog evidence: ".length).split(/[;,]/).map((term) => term.replace(/^\s*[\w-]+:/, "").trim())
    .filter((term) => term && !FILLER.has(term.toLowerCase()));
  return terms.length ? `Matches ${[...new Set(terms)].slice(0, 6).join(", ")}` : "";
}

function routingNode(routing, handlers) {
  const names = (routing.candidates || []).map((candidate) => shortName(candidate.name));
  if (routing.state === "resolved" && names.length) {
    const step = el("p", undefined, "thread-step");
    const reason = routing.candidates[0].reason ? ` because ${routing.candidates[0].reason}` : "";
    step.append(`✓ Looked in ${names.join(", ")}${reason} · `);
    button(step, "change", () => handlers.onChooseRepos(), { variant: "ghost" }).classList.add("link-button");
    return step;
  }
  const card = el("div", undefined, "thread-card");
  if (routing.state === "needs_selection" && names.length) {
    card.append(el("p", "Which repository?", "thread-card-title"));
    const choices = el("div", undefined, "route-choices");
    for (const candidate of routing.candidates) {
      const choice = button(choices, "", () => handlers.onPickRoute(candidate.repository_id));
      choice.classList.add("route-choice");
      choice.append(el("span", shortName(candidate.name), "route-choice-name"));
      const why = routeReason(candidate.reason);
      if (why) choice.append(el("span", why, "route-choice-reason"));
    }
    card.append(choices);
  } else {
    card.append(el("p", "No repository matched this question.", "thread-card-title"));
  }
  button(card, "Choose…", () => handlers.onChooseRepos());
  return card;
}

function planCard(plan, view, handlers) {
  const card = el("div", undefined, "plan-card");
  card.append(el("p", `📋 ${plan.title} · ${plan.steps ?? "?"} steps · v${plan.version ?? "?"}`, "plan-card-title"));
  if (view.staleScopeSincePlan?.planId === plan.id) {
    const scope = view.staleScopeSincePlan;
    const changes = [...(scope.addedNames || []).map(n=>`${n} was added`), ...(scope.removedNames || []).map(n=>`${n} was removed`)];
    const stale = el("p", `Made before ${changes.join(", ")} · `, "plan-card-stale");
    button(stale, "Regenerate", () => handlers.onRegenerate(plan.id), { variant: "ghost" }).classList.add("link-button");
    card.append(stale);
  }
  const actions = el("div", undefined, "plan-card-actions");
  button(actions, "View plan", () => handlers.onViewPlan(plan.id));
  const start = button(actions, "Start session", async (_event, control) => {
    if (control.disabled) return;
    control.disabled = true;
    try { await handlers.onStartSession(plan.id); }
    finally { control.disabled = !plan.ready || view.staleScopeSincePlan?.planId === plan.id; }
  }, { variant: "primary" });
  start.disabled = !plan.ready || view.staleScopeSincePlan?.planId === plan.id;
  if (!plan.ready && plan.reason) start.setAttribute("title", plan.reason);
  card.append(actions);
  if (!plan.ready) {
    const codes = (plan.reason || '').split(', ').filter(Boolean);
    const reason = el("p", readinessSentences({blocking_reasons: codes}).reasons.join(' ') || 'Review the plan before starting.', "plan-card-reason");
    const fix = el("a", codes.includes('execution_not_configured') ? "Edit plan → Execution and publication" : "Review and fix plan");
    fix.setAttribute("href", `#/plans/${encodeURIComponent(plan.id)}${plan.latest ? `/${encodeURIComponent(plan.latest)}` : ''}`);
    fix.addEventListener("click", event => { event.preventDefault(); handlers.onStartSession(plan.id, {fix: true}); });
    reason.append(' ', fix);
    card.append(reason);
  }
  return card;
}

export function renderThread(host, view, handlers) {
  const timeline = [
    ...view.messages.map((message) => ({ at: message.created_at, kind: "message", message })),
    ...(view.scopeEvents || []).map((event) => ({ at: event.at, kind: "scope", event })),
    ...view.plans.map((plan) => ({ at: plan.created_at ?? Infinity, kind: "plan", plan })),
  ].sort((a, b) => a.at - b.at);
  const lastUser = timeline.findLastIndex((item) => item.kind === "message" && item.message.role === "user");
  const planIds = new Set(view.plans.map((plan) => plan.id));
  // The server's "Plan saved: <id>" receipt adds nothing when the plan card itself is shown.
  const receipt = (message) => message.role === "assistant" && /^Plan saved: \S+$/.test(message.content ?? "")
    && planIds.has(message.content.slice("Plan saved: ".length));
  const nodes = [];
  timeline.forEach((item, index) => {
    if (item.kind === "message" && receipt(item.message)) {
      // The plan card follows in the timeline.
    } else if (item.kind === "message") {
      nodes.push(item.message.role === "user" ? el("div", item.message.content, "msg-user") : assistant(item.message, view, handlers));
    } else if (item.kind === "scope") {
      nodes.push(el("p", `${item.event.kind === "added" ? "Added" : "Removed"} ${item.event.name}`, "thread-event"));
    } else {
      nodes.push(planCard(item.plan, view, handlers));
    }
    if (index === lastUser && view.routing) nodes.push(routingNode(view.routing, handlers));
  });
  if (lastUser === -1 && view.routing) nodes.push(routingNode(view.routing, handlers));
  host.replaceChildren(...nodes);
  return { pending: view.messages.some((message) => message.role === "assistant" && !DONE.has(message.state)) };
}
