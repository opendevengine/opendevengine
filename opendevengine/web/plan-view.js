// Read-only plan document: the saved plan as something a person reads top to bottom.
// Pure view. Identifiers live in a collapsed "Version details" section; editing stays in
// the plan editor. Model text is always rendered as text.
import { button, el } from "./ui.js";

const READINESS = {
  blocking_questions: () => "Answer the blocking questions above.",
  execution_not_configured: () => "Choose a tested execution profile: Edit plan → Execution and publication, then save a new version.",
  implementation_not_permitted: () => "Changes aren't allowed for this repository yet. Turn on \"Allow changes\" in Repositories.",
  base_choice_required: () => "The repository changed after this plan was made. Keep the pinned commit or regenerate the plan.",
  exactly_one_write_target_required: () => "Choose exactly one repository to change. A session edits one repository at a time.",
  scope_changed: () => "The conversation's repositories changed after this plan. Regenerate it.",
  repository_unavailable: () => "A repository in this plan is no longer available.",
  citation_unavailable: () => "Some cited source is no longer available.",
  snapshot_unavailable: () => "The indexed snapshot for this plan is no longer available.",
  execution_preflight_failed: () => "The execution check failed. Test the execution profile in Settings.",
};
const WARNINGS = {
  source_stale: "The repository has changed since it was indexed.",
  source_partial: "Only part of the repository is indexed.",
  source_indexing: "The repository is still being indexed.",
  source_metadata_only: "Only repository metadata is indexed, not its code.",
  source_failed: "The last indexing run failed.",
  validation_unapproved: "Some checks are not approved and will be skipped.",
};

export function readinessSentences(readiness = {}) {
  const reasons = (readiness.blocking_reasons || []).map((code) => READINESS[code]?.() ?? code.replaceAll("_", " "));
  const warnings = (readiness.warnings || []).map((code) => WARNINGS[code] ?? code.replaceAll("_", " "));
  return { reasons, warnings };
}

function section(parent, title, className) {
  const box = el("section", undefined, `plan-doc-section${className ? ` ${className}` : ""}`);
  box.append(el("h3", title));
  parent.append(box);
  return box;
}

function citeChips(parent, ids, byId, onCitation) {
  const found = (ids || []).map((id) => byId.get(id)).filter(Boolean);
  if (!found.length) return;
  const list = el("div", undefined, "cite-list");
  for (const citation of found) {
    const chip = button(list, `${citation.path.split("/").pop()}:${citation.start_line}–${citation.end_line}`,
      (event, control) => onCitation(citation, control));
    chip.className = "cite";
    chip.setAttribute("title", `${citation.path} lines ${citation.start_line}–${citation.end_line}`);
  }
  parent.append(list);
}

function bullets(parent, items) {
  const list = el("ul");
  for (const item of items) list.append(el("li", item));
  parent.append(list);
}

export function renderPlanDocument(version, { nameFor = (id) => id, onCitation = () => {} } = {}) {
  const doc = el("article", undefined, "plan-doc");
  const content = version.content;
  if (!content) {
    doc.append(el("p", "This plan's content is unavailable or was removed."));
    return doc;
  }
  const byId = new Map((content.citations || []).map((citation) => [citation.id, citation]));
  const repos = (content.repositories || []).map((repo) => `${nameFor(repo.repository_id)}${repo.role === "write" ? " (changes)" : " (reference)"}`);
  doc.append(el("h2", content.goal, "plan-doc-title"));
  const generation = version.generation || {};
  const created = Number.isFinite(version.created_at) ? new Date(version.created_at).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" }) : null;
  doc.append(el("p", [`Version ${version.version}`, repos.join(", ") || "no repository", generation.model, created].filter(Boolean).join(" · "), "plan-doc-meta"));

  const questions = content.open_questions || [];
  const blocking = questions.filter((q) => q.blocking && !q.answer);
  if (blocking.length) {
    const box = section(doc, blocking.length === 1 ? "Needs your answer" : `Needs your answer (${blocking.length})`, "plan-doc-attention");
    bullets(box, blocking.map((q) => q.text));
    box.append(el("p", "Answer them in the plan editor, then save a new version.", "plan-doc-note"));
  }

  const request = section(doc, "What you asked");
  request.append(el("blockquote", content.user_request));

  if (content.requirements?.length) bullets(section(doc, "Requirements"), content.requirements);

  if (content.findings?.length) {
    const box = section(doc, "What the code does today");
    for (const finding of content.findings) {
      const item = el("div", undefined, "plan-doc-item");
      item.append(el("p", finding.text));
      citeChips(item, finding.citation_ids, byId, onCitation);
      box.append(item);
    }
  }

  if (content.steps?.length) {
    const box = section(doc, "Steps");
    const list = el("ol", undefined, "plan-doc-steps");
    for (const step of content.steps) {
      const item = el("li");
      item.append(el("strong", step.title));
      if (step.description) item.append(el("p", step.description));
      if (step.paths?.length) {
        const paths = el("div", undefined, "plan-doc-paths");
        for (const path of step.paths) paths.append(el("code", path));
        item.append(paths);
      }
      citeChips(item, step.citation_ids, byId, onCitation);
      list.append(item);
    }
    box.append(list);
  }

  if (content.validation?.length) {
    const box = section(doc, "How to check it");
    for (const check of content.validation) {
      const item = el("div", undefined, "plan-doc-item");
      item.append(el("code", check.command, "plan-doc-command"));
      if (check.expected) item.append(el("p", `Expect: ${check.expected}`));
      item.append(el("p", check.approved === true ? "Approved — runs as a shell command in the session container."
        : "Not approved — this check will be skipped. Approve it by editing the plan.", "plan-doc-note"));
      box.append(item);
    }
  }

  if (content.risks?.length) bullets(section(doc, "Risks"), content.risks);
  if (content.assumptions?.length) bullets(section(doc, "Assumptions"), content.assumptions);
  const other = questions.filter((q) => !blocking.includes(q));
  if (other.length) bullets(section(doc, "Other questions"), other.map((q) => (q.answer ? `${q.text} Answer: ${q.answer}` : q.text)));

  const { reasons, warnings } = readinessSentences(version.readiness);
  const remaining = reasons.filter((text) => !text.startsWith("Answer the blocking"));
  if (remaining.length || warnings.length) {
    const box = section(doc, "Before a session can start", "plan-doc-readiness");
    bullets(box, [...remaining, ...warnings]);
  }

  if (content.citations?.length) {
    const box = section(doc, "Sources");
    const list = el("ul", undefined, "plan-doc-sources");
    for (const citation of content.citations) {
      const item = el("li");
      const chip = button(item, `${citation.path}:${citation.start_line}–${citation.end_line}`, (event, control) => onCitation(citation, control));
      chip.className = "cite";
      item.append(el("span", ` ${nameFor(citation.repository_id)} @ ${String(citation.revision).slice(0, 7)}`, "plan-doc-note"));
      list.append(item);
    }
    box.append(list);
  }

  const more = el("details", undefined, "plan-doc-details");
  more.append(el("summary", "Version details"));
  const usage = generation.usage || {};
  const tokens = Number.isFinite(usage.input_tokens) ? `${usage.input_tokens} in / ${usage.output_tokens ?? "?"} out tokens` : "usage unknown";
  for (const line of [
    `Version ${version.id}`,
    `SHA-256 ${version.content_hash}`,
    ...(content.repositories || []).map((repo) => `${nameFor(repo.repository_id)} · ${repo.repository_id} · commit ${repo.revision} · snapshot ${repo.snapshot_id}`),
    `Model ${generation.model || "unknown"} · ${tokens}`,
  ]) more.append(el("p", line, "plan-doc-mono"));
  doc.append(more);
  return doc;
}

// Shared launch decision for the conversation card and saved plan Start controls.
export function renderBaseAdvanced(parent, observations, {nameFor = id => id, onRetain, onRegenerate, onError = () => {}, retainLabel = 'Start on the saved base', regenerateLabel = 'Regenerate plan'}) {
  parent.querySelector('.base-advanced-notice')?.remove();
  const notice = el('section', undefined, 'base-advanced-notice');
  notice.setAttribute('role', 'alert'); notice.setAttribute('tabindex', '-1');
  const acknowledged = {};
  for (const item of observations) {
    notice.append(el('p', `${nameFor(item.repository_id)} moved from ${String(item.pinned_revision).slice(0,7)} to ${String(item.current_head).slice(0,7)} since this plan was made.`));
    acknowledged[item.repository_id] = item.pinned_revision;
  }
  const action = (label, run) => {
    const control = button(notice, label, async () => {
      if (control.disabled || !control.isConnected) return;
      control.disabled = true;
      try { await run(); } catch (error) { onError(error); notice.focus(); }
      finally { control.disabled = false; }
    });
    control.classList.add('btn');
  };
  action(retainLabel, () => onRetain(acknowledged));
  action(regenerateLabel, onRegenerate);
  parent.append(notice); notice.focus();
  return notice;
}
