import { renderBaseAdvanced } from './plan-view.js';
// Durable session views consume safe public projections. Source is always text.
import { badge, relativeTime, renderMarkdown } from './ui.js';
import { readRepositoryPages } from './repo-source.js';
const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = String(text); if (cls) n.className = cls; return n; };
const enc = encodeURIComponent;
const terminal = new Set(['completed', 'failed', 'cancelled', 'interrupted', 'blocked']);
const inside = (root, child) => { for (let n = child; n; n = n.parentNode) if (n === root) return true; return false; };
const time = value => value ? new Date(value).toLocaleString() : 'unknown';
const human = value => String(value ?? 'unknown').replaceAll('_', ' ');
// Plain-language explanations for session errors a user can reach; other codes fall back to their words.
const ERROR_TEXT = {
  retry_exists: 'A newer attempt of this session already exists; open it to continue.',
  session_active: 'This session is still running; stop it or wait for it to finish first.',
  runtime_in_use: 'Another OpenDevEngine process on this data directory is handling sessions; close it first.',
  plan_not_ready: 'The saved plan is not ready to start; open it to see what needs fixing.',
  publication_in_progress: 'A pull request is still being published for this session; wait for it to finish.',
  purged: 'This session’s content was deleted.',
  idempotency_conflict: 'A different request already used this action; reload the session and try again.',
  source_unavailable: 'The repository source is not available under the current sharing settings.',
};
const errorText = code => ERROR_TEXT[code] || human(code);
const scalar = value => typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean' ? String(value) : 'unknown';
const storage = {
  get(key) { try { return JSON.parse(window.sessionStorage?.getItem(key) || 'null'); } catch { return null; } },
  set(key, value) { try { if (value === null) window.sessionStorage?.removeItem(key); else window.sessionStorage?.setItem(key, JSON.stringify(value)); } catch {} },
};
export async function executionRequest(path, method = 'GET', body, {signal} = {}) {
  let response;
  try { response = await fetch(path, {method, cache: 'no-store', headers: {'Content-Type': 'application/json'}, ...(signal ? {signal} : {}), ...(body === undefined ? {} : {body: JSON.stringify(body)})}); }
  catch { throw new Error(method === 'GET' ? 'Connection unavailable. Reconnect to reload saved state.' : 'Connection lost; outcome unknown. Retry the same request to reconcile saved state.'); }
  let value;
  try { value = await response.json(); } catch { throw new Error('Response unavailable; outcome unknown. Reload saved state or retry the same request.'); }
  if (!response.ok) { const code = /^[a-z_]{1,80}$/.test(value?.code) ? value.code : `http_${response.status}`; const e = new Error(errorText(code)); e.status = response.status; e.code = code; e.details = value?.details; throw e; }
  return value;
}
function link(parent, text, hash) { const a = el('a', text); a.setAttribute('href', hash); a.dataset.focus = hash; parent.append(a); return a; }
function button(parent, text, fn) {
  const b = el('button', text, 'button'); b.type = 'button';
  b.addEventListener('click', () => { if (!b.disabled && b.isConnected) fn(b); }); parent.append(b); return b;
}
// One phase label per session, shared by the list, the rail and the session header.
// `pub` is the session's verified publication record when the page has read it.
export function phaseBadge(s, checks = null, pub = null) {
  if (pub?.state === 'published' && pub.pull_request_state === 'merged') return {label: 'PR merged', tone: 'ok'};
  if (['open', 'pr_open', 'published'].includes(s.publication?.state) || (pub?.state === 'published' && pub.pull_request_state !== 'closed')) return {label: 'PR open', tone: 'ok'};
  if (s.state === 'waiting_for_user') return {label: 'needs your input', tone: 'warn'};
  if (s.state === 'completed') return checks?.items?.some(c => c.status === 'failed') ? {label: 'completed · checks failing', tone: 'warn'} : {label: 'completed', tone: 'ok'};
  if (s.state === 'cancelled') return {label: 'cancelled', tone: 'off'};
  // Terminal states win over a recorded stop request: a finished session is never "stopping".
  if (terminal.has(s.state)) return {label: human(s.state === 'interrupted' || s.state === 'blocked' ? s.state : 'failed'), tone: 'danger'};
  if (s.state === 'stopping' || s.stop_requested) return {label: 'stopping', tone: 'warn'};
  if (s.state === 'queued' || s.state === 'provisioning') return {label: 'starting', tone: 'info'};
  if (s.state === 'validating') return {label: 'running checks', tone: 'info'};
  return {label: 'running', tone: 'info'};
}
const elapsedSeconds = s => { const start = s.started_at || s.created_at; return start ? Math.max(0, Math.floor(((s.finished_at || Date.now()) - start) / 1000)) : null; };
// "25 s", "4 min 10 s", "2 h 5 min": durations people read at a glance.
export function duration(seconds) {
  if (!Number.isFinite(seconds)) return null;
  if (seconds > 0 && seconds < 0.5) return 'under 1 s';
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  if (s < 3600) { const m = Math.floor(s / 60), r = s % 60; return r ? `${m} min ${r} s` : `${m} min`; }
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60); return m ? `${h} h ${m} min` : `${h} h`;
}
// A visual separator only; screen readers get the parts without punctuation noise.
const sep = () => { const n = el('span', '·', 'session-meta-sep'); n.setAttribute('aria-hidden', 'true'); return n; };
const shortSha = sha => typeof sha === 'string' && /^[0-9a-f]{7,64}$/i.test(sha) ? sha.slice(0, 7) : null;
function lines(parent, entries) { const dl = el('dl', undefined, 'session-facts'); for (const [label, value] of entries) dl.append(el('dt', label), el('dd', value)); parent.replaceChildren(dl); }
export function limitsText(limits = {}) {
  const runner = limits.runner || limits.runner_limits || {}, agent = limits.agent || limits.agent_limits || {};
  const parts = [];
  for (const [key, label, unit] of [['memory_mib', 'Memory', 'MiB'], ['workspace_mib', 'Workspace', 'MiB'], ['tmp_mib', 'Temporary disk', 'MiB'], ['cpus', 'CPU', 'cores'], ['pids', 'Processes', ''], ['wall_seconds', 'Runner deadline', 's'], ['max_files', 'Files', ''], ['max_file_bytes', 'File size', 'bytes'], ['max_output_bytes', 'Command output', 'bytes']]) if (runner[key] !== undefined) parts.push(`${label} ${runner[key]} ${unit}`.trim());
  for (const [key, label, unit] of [['wall_seconds', 'Agent deadline', 's'], ['max_turns', 'Turns', ''], ['max_budget_usd', 'Spending cap', 'USD'], ['max_output_bytes', 'Agent output', 'bytes']]) if (agent[key] !== undefined) parts.push(`${label} ${agent[key]} ${unit}`.trim());
  return parts.join(' · ') || 'Limits unavailable';
}
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
const TOOL_VERB = {read_file: 'Read', write_file: 'Edited', delete_file: 'Deleted', list_files: 'Listed files', run_command: 'Ran a command', ask_user: 'Asked a question'};
function checkpointText(p) {
  if (!p.ok) return `Checkpoint unavailable · ${human(p.code || p.reason)}`;
  const files = Number.isFinite(p.files) ? p.files : ['added', 'modified', 'deleted'].reduce((n, k) => n + (Number.isFinite(p[k]) ? p[k] : 0), 0);
  const left = Number.isFinite(p.rejected) && p.rejected > 0 ? ` · ${plural(p.rejected, 'path')} left out` : '';
  if (!files) return `Checkpoint saved · no changes${left}`;
  const counts = Number.isFinite(p.additions) && Number.isFinite(p.deletions) ? `+${p.additions} −${p.deletions} in ` : '';
  return `Checkpoint saved · ${counts}${plural(files, 'file')}${left}`;
}
// Maps one saved event to a short label. Only mapped fields are read; arbitrary payloads and
// private reasoning never render, and agent text is always set as text, never markup.
// `setup` marks start-up steps; `work` marks agent tool work and checkpoints; `message` is agent prose.
function describeEvent(event, sourceAllowed) {
  const p = event.payload && typeof event.payload === 'object' ? event.payload : {};
  const type = typeof event.type === 'string' ? event.type : 'event';
  if (p.redacted || !sourceAllowed) return {label: `${human(type)} · details redacted by current source policy`};
  const text = value => typeof value === 'string' && value.trim() ? value : null;
  switch (type) {
    case 'session.state': {
      const st = p.state;
      if (st === 'queued') return {label: Number.isSafeInteger(p.attempt) && p.attempt > 1 ? `Queued · attempt ${p.attempt}` : 'Queued', setup: true};
      if (st === 'provisioning') return {label: p.phase === 'access_check' ? 'Checked repository access' : p.phase === 'source_acquired' ? `Copied the repository${Number.isSafeInteger(p.counts?.files) ? ` · ${plural(p.counts.files, 'file')}` : ''}` : 'Preparing the container', setup: true};
      if (st === 'running') return p.previous === 'provisioning' || p.previous === 'queued' ? {label: 'Agent started', setup: true} : {label: 'Agent running'};
      if (st === 'waiting_for_user') return {label: 'Waiting for your answer', tone: 'warn'};
      if (st === 'validating') return {label: 'Running checks'};
      if (st === 'stopping') return {label: 'Stopping', tone: 'warn'};
      if (st === 'completed') return {label: 'Session completed', tone: 'ok'};
      if (st === 'cancelled') return {label: p.stop_reason === 'user_requested' ? 'Cancelled · stopped by you' : `Cancelled${p.stop_reason ? ` · ${human(p.stop_reason)}` : ''}`, tone: 'off'};
      const why = p.error_code || p.reason || p.stop_reason;
      const name = human(st);
      return {label: `${name.charAt(0).toUpperCase()}${name.slice(1)}${why ? ` · ${human(why)}` : ''}`, tone: ['failed', 'interrupted', 'blocked'].includes(st) ? 'danger' : undefined};
    }
    case 'agent.summary':
      if (p.kind === 'ready') return {label: `Agent ready · ${typeof p.model === 'string' && p.model ? p.model : 'model unknown'}`, setup: true, model: typeof p.model === 'string' ? p.model : null};
      if (p.kind === 'inventory') return {label: 'Agent tools connected', setup: true};
      if (p.kind === 'assistant') return {label: 'Agent', message: text(p.text), agent: true};
      if (p.kind === 'result') return {label: `Agent finished · ${human(p.status || p.code || 'unknown')}`, message: text(p.text), result: true, tone: p.status === 'completed' ? undefined : 'warn'};
      return {label: [human(p.kind), p.status, p.code].filter(Boolean).map(scalar).join(' · '), message: text(p.text)};
    case 'tool.started': {
      const tool = scalar(p.tool), target = typeof p.path === 'string' ? p.path : typeof p.program === 'string' ? p.program : null;
      return {label: `${TOOL_VERB[tool] || human(tool)}${target ? ` · ${target}` : ''}`, work: true, path: typeof p.path === 'string' ? p.path : null};
    }
    case 'tool.finished': case 'tool.denied': {
      const tool = human(scalar(p.tool)), denied = type === 'tool.denied', failed = p.ok === false;
      const parts = [`${tool.charAt(0).toUpperCase()}${tool.slice(1)} ${denied ? 'denied' : failed ? 'failed' : 'finished'}`, typeof p.path === 'string' ? p.path : null, Number.isSafeInteger(p.exit_code) ? `exit ${p.exit_code}` : null, p.code ? human(p.code) : null, p.reason ? human(p.reason) : null];
      return {label: parts.filter(Boolean).map(scalar).join(' · '), work: true, tone: denied ? 'warn' : failed ? 'danger' : undefined, path: typeof p.path === 'string' ? p.path : null};
    }
    case 'question.asked': return {label: 'The agent asked', message: text(p.prompt) || human(p.state), tone: 'warn'};
    case 'message.delivery': return {label: `Your message · ${human(p.delivery)}${p.reason ? ` · ${human(p.reason)}` : ''}`};
    case 'checkpoint': return {label: checkpointText(p), checkpoint: true, tone: p.ok ? undefined : 'warn'};
    case 'validation.started': return {label: `Check ${scalar(p.ordinal ?? '')} started`};
    case 'validation.finished': return {label: `Check ${scalar(p.ordinal ?? '')} ${human(p.status)}${Number.isSafeInteger(p.exit_code) ? ` · exit ${p.exit_code}` : ''}${p.reason ? ` · ${human(p.reason)}` : ''}`, tone: p.status === 'passed' ? 'ok' : p.status === 'not_run' ? 'off' : 'danger'};
    case 'stop.requested': return {label: p.reason === 'user_requested' ? 'You asked the session to stop' : `Stop requested · ${human(p.reason)}`, tone: 'warn'};
    case 'containment': return p.confirmed === true ? {label: 'Container removed', tone: 'ok'} : {label: `Container removal not confirmed · ${human(p.code || p.container_code)}`, tone: 'danger'};
    case 'usage': return {label: 'Usage recorded (totals in Run details)'};
    case 'error': return {label: `Error · ${human(p.code || p.error_code)}`, tone: 'danger'};
    case 'publication.state': return {label: `Draft pull request · ${human(p.state)}`};
    default: return {label: human(type)};
  }
}
// Plain-language session outcomes; the raw code is always shown beside them in small print.
const SESSION_TEXT = {
  runtime_interrupted: 'OpenDevEngine stopped while this session ran.',
  containment_failed: "OpenDevEngine couldn't confirm the container stopped. Request a stop again and check Docker.",
  harness_containment_failed: "OpenDevEngine couldn't confirm the agent process stopped.",
  budget_exhausted: 'The agent reached its spending or turn limit before finishing.',
  repository_unavailable: "The repository isn't available to OpenDevEngine any more.",
  policy_denied: 'A workspace or repository policy blocked this session.',
  source_access_denied: "OpenDevEngine's GitHub access can't read this repository.",
  source_acquisition_failed: "OpenDevEngine couldn't copy the repository source into the container.",
  source_unavailable: 'Source is unavailable under the current repository access or retention policy.',
  execution_profile_unavailable: 'The execution profile pinned by the plan is no longer available.',
  execution_test_required: "The execution profile hasn't passed its synthetic coding test.",
  cli_unavailable: "The coding agent CLI isn't installed or couldn't start.",
  authentication_required: 'The coding agent CLI is not signed in on this machine.',
  docker_unavailable: "Docker isn't running or isn't reachable.",
  preflight_failed: 'The container image failed its pre-flight check.',
  image_unsupported: "The pinned container image isn't supported.",
  checkpoint_unavailable: "The session's changes couldn't be saved as a checkpoint.",
  runtime_identity_unverified: "OpenDevEngine couldn't verify the container it started.",
  lease_expired: 'OpenDevEngine restarted while this session ran.',
  plan_unavailable: 'The saved plan version is no longer available.',
  external_source_denied: 'Sending repository source to the external coding model is not permitted.',
  repository_purged: 'The repository was removed from OpenDevEngine, so the session stopped.',
};

// ---- Draft pull request --------------------------------------------------------------------
// Renders only mapped fields of the safe public preview/publication projections: never tokens,
// object bodies or raw GitHub responses. There is deliberately no merge action anywhere.
const PUBLICATION_STATES = new Set(['queued', 'preparing', 'creating_objects', 'creating_branch', 'creating_pr', 'reconciling', 'published', 'blocked', 'failed', 'no_changes']);
const PUBLICATION_DONE = new Set(['published', 'blocked', 'failed', 'no_changes']);
const PUBLICATION_STEPS = [['queued', 'Waiting to start'], ['preparing', 'Checking the base branch'], ['creating_objects', 'Creating the commit'], ['creating_branch', 'Creating the branch'], ['creating_pr', 'Opening the draft pull request']];
const SAFE_PR_URL = /^https:\/\/github\.com\/[^/]+\/[^/]+\/pull\/\d+$/;
const PUBLICATION_TEXT = {
  publication_identity_required: 'Set the commit author name and email used for pull requests.',
  publication_permission_denied: "OpenDevEngine's GitHub App can't write to this repository. It needs Contents and Pull requests write access.",
  automatic_policy_not_satisfied: 'The saved policy publishes automatically only after a fully validated, reviewable result.',
  stale_diff: "The session's changes no longer match the preview you reviewed.",
  idempotency_conflict: 'A different publish request already exists for this session.',
  source_unavailable: 'Source is unavailable under the current repository access or retention policy.',
  sensitive_content: "Sensitive content was detected in the changes, so this change can't be published.",
  rejected_paths: "Some changed paths were rejected by the artifact policy, so this change can't be published.",
  session_snapshot_invalid: "The session's saved checkpoint couldn't be verified for publishing.",
  session_unavailable: "The session's saved changes aren't available for publishing.",
  base_conflict: 'The base branch has since changed some of the same files. Nothing was rebased or overwritten.',
  divergence_unknown: 'The base branch moved too far to check safely for overlapping changes.',
  branch_collision: 'A branch with this name already points to different work.',
  pull_request_invalid: "GitHub's pull request response couldn't be verified.",
  pull_request_mismatch: "The pull request GitHub returned doesn't match this change.",
  pull_request_conflict: 'More than one pull request matches this change.',
  github_unavailable: 'GitHub was unavailable.',
  repository_purged: 'The repository was removed from OpenDevEngine, so publishing stopped.',
  reconciliation_pending: "GitHub hasn't confirmed the earlier step yet. OpenDevEngine keeps checking; try again in a moment.",
  pr_outcome_unknown: "GitHub didn't confirm whether the pull request was created.",
  branch_outcome_unknown: "GitHub didn't confirm whether the branch was created.",
  validation: 'Check the title (1 to 256 characters) and the description.',
  bad_request: 'The request was not accepted. Preview again and retry.',
  not_found: 'The session or its publication was not found.',
  publication_stopped: 'Publishing stopped because the operation was superseded or its job ended.',
  base_overlap: 'The base branch has since changed some of the same files. Nothing was rebased or overwritten.',
  timeout: 'GitHub did not answer in time.',
  upstream_unavailable: 'GitHub was unavailable.',
  rate_limited: 'GitHub is rate limiting requests. Try again later.',
  auth: "OpenDevEngine's GitHub App could not authenticate. Reconnect GitHub in Settings.",
  capability_missing: "OpenDevEngine's GitHub App lacks a permission this needs. It needs Contents and Pull requests write access.",
  repository_unavailable: "The repository isn't available to OpenDevEngine's GitHub App.",
};
const publicationFix = code => code === 'publication_identity_required' ? ['Set the pull request author in Settings', '#/settings/providers'] : /permission|^auth$|^capability_missing$|^repository_unavailable$/.test(code) ? ['Review GitHub access in Settings', '#/settings/github'] : null;
function explainCode(parent, code, fallback) {
  const known = /^[a-z_]{1,80}$/.test(code || '') ? code : null;
  parent.append(el('p', (known && PUBLICATION_TEXT[known]) || fallback || `Publishing stopped: ${human(known || 'unknown')}.`));
  const fix = known && publicationFix(known); if (fix) link(parent, fix[0], fix[1]);
  if (known) parent.append(el('small', `Code: ${known}`, 'publication-code'));
}
const newPublicationKey = () => `pub-${globalThis.crypto.randomUUID()}`;
const count = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

function createPublicationArea({section, sessionID, request, pollMs, isLive, onChange = () => {}}) {
  let mode = 'waiting', session = null, preview = null, key = null, unknown = false, draft = null, pub = null, notice = null;
  let busy = false, dead = false, timer, retryKey = null, recovering = false, recheckTimer = null, rechecks = 0, seenUpdate;
  // Publication reads are cancelled when the page is left; late answers are fenced by alive() as well.
  const reads = new AbortController();
  const read = path => request(path, 'GET', undefined, {signal: reads.signal});
  const RECHECK_LIMIT = 8;
  const heading = el('h2', 'Draft pull request'); heading.setAttribute('tabindex', '-1');
  const announcer = el('p', '', 'publication-status'); announcer.setAttribute('role', 'status'); announcer.setAttribute('aria-live', 'polite');
  const body = el('div', undefined, 'publication-area');
  section.replaceChildren(heading, announcer, body);
  const alive = () => !dead && isLive();
  const announce = text => { if (announcer.textContent !== text) announcer.textContent = text; };
  const validPublication = v => Boolean(v) && typeof v.id === 'string' && v.id && v.session_id === sessionID && PUBLICATION_STATES.has(v.state);
  const blocked = () => !preview || preview.publishable !== true || (Array.isArray(preview.blocking_reasons) && preview.blocking_reasons.length > 0);
  // Focus moves only as a consequence of the user's own action inside this area; polling never steals it.
  function render(focusKey = null, take = inside(section, document.activeElement)) {
    const previous = inside(section, document.activeElement) ? document.activeElement?.dataset?.focus : null;
    body.replaceChildren(); ({waiting, loading: () => body.append(el('p', 'Checking for an existing draft pull request…')), unavailable, idle, review, follow, recoverError})[mode]();
    onChange({mode, pub});
    if (!take) return;
    const wanted = focusKey || (document.activeElement === document.body || !document.activeElement?.isConnected ? previous : null);
    if (!wanted && document.activeElement !== document.body && document.activeElement?.isConnected) return;
    const target = wanted && [...section.querySelectorAll('[data-focus]')].find(n => n.dataset.focus === wanted && !n.disabled);
    (target || heading).focus();
  }
  function policyLines() {
    const policy = session?.publication?.policy || 'unavailable';
    body.append(el('p', `Saved publication policy: ${human(policy)}.`, 'detail-muted'), el('p', policy === 'on_success_draft' ? 'The saved launch policy authorizes a remote branch and draft PR only after server success and publication checks.' : 'An explicit publication action is required after review.', 'detail-muted'));
  }
  function showNotice(parent) {
    if (!notice) return;
    const box = el('div', undefined, 'publication-notice');
    if (notice.code) explainCode(box, notice.code, notice.message); else box.append(el('p', notice.message));
    if (notice.offerPreview) { const again = button(box, 'Preview again', b => startPreview(b)); again.dataset.focus = 'notice-preview'; }
    parent.append(box);
  }
  function waiting() { body.append(el('p', "Publishing isn't available yet. You can create a draft pull request after the session finishes.")); policyLines(); }
  function unavailable() { body.append(el('p', "Publishing isn't available yet in this build.")); policyLines(); }
  function recoverError() {
    body.append(el('p', `Couldn't check for an existing draft pull request: ${notice?.message || 'unavailable'}.`));
    const again = button(body, 'Check again', () => recover(true)); again.dataset.focus = 'check-again';
  }
  function idle() {
    body.append(el('p', 'Review the changes, checks and description, then publish them as a draft pull request on a new branch. OpenDevEngine never merges, force-pushes or changes the base branch.'));
    const create = button(body, busy ? 'Preparing preview…' : 'Create draft PR', b => startPreview(b)); create.dataset.focus = 'create'; create.classList.add('publication-primary'); create.disabled = busy;
    showNotice(body); policyLines();
  }
  function facts(parent, entries) { const dl = el('dl', undefined, 'session-facts'); for (const [label, value] of entries) dl.append(el('dt', label), el('dd', value)); parent.append(dl); }
  function renderChecks(parent, checks) {
    const box = el('div', undefined, 'publication-checks'); box.append(el('h4', 'Checks'));
    const c = checks && typeof checks === 'object' ? checks : {};
    const summary = c.summary && typeof c.summary === 'object' ? c.summary : {};
    const items = Array.isArray(c.items) ? c.items : [];
    const tally = {...summary}; if (!Object.keys(tally).length) for (const item of items) tally[item.status] = (tally[item.status] || 0) + 1;
    // Checks the plan did not approve were never run; they are skipped, not failing.
    const skipped = Number.isSafeInteger(c.skipped_not_approved) && c.skipped_not_approved > 0 ? c.skipped_not_approved : 0;
    if (skipped && tally.not_run) { tally.not_run = Math.max(0, tally.not_run - skipped); if (!tally.not_run) delete tally.not_run; }
    const counts = Object.entries(tally).filter(([, n]) => Number.isFinite(n)).map(([status, n]) => `${n} ${human(status)}`).join(' · ');
    const failing = ['failed', 'unavailable', 'error', 'not_run', 'timed_out'].filter(s => tally[s] > 0);
    const skippedText = skipped ? ` ${skipped} skipped (not approved).` : '';
    if (c.fully_validated === true) box.append(el('p', `All recorded checks passed${counts ? ` (${counts})` : ''}.`));
    else {
      box.append(el('p', (failing.length ? `Not fully validated: ${failing.map(s => `${tally[s]} ${human(s)}`).join(', ')}. The default description discloses this; reviewers see it on GitHub.` : `Not fully validated${counts ? ` (${counts})` : ': no passing check run was recorded'}. The default description discloses this.`) + skippedText, 'session-warning'));
    }
    if (items.length) { const list = el('ul'); for (const item of items) list.append(el('li', `Check ${scalar(item.ordinal)} · ${human(item.status)}${item.required ? ' · required' : ''}`)); box.append(list); }
    parent.append(box);
  }
  function review() {
    const p = preview, stats = p.stats && typeof p.stats === 'object' ? p.stats : {};
    const title = el('h3', 'Review draft pull request'); title.setAttribute('tabindex', '-1'); title.dataset.focus = 'review'; body.append(title);
    const changes = [Number.isFinite(stats.files) ? count(stats.files, 'file') : null, Number.isFinite(stats.additions) ? `+${stats.additions}` : null, Number.isFinite(stats.deletions) ? `−${stats.deletions}` : null].filter(Boolean).join(' · ') || 'unknown';
    facts(body, [['Repository', p.repository || `${p.repository_id || 'unknown'} (GitHub access not confirmed)`], ['Base branch', p.base_branch || 'unknown'], ['Head branch', `${p.head_branch || 'session branch'} (published as a new branch)`], ['Changes', changes], ['Policy', human(p.policy)]]);
    const observed = p.base_observation && typeof p.base_observation === 'object' ? p.base_observation : {};
    if (observed.diverged === true) body.append(el('p', `The base branch has moved since the session started${typeof observed.sha === 'string' ? ` (now ${observed.sha.slice(0, 12)})` : ''}. The commit keeps the session base as its parent; nothing is rebased.`, 'session-warning'));
    else if (observed.diverged === false) body.append(el('p', 'The base branch has not moved since the session started.', 'detail-muted'));
    const paths = Array.isArray(p.changed_paths) ? p.changed_paths.filter(x => typeof x === 'string') : [];
    const list = el('ul', undefined, 'publication-paths'); for (const path of paths) list.append(el('li', path));
    if (!paths.length) body.append(el('p', 'No changed paths reported; nothing would be published.'));
    else if (paths.length <= 6) { body.append(el('h4', `Changed paths (${paths.length})`), list); }
    else { const more = el('details'); const summary = el('summary', `${paths.length} changed paths`); summary.dataset.focus = 'paths'; more.append(summary, list); body.append(more); }
    const omitted = Array.isArray(p.omitted_paths) ? p.omitted_paths.filter(x => typeof x === 'string') : [];
    if (omitted.length) {
      body.append(el('h4', `Omitted generated paths (${omitted.length})`), el('p', 'These generated artifacts will not be published.', 'detail-muted'));
      const omittedList = el('ul', undefined, 'publication-paths');
      for (const path of omitted.slice(0, 20)) omittedList.append(el('li', path.slice(0, 300)));
      body.append(omittedList);
      if (omitted.length > 20) body.append(el('p', `${omitted.length - 20} more omitted paths.`, 'detail-muted'));
    }
    renderChecks(body, p.checks);
    const reasons = Array.isArray(p.blocking_reasons) ? p.blocking_reasons : [];
    if (reasons.length || p.publishable !== true) {
      const box = el('section', undefined, 'publication-blockers'); box.setAttribute('aria-label', "Can't publish yet"); box.append(el('h4', "Can't publish yet"));
      for (const code of reasons) { const item = el('div'); explainCode(item, code); box.append(item); }
      if (!reasons.length) box.append(el('p', 'The server did not mark this change publishable.'));
      box.append(el('p', 'After fixing these, choose Preview again.', 'detail-muted')); body.append(box);
    }
    const form = el('form', undefined, 'publication-form'); body.append(form);
    const titleLabel = el('label', 'Pull request title'); const titleInput = el('input'); titleInput.type = 'text'; titleInput.maxLength = 256; titleInput.required = true; titleInput.value = draft.title; titleInput.dataset.focus = 'title'; titleLabel.append(titleInput);
    const bodyLabel = el('label', 'Pull request description'); const bodyInput = el('textarea'); bodyInput.maxLength = 65536; bodyInput.rows = 8; bodyInput.value = draft.body; bodyInput.dataset.focus = 'body'; bodyLabel.append(bodyInput);
    // After an unknown outcome the exact request must be resent, so its fields are frozen until a new preview.
    titleInput.readOnly = bodyInput.readOnly = unknown;
    titleInput.addEventListener('input', () => { draft.title = titleInput.value; submit.disabled = busy || blocked() || !draft.title.trim(); });
    bodyInput.addEventListener('input', () => { draft.body = bodyInput.value; });
    form.append(titleLabel, bodyLabel);
    showNotice(form);
    const actions = el('div', undefined, 'publication-actions'); form.append(actions);
    const submit = el('button', busy ? 'Publishing…' : unknown ? 'Publish again (same request)' : 'Publish draft PR', 'button publication-primary'); submit.type = 'submit'; submit.dataset.focus = 'publish'; actions.append(submit);
    submit.disabled = busy || blocked() || !draft.title.trim();
    if (blocked()) submit.setAttribute('aria-describedby', 'publication-blocked-note');
    const again = button(actions, 'Preview again', b => startPreview(b)); again.dataset.focus = 'preview-again'; again.disabled = busy;
    const cancel = button(actions, 'Cancel', () => { if (busy) return; preview = null; key = null; unknown = false; notice = null; mode = 'idle'; announce('Draft pull request review closed.'); render('create', true); }); cancel.dataset.focus = 'cancel'; cancel.disabled = busy;
    const note = el('p', blocked() ? 'Publishing is disabled until the issues above are fixed.' : 'Creates one new branch and one draft pull request. Nothing is merged.', 'detail-muted'); note.id = 'publication-blocked-note'; form.append(note);
    form.addEventListener('submit', e => { e.preventDefault(); publish(); });
  }
  function follow() {
    const p = pub, s = p.state;
    const title = el('h3', s === 'published' ? `Draft PR #${p.pull_request_number ?? '?'}` : s === 'no_changes' ? 'Nothing to publish' : s === 'blocked' || s === 'failed' ? "Couldn't publish the draft pull request" : s === 'reconciling' ? 'Checking GitHub…' : 'Publishing draft pull request');
    title.setAttribute('tabindex', '-1'); title.dataset.focus = 'progress'; body.append(title);
    if (s === 'published') {
      const number = Number.isSafeInteger(p.pull_request_number) && p.pull_request_number > 0 ? p.pull_request_number : null;
      if (number && typeof p.pull_request_url === 'string' && SAFE_PR_URL.test(p.pull_request_url)) {
        const a = el('a', `Draft PR #${number}`, 'publication-pr-link'); a.setAttribute('href', p.pull_request_url); a.setAttribute('target', '_blank'); a.setAttribute('rel', 'noopener noreferrer'); a.dataset.focus = 'pr-link';
        const line = el('p'); line.append(a, el('span', ' (opens GitHub in a new tab)', 'detail-muted')); body.append(line);
      } else body.append(el('p', 'GitHub confirmed the draft pull request, but its address could not be verified, so no link is shown. Open it from the repository on GitHub.', 'session-warning'));
      body.append(el('p', `Status on GitHub: ${p.pull_request_state ? human(p.pull_request_state) : 'unknown'}. OpenDevEngine never merges; review and merge on GitHub.`));
      const refreshBtn = button(body, busy ? 'Refreshing…' : 'Refresh status', b => refreshStatus(b)); refreshBtn.dataset.focus = 'refresh'; refreshBtn.disabled = busy;
    } else if (s === 'no_changes') {
      body.append(el('p', "The session's checkpoint has no file changes, so nothing was published: no branch, commit or pull request was created."));
    } else if (s === 'blocked' || s === 'failed') {
      explainCode(body, p.error_code, p.error_code ? undefined : s === 'failed' ? 'Publishing failed.' : 'Publishing was blocked.');
      body.append(el('p', 'Nothing was merged or force-pushed. A session has one publication intent; this one is final and kept for audit.', 'detail-muted'));
    } else if (s === 'reconciling') {
      body.append(el('p', "An earlier step's outcome on GitHub is unknown. OpenDevEngine never repeats it blindly; it first checks what GitHub recorded."));
      if (p.error_code) explainCode(body, p.error_code);
      const retryBtn = button(body, busy ? 'Retrying…' : 'Retry', b => retryPublication(b)); retryBtn.dataset.focus = 'retry'; retryBtn.disabled = busy;
    } else {
      const steps = el('ol', undefined, 'publication-steps'), at = PUBLICATION_STEPS.findIndex(([state]) => state === s);
      PUBLICATION_STEPS.forEach(([, label], i) => { const li = el('li', `${i < at ? 'Done' : i === at ? 'In progress' : 'Pending'} · ${label}`, i < at ? 'is-done' : i === at ? 'is-current' : 'is-pending'); if (i === at) li.setAttribute('aria-current', 'step'); steps.append(li); });
      body.append(steps, el('p', 'You can leave this page; publishing continues in the background.', 'detail-muted'));
    }
    showNotice(body);
    const checks = p.checks && typeof p.checks === 'object' ? p.checks : null;
    facts(body, [['Base branch', p.base_branch || 'unknown'], ['Head branch', p.head_branch || 'not yet assigned'], ['Commit', p.commit_sha || 'not yet created'], ['Local checks', !checks || checks.status === 'unavailable' ? 'unavailable' : checks.fully_validated === true ? 'all passed' : 'not fully validated'], ['Publication', p.id]]);
  }
  function stateText(p) {
    if (p.state === 'published') return `Draft PR #${p.pull_request_number ?? '?'} published${p.pull_request_state ? ` · ${human(p.pull_request_state)}` : ''}.`;
    if (p.state === 'reconciling') return 'Checking GitHub…';
    if (p.state === 'no_changes') return 'Nothing to publish: no file changes.';
    if (p.state === 'blocked' || p.state === 'failed') return `Publishing ${p.state === 'blocked' ? 'blocked' : 'failed'}: ${PUBLICATION_TEXT[p.error_code] || human(p.error_code)}`;
    return `Publishing: ${(PUBLICATION_STEPS.find(([state]) => state === p.state) || [, human(p.state)])[1]}…`;
  }
  function followPublication(value, focusKey = null, take = false) {
    const changed = !pub || pub.state !== value.state || pub.pull_request_state !== value.pull_request_state || pub.error_code !== value.error_code || pub.head_branch !== value.head_branch || pub.commit_sha !== value.commit_sha;
    pub = value; preview = null; key = null; unknown = false; draft = null;
    if (mode !== 'follow' || changed || focusKey) { mode = 'follow'; render(focusKey, take || undefined); }
    announce(stateText(pub)); schedule();
  }
  function schedule() {
    clearTimeout(timer);
    if (pub && !PUBLICATION_DONE.has(pub.state) && alive()) { timer = setTimeout(poll, pollMs); timer?.unref?.(); }
  }
  async function poll() {
    if (!alive() || !pub) return;
    try {
      const value = await read(`/api/publications/${enc(pub.id)}`);
      if (!alive()) return;
      if (!validPublication(value) || value.id !== pub.id) throw new Error('Unsupported publication response.');
      if (!busy) followPublication(value); else schedule();
    } catch (e) { if (!alive()) return; announce(`Reconnecting to publication status… ${e.message}`); schedule(); }
  }
  async function recover(take = false) {
    if (recovering || dead) return; recovering = true; mode = 'loading'; notice = null; render(null, take || undefined);
    try {
      const value = await read(`/api/sessions/${enc(sessionID)}/publication`);
      if (!alive()) return;
      if (!validPublication(value)) throw new Error('Unsupported publication response.');
      followPublication(value, take ? 'progress' : null, take);
    } catch (e) {
      if (!alive()) return;
      // not_found means no intent yet; an unrouted 404 means this build has no publication API.
      if (e.code === 'not_found') { mode = 'idle'; render(take ? 'create' : null, take || undefined); scheduleRecheck(); }
      else if (e.status === 404) { mode = 'unavailable'; render(); }
      else { mode = 'recoverError'; notice = {message: e.message}; render(take ? 'check-again' : null, take || undefined); }
    } finally { recovering = false; }
  }
  // While no intent exists, recheck with bounded backoff: an automatic (on_success_draft) intent, or one made
  // in another tab, can appear after the first read. Stops on leave, once an intent exists, or at the limit.
  function scheduleRecheck() {
    clearTimeout(recheckTimer); recheckTimer = null;
    if (!alive() || pub || rechecks >= RECHECK_LIMIT || !['idle', 'review'].includes(mode)) return;
    const delay = Math.min(pollMs * 2 ** rechecks, pollMs * 32); rechecks += 1;
    recheckTimer = setTimeout(recheck, delay); recheckTimer?.unref?.();
  }
  async function recheck() {
    recheckTimer = null;
    if (!alive() || pub) return;
    if (busy || recovering || !['idle', 'review'].includes(mode)) { scheduleRecheck(); return; }
    try {
      const value = await read(`/api/sessions/${enc(sessionID)}/publication`);
      if (!alive() || pub) return;
      if (validPublication(value) && !busy) {
        notice = mode === 'review' ? {message: 'This session already has a publication intent, so the review was closed and its progress is shown instead.'} : null;
        followPublication(value); return;
      }
    } catch (e) {
      if (!alive()) return;
      if (e.status === 404 && e.code !== 'not_found') { mode = 'unavailable'; render(); return; }
    }
    scheduleRecheck();
  }
  async function startPreview(b) {
    if (busy || dead) return;
    const take = inside(section, document.activeElement) || document.activeElement === b;
    busy = true; notice = null; b.disabled = true; announce('Preparing a preview of the draft pull request…');
    try {
      const value = await request(`/api/sessions/${enc(sessionID)}/publication-preview`, 'POST', {});
      if (!alive()) return;
      if (!value || value.session_id !== sessionID || typeof value.diff_hash !== 'string' || !Number.isSafeInteger(value.intent_version)) throw new Error('Unsupported preview response.');
      // A new preview is a new review step: it gets a fresh idempotency key.
      preview = value; key = newPublicationKey(); unknown = false;
      draft = {title: typeof value.title === 'string' ? value.title : '', body: typeof value.body === 'string' ? value.body : ''};
      busy = false; mode = 'review';
      announce(blocked() ? "Preview ready. This change can't be published yet; see the reasons listed." : 'Preview ready. Review the details, then publish the draft pull request.');
      render('review', take);
    } catch (e) {
      if (!alive()) return;
      busy = false; preview = null; key = null; unknown = false;
      notice = {code: e.code, message: `Preview unavailable: ${e.message}.`};
      if (e.code === 'idempotency_conflict') { recover(take); return; }
      mode = 'idle'; announce(`Preview unavailable: ${PUBLICATION_TEXT[e.code] || e.message}`); render('create', take);
    } finally { if (alive()) busy = false; }
  }
  async function publish() {
    if (busy || dead || mode !== 'review' || blocked() || !draft.title.trim()) return;
    const take = inside(section, document.activeElement);
    busy = true; render('publish', take);
    announce('Publishing the draft pull request…');
    // Exactly the contract fields; the same key is reused for double clicks and unknown outcomes.
    const payload = {title: draft.title, body: draft.body, expected_diff_hash: preview.diff_hash, intent_version: preview.intent_version, idempotency_key: key};
    try {
      const value = await request(`/api/sessions/${enc(sessionID)}/pull-requests`, 'POST', payload);
      if (!alive()) return;
      if (!validPublication(value)) { const e = new Error('Publish outcome unknown.'); throw e; }
      busy = false; notice = null; followPublication(value, 'progress', take);
    } catch (e) {
      if (!alive()) return;
      busy = false;
      if (e.status === undefined) {
        unknown = true; notice = {message: 'Connection lost; the outcome is unknown. Publish again to reconcile: the same request key is resent, so a second pull request cannot be created.'};
        announce('Publish outcome unknown. Checking saved state…'); render('publish', take);
        try { const found = await read(`/api/sessions/${enc(sessionID)}/publication`); if (alive() && validPublication(found) && !busy) { notice = null; followPublication(found, 'progress', inside(section, document.activeElement) || take); } } catch {}
        return;
      }
      if (e.code === 'stale_diff' || e.code === 'idempotency_conflict') notice = {code: e.code, offerPreview: true};
      else notice = {code: e.code, message: e.message};
      announce(`Not published: ${PUBLICATION_TEXT[e.code] || e.message}`);
      render(e.code === 'stale_diff' || e.code === 'idempotency_conflict' ? 'notice-preview' : 'publish', take);
      if (e.code === 'idempotency_conflict') { try { const found = await read(`/api/sessions/${enc(sessionID)}/publication`); if (alive() && validPublication(found) && !busy) followPublication(found, 'progress', inside(section, document.activeElement)); } catch {} }
    }
  }
  async function retryPublication(b) {
    if (busy || dead || !pub) return;
    const take = inside(section, document.activeElement) || document.activeElement === b;
    // A fresh key per deliberate retry; only an unknown outcome resends the same key.
    busy = true; b.disabled = true; retryKey ||= newPublicationKey(); notice = null; announce('Checking GitHub before retrying…');
    try {
      const value = await request(`/api/publications/${enc(pub.id)}/retry`, 'POST', {idempotency_key: retryKey});
      if (!alive()) return;
      // An unsupported success body is an unknown outcome: keep the key so the next Retry resends it.
      if (!validPublication(value) || value.id !== pub.id) throw new Error('Retry outcome unknown. Retry again to reconcile with the same request');
      retryKey = null; busy = false; followPublication(value, 'progress', take);
    } catch (e) {
      if (!alive()) return;
      busy = false; if (e.status !== undefined) retryKey = null;
      notice = e.code ? {code: e.code} : {message: e.message};
      announce(e.code === 'reconciliation_pending' ? 'GitHub has not confirmed the earlier step yet.' : `Retry not started: ${PUBLICATION_TEXT[e.code] || e.message}`);
      render('retry', take); schedule();
    }
  }
  async function refreshStatus(b) {
    if (busy || dead || !pub) return;
    const take = inside(section, document.activeElement) || document.activeElement === b;
    busy = true; b.disabled = true; announce('Refreshing pull request status from GitHub…');
    try {
      const value = await request(`/api/publications/${enc(pub.id)}/refresh`, 'POST', {});
      if (!alive()) return;
      if (!validPublication(value) || value.id !== pub.id) throw new Error('Unsupported publication response.');
      busy = false; notice = null; pub = value; mode = 'follow'; render('refresh', take);
      announce(`Pull request status: ${value.pull_request_state ? human(value.pull_request_state) : 'unknown'}.`);
    } catch (e) { if (!alive()) return; busy = false; notice = {message: `Status refresh unavailable: ${e.message}`}; render('refresh', take); announce(notice.message); }
  }
  render();
  return {
    update(s) {
      const wasTerminal = session && terminal.has(session.state); session = s;
      if (!terminal.has(s.state)) { if (mode === 'waiting') render(); return; }
      if (!wasTerminal && mode === 'waiting') { seenUpdate = s.updated_at; recover(); return; }
      // A later session change (for example an automatic publication) restarts the bounded recheck.
      if (s.updated_at !== seenUpdate) { seenUpdate = s.updated_at; if (!pub && ['idle', 'review'].includes(mode)) { rechecks = 0; if (!recheckTimer) scheduleRecheck(); } }
    },
    sourceLost() { if (mode === 'review' && !busy) { preview = null; key = null; unknown = false; draft = null; mode = 'idle'; notice = {code: 'source_unavailable'}; render(); } },
    dispose() { dead = true; clearTimeout(timer); clearTimeout(recheckTimer); reads.abort(); },
  };
}

export function createSessionPanel({host, request = executionRequest, onAvailable = () => {}, pollMs = 2000, shell = null}) {
  let listControls = [];
  const panel = el('div', undefined, 'sessions-panel'); host.append(panel);
  let epoch = 0, timer, active = false, currentID = null, refreshBusy = false, current, cursor = 0, sourceAllowed = true, messageCursor = null;
  const events = new Map(), history = new Map(); let messageSending = false, targetEdited = false, pendingMessage, retryKey, listCursor, listItems = new Map(), listFilters;
  let diffSignature, checksSignature, lastChecks = null, lastDiff = null, lastConfirmed = null, plan = null, publicationArea = null, pubInfo = {mode: 'waiting', pub: null};
  let status, statusEphemeral = false, conn, reconnect, summary, header, titleNode, meta, phase, phaseRail, result, activity, patches, checks, progress, publication, messageRows, questionArea;
  let form, compose, composeLabel, targetLabel, ended, send, responseTo, stop, retry, prAction, moreEvents, moreMessages, listEmpty, selectTab = () => {};
  // Display names: plan titles and repository names replace raw identifiers wherever people scan.
  // Resolved titles (a string, or null for a valid plan without one) are kept apart from in-flight reads,
  // so a view opened while a read is pending awaits it instead of treating it as settled.
  const planTitles = new Map(), planReads = new Map(), repoNames = {map: new Map(), at: 0, pending: null};
  const planTitle = s => planTitles.get(s.plan_id) || null;
  const repoName = (id, full = false) => { const r = repoNames.map.get(id); return (full ? r?.full || r?.name : r?.name || r?.full) || id || 'unknown repository'; };
  const repos = s => Array.isArray(s.repositories) ? s.repositories : [];
  const primaryRepo = s => repos(s).find(r => r.role === 'write') || repos(s)[0] || null;
  function loadRepositoryNames() {
    if (repoNames.pending) return repoNames.pending;
    if (repoNames.at && Date.now() - repoNames.at < 300000) return Promise.resolve(false);
    repoNames.pending = readRepositoryPages(request, '/api/repositories?limit=100').then(({items}) => {
      const map = new Map();
      for (const r of items) if (typeof r?.id === 'string') map.set(r.id, {name: typeof r.name === 'string' && r.name ? r.name : null, full: typeof r.full_name === 'string' && r.full_name ? r.full_name : null});
      repoNames.map = map; repoNames.at = Date.now(); return true;
    }, () => { repoNames.at = Date.now(); return false; }).finally(() => { repoNames.pending = null; });
    return repoNames.pending;
  }
  function loadPlanTitles(items) {
    const wanted = [...new Set(items.map(s => s.plan_id).filter(id => typeof id === 'string' && !planTitles.has(id)))];
    return Promise.allSettled(wanted.map(id => {
      if (!planReads.has(id)) {
        // A failed read is not cached, so the next list load or reload tries again.
        planReads.set(id, request(`/api/plans/${enc(id)}`).then(p => { planTitles.set(id, p?.id === id && typeof p.title === 'string' && p.title.trim() ? p.title.trim() : null); }).finally(() => planReads.delete(id)));
      }
      return planReads.get(id);
    }));
  }
  // Names arrive after the first paint; the list and rail repaint once when they do.
  function resolveNames(items, token, paint) {
    Promise.allSettled([loadRepositoryNames(), loadPlanTitles(items)]).then(() => { if (live(token)) paint(); });
  }
  function publishRecent(items) {
    shell?.setRecent(items.map(s => { const b = phaseBadge(s), title = planTitle(s) || 'Saved plan', repo = primaryRepo(s);
      return {href: `#/sessions/${enc(s.id)}`, title, tooltip: title, meta: `${repo ? repoName(repo.repository_id) : 'no repository'} · attempt ${s.attempt ?? '?'} · ${b.label}`, attention: s.state === 'waiting_for_user'}; }), {heading: 'Recent sessions'});
  }
  // Ephemeral messages describe connection or state; a successful refresh clears them. Action outcomes stay.
  const say = (text, ephemeral = false) => { if (!status) return; statusEphemeral = ephemeral; if (status.textContent !== text) status.textContent = text; };
  function setConnection(state, detail = '', tip = '') {
    if (!conn) return;
    const label = {live: 'Live', connecting: 'Connecting…', reconnecting: 'Reconnecting…', offline: 'Offline'}[state];
    if (conn.dataset.state !== state || conn.dataset.detail !== detail) {
      conn.dataset.state = state; conn.dataset.detail = detail;
      conn.replaceChildren(el('span', undefined, 'session-live-dot'), el('span', label, 'session-live-label'));
      if (detail) conn.append(el('small', ` · ${detail}`, 'session-live-detail'));
      conn.firstChild.setAttribute('aria-hidden', 'true');
    }
    if (tip) conn.setAttribute('title', tip); else conn.removeAttribute('title');
    if (reconnect) reconnect.hidden = state === 'live' || state === 'connecting';
  }
  const live = token => active && epoch === token && /^#\/sessions(?:\/[^/]+)?$/.test(location.hash);
  const schedule = token => { clearTimeout(timer); if (live(token)) { timer = setTimeout(() => refresh(token), pollMs); timer?.unref?.(); } };
  function focusReplacement(root, render) {
    const focused = document.activeElement, key = inside(root, focused) ? focused?.dataset?.focus : null;
    render();
    if (key && (document.activeElement === document.body || !document.activeElement?.isConnected)) {
      const target = [...root.querySelectorAll('[data-focus]')].find(n => n.dataset.focus === key);
      if (target) target.focus(); else { const heading = root.querySelector('h2'); heading?.setAttribute('tabindex', '-1'); heading?.focus(); }
    }
  }
  // <details> keep the open/closed state the reader chose across repaints; new ones use their default.
  function keepOpen(root, render) {
    const state = new Map([...root.querySelectorAll('details')].map(n => [n.querySelector('summary')?.dataset.focus, n.open]));
    render();
    for (const n of root.querySelectorAll('details')) { const key = n.querySelector('summary')?.dataset.focus; if (state.has(key)) { if (state.get(key)) n.setAttribute('open', ''); else n.removeAttribute('open'); } }
  }
  const stopLocked = () => Boolean(!current || (terminal.has(current.state) && current.runner?.containment !== 'failed'));
  const retryLocked = () => Boolean(!current || !terminal.has(current.state) || current.successor_id);
  const sendLocked = () => Boolean(!sourceAllowed || messageSending || (current && !pendingMessage && (terminal.has(current.state) || current.stop_requested)));
  function clearSource(revoke = true) {
    events.clear(); history.clear(); cursor = 0; messageCursor = null; diffSignature = null; checksSignature = null; lastDiff = null;
    if (revoke) { pendingMessage = null; storage.set(`session-message-${currentID}`, null); }
    activity.replaceChildren(el('h2', 'Activity'), el('p', 'Activity details cleared under current source policy.', 'detail-muted'));
    messageRows.hidden = false; messageRows.replaceChildren(el('h2', 'Your messages'), el('p', 'Message content redacted.', 'detail-muted'));
    patches.replaceChildren(el('h2', 'Diff'), el('p', 'Source unavailable under current repository access or retention policy.'));
    checks.replaceChildren(el('h2', 'Checks'), el('p', 'Source-bearing check output cleared.'));
    progress.replaceChildren(el('h2', 'Progress'), el('p', 'Plan steps unavailable or redacted under current source policy.'));
    questionArea.replaceChildren(); if (revoke) { compose.value = ''; responseTo.value = ''; } send.disabled = true; compose.disabled = true; responseTo.disabled = true;
    if (current) renderResult();
    if (revoke) publicationArea?.sourceLost();
  }
  function renderSummary() {
    const s = current, x = s.execution || {}, u = s.usage || {}, outcome = s.outcome || {};
    const seconds = elapsedSeconds(s), elapsed = seconds === null ? 'unknown' : `${duration(seconds)}${s.finished_at ? '' : ' so far'}`;
    focusReplacement(summary, () => {
      const details = el('details', undefined, 'session-run-details'); const label = el('summary', 'Run details'); label.dataset.focus = 'run-details'; details.append(label);
      const facts = el('div'); details.append(facts);
      const open = summary.querySelector('details')?.open; if (open) details.setAttribute('open', '');
      summary.replaceChildren(details);
      const reportedTokens = u.tokens || u;
      const tokens = ['input_tokens', 'output_tokens', 'total_tokens'].filter(k => Number.isFinite(reportedTokens[k])).map(k => `${human(k)} ${reportedTokens[k]}`).join(' · ') || 'unknown';
      const cost = Number.isFinite(u.reported_cost_usd) ? `${u.reported_cost_usd} USD (reported)` : Number.isFinite(u.estimated_cost) ? `${u.estimated_cost} USD (${u.cost_status || 'estimated'})` : 'unknown';
      lines(facts, [['Attempt', `${s.attempt ?? 'unknown'} · ${s.id}`], ['Plan version', `${s.plan_version_id} · SHA256 ${s.content_hash}`], ['Elapsed time', elapsed], ['Execution profile', `${x.profile_id || 'unknown'} · Version ${x.version ?? 'unknown'} (${x.profile_version_id || 'unknown'})`], ['Agent', `${x.adapter || 'unknown'} · CLI ${x.cli_version || 'unknown'} · Model ${u.observed_model || u.model || [...events.values()].find(e => e.type === 'agent.summary' && e.payload?.kind === 'ready')?.payload.model || outcome.model || 'unknown'}`], ['Destination', x.destination ? `${x.destination.kind || 'unknown'} · ${x.destination.host || x.destination.disclosure || 'destination details unknown'}` : 'unknown'], ['Pinned image', x.image_id || 'unknown'], ['Resource limits', limitsText(s.limits)], ['Usage', tokens], ['Cost', cost], ['Containment', human(s.runner?.containment)], ['Outcome', human(outcome.result || outcome.status || outcome.code || outcome.reason || s.error_code || 'not yet recorded')], ['Stop reason', human(s.stop_reason || 'none')]]);
      for (const r of repos(s)) details.append(el('p', `${repoName(r.repository_id, true)} (${r.repository_id}) · ${r.role} · Base ${r.base_sha || 'unknown'} (${r.base_branch || 'unknown branch'}) · Session branch ${r.session_branch || 'not assigned'}`));
      details.append(el('p', `Capabilities: user questions ${s.capabilities?.ask_user ? 'available' : 'unavailable'} · Follow-ups ${s.capabilities?.follow_up ? 'available' : 'no proven delivery channel; messages may remain queued'} · Resume ${s.capabilities?.resume ? 'available' : 'unavailable'}`));
      if (Object.keys(s.instructions?.acknowledged_base || {}).length) details.append(el('p', 'Started on an older base (acknowledged)', 'session-warning'));
      details.append(el('p', `External source sharing at launch: ${x.allow_external_source === true ? 'explicitly permitted by the pinned profile; live workspace and repository policy still apply' : x.allow_external_source === false ? 'disabled' : 'unknown'}.`));
    });
    const b = phaseBadge(s, lastChecks, pubInfo.pub);
    phase.replaceChildren(badge(b.tone, b.label));
    renderHeader(); renderPhaseRail(); renderProgress(); renderPublication(); renderResult();
    shell?.setHeader({title: 'Session workspace', pills: [{text: b.label}]});
    // Stop stays available while work may still be running, including after a failed containment.
    stop.hidden = stop.disabled = stopLocked();
    const stopping = !terminal.has(s.state) && (s.state === 'stopping' || s.stop_requested);
    stop.textContent = stopping || s.runner?.containment === 'failed' ? 'Request stop again' : 'Stop session';
    retry.disabled = retryLocked(); retry.hidden = !terminal.has(s.state) || Boolean(s.successor_id);
    // A finished session keeps stop_requested; only unfinished work is described as still stopping.
    if (stopping) say(`Stop requested. Containment ${human(s.runner?.containment)}; work is not confirmed stopped until the server verifies termination.`, true);
  }
  function renderPhaseRail() {
    if (!phaseRail || !current) return;
    const state = current.state, phaseName = current.phase || '';
    let active = state === 'completed' ? 4 : state === 'validating' || phaseName === 'validation' ? 3 : state === 'queued' || state === 'provisioning' ? 1 : 2;
    // Failed terminal records often replace the useful phase with "failed"/"cancelled".
    // Use durable events to show only the last phase we can prove was reached.
    if (terminal.has(state) && state !== 'completed') {
      active = 1;
      for (const event of events.values()) {
        if (event.type === 'validation.started' || event.type === 'validation.finished') active = Math.max(active, 3);
        else if (event.type === 'tool.started' || event.type === 'tool.finished' || event.type === 'tool.denied' || event.type === 'checkpoint' || event.type === 'agent.summary' || (event.type === 'session.state' && event.payload?.state === 'running')) active = Math.max(active, 2);
      }
    }
    const failed = ['failed', 'blocked', 'interrupted'].includes(state);
    const stages = [['01', 'Prepare'], ['02', 'Implement'], ['03', 'Verify'], ['04', 'Review']];
    phaseRail.replaceChildren();
    for (let i = 0; i < stages.length; i++) {
      const item = el('div', undefined, `session-stage${i + 1 < active || state === 'completed' ? ' is-done' : ''}${i + 1 === active ? ' is-current' : ''}${terminal.has(state) && state !== 'completed' && i + 1 === active ? ' is-stopped' : ''}${failed && i + 1 === active ? ' is-failed' : ''}`);
      item.append(el('span', stages[i][0], 'session-stage-number'), el('span', stages[i][1], 'session-stage-label'));
      phaseRail.append(item);
    }
  }
  // Header: what this is, where it runs, which attempt, how long; the state's primary actions sit beside it.
  function renderHeader() {
    const s = current, version = plan?.version?.version;
    if (s.purged) {
      titleNode.textContent = 'Session';
      meta.replaceChildren(el('span', s.id), el('span', human(s.state)), el('span', `Plan version ${s.plan_version_id}`), el('p', `Content deleted on ${time(s.purged_at)}`, 'session-purge-status'));
      for (const r of repos(s)) meta.append(el('span', `${r.repository_id} · Base ${r.base_sha}`));
      const note = el('p', 'Start a new session from the ', 'detail-muted');
      if (s.plan_id) link(note, 'saved plan', `#/plans/${enc(s.plan_id)}/${enc(s.plan_version_id)}`);
      else link(note, 'saved plans in Ask', '#/ask');
      note.append('.'); meta.append(note);
      if (prAction) prAction.hidden = true;
      return;
    }
    if (titleNode.textContent !== (plan?.title || 'Session')) titleNode.textContent = plan?.title || 'Session';
    focusReplacement(meta, () => {
      meta.replaceChildren();
      const part = node => { if (meta.childNodes.length) meta.append(sep()); meta.append(node); return node; };
      for (const r of repos(s)) {
        if (s.instructions?.acknowledged_base?.[r.repository_id]) part(el('span', 'Started on an older base (acknowledged)', 'session-warning'));
        const where = el('span', undefined, 'session-meta-repo'); where.append(el('strong', repoName(r.repository_id, true)));
        const sha = shortSha(r.base_sha); if (r.base_branch || sha) { const base = el('code', ` ${r.base_branch || 'base'}${sha ? `@${sha}` : ''}`); if (r.base_sha) base.setAttribute('title', `Base ${r.base_sha}`); where.append(base); }
        where.setAttribute('title', r.repository_id); part(where);
      }
      const attempt = part(el('span', `Attempt ${s.attempt ?? '?'}`));
      if (s.predecessor_id) { attempt.append(' ('); link(attempt, 'previous', `#/sessions/${enc(s.predecessor_id)}`).setAttribute('aria-label', 'Previous attempt'); attempt.append(')'); }
      if (s.successor_id) { attempt.append(' · '); link(attempt, 'next attempt', `#/sessions/${enc(s.successor_id)}`).setAttribute('aria-label', 'Next attempt'); }
      const seconds = elapsedSeconds(s);
      if (seconds !== null) { const t = part(el('span', s.finished_at ? `Ran ${duration(seconds)}` : `Running ${duration(seconds)}`)); if (s.created_at) t.setAttribute('title', `Started ${time(s.started_at || s.created_at)}${s.finished_at ? ` · finished ${time(s.finished_at)}` : ''}`); }
      const origin = part(el('span', undefined, 'session-origin'));
      link(origin, `Plan ${version ? `v${version}` : 'version'}`, `#/plans/${enc(s.plan_id)}/${enc(s.plan_version_id)}`).setAttribute('aria-label', `Saved plan ${version ? `version ${version}` : 'version'}${plan?.title ? `: ${plan.title}` : ''}`);
      if (plan?.conversation_id) { origin.append(' · '); link(origin, 'conversation', `#/ask/${enc(plan.conversation_id)}`).setAttribute('aria-label', 'Back to the conversation'); }
    });
    if (terminal.has(s.state)) {
      const token = epoch, id = currentID;
      const action = button(meta, 'Delete session content', async b => {
        if (!live(token) || !window.confirm('Delete this session’s events, guidance messages, diffs, checkpoints and validation output? Session identity, state, dates, saved plan version, repository/base commit and PR identity stay. The PR on GitHub is not affected. Backups require separate handling.')) return;
        b.disabled = true;
        try {
          const deleted = await request(`/api/sessions/${enc(id)}/purge`, 'POST', {});
          if (!live(token) || currentID !== id) return;
          current = {id, state: current.state, plan_id: current.plan_id, plan_version_id: current.plan_version_id, repositories: repos(current).map(r => ({repository_id:r.repository_id,role:r.role,base_sha:r.base_sha})), purged:true, purged_at:deleted.purged_at, source_available:false};
          showPurged(); await refresh(token);
        } catch (e) { if (live(token)) { say(`Content deletion failed: ${e.code || e.message}`); b.disabled = false; } }
      });
      action.className = 'session-purge-action';
    }
    renderPrAction();
  }
  function renderPrAction() {
    if (!prAction || !current) return;
    const done = terminal.has(current.state), pub = pubInfo.pub;
    // Only a completed session offers to start a draft PR from the header; an existing one is always reachable.
    prAction.hidden = current.purged || !done || (!pub && (current.state !== 'completed' || !['idle', 'review'].includes(pubInfo.mode)));
    prAction.textContent = pub ? (pub.state === 'published' ? 'View draft PR' : 'Draft PR status') : 'Create draft PR…';
    prAction.classList.toggle('btn-primary', !pub && current.state === 'completed');
  }
  // Overall state for the plan steps; the agent never reports per-step completion, so steps are not ticked.
  function renderProgress() {
    const steps = sourceAllowed ? plan?.version?.content?.steps : null;
    progress.replaceChildren(el('h2', 'Progress'));
    if (current) {
      const s = current.state, overall = !terminal.has(s) ? (s === 'waiting_for_user' ? ['needs your input', 'warn'] : current.stop_requested || s === 'stopping' ? ['stopping', 'warn'] : ['running', 'info']) : s === 'completed' ? ['done', 'ok'] : s === 'cancelled' ? ['stopped', 'off'] : [human(s), 'danger'];
      const line = el('p', undefined, 'progress-overall'); line.append(badge(overall[1], overall[0]), el('span', `${Array.isArray(steps) ? plural(steps.length, 'step') : 'Plan steps'}${terminal.has(s) ? '' : ` · phase ${human(current.phase)}`}`)); progress.append(line);
    }
    if (!Array.isArray(steps)) { progress.append(el('p', plan === null || plan.loading ? 'Loading the saved plan…' : 'Plan steps unavailable or redacted under current source policy.')); return; }
    const list = el('ol', undefined, `session-steps${current && terminal.has(current.state) ? ' is-ended' : ''}`);
    for (const step of steps) list.append(el('li', step.title || 'Untitled step'));
    progress.append(list, el('p', 'The agent does not report per-step completion. Follow the activity and checks for confirmed progress.', 'detail-muted progress-note'));
  }
  function renderPublication() { if (current) publicationArea?.update(current); }
  // The result card: what happened in plain words, what to do next, and the agent's last message as text.
  function finalAgentMessage() {
    if (!sourceAllowed) return null;
    let found = null;
    for (const e of [...events.values()].sort((a, b) => a.seq - b.seq)) {
      const p = e.payload || {};
      if (e.type === 'agent.summary' && !p.redacted && (p.kind === 'result' || p.kind === 'assistant') && typeof p.text === 'string' && p.text.trim()) found = p.text;
    }
    return found;
  }
  function diffCounts() {
    if (lastDiff?.available && Array.isArray(lastDiff.files)) {
      const files = lastDiff.files, known = files.every(f => Number.isFinite(f.additions) && Number.isFinite(f.deletions));
      return {files: files.length, additions: known ? files.reduce((n, f) => n + f.additions, 0) : null, deletions: known ? files.reduce((n, f) => n + f.deletions, 0) : null};
    }
    const n = current?.outcome?.changed_files; return Number.isSafeInteger(n) ? {files: n, additions: null, deletions: null} : null;
  }
  function checksPhrase() {
    const c = lastChecks; if (!c) return {text: 'checks not loaded', tone: 'info'};
    const items = Array.isArray(c.items) ? c.items : [];
    if (c.fully_validated === true) return {text: items.length ? 'checks passed' : 'all recorded checks passed', tone: 'ok'};
    const failed = items.filter(i => ['failed', 'timeout', 'error'].includes(i.status)).length;
    if (failed) return {text: `${plural(failed, 'check')} failed`, tone: 'warn'};
    if (!items.length) return {text: 'no checks recorded', tone: 'warn'};
    return {text: 'checks not fully validated', tone: 'warn'};
  }
  function renderResult() {
    if (!current || current.purged || !terminal.has(current.state)) { if (!result.hidden || result.childNodes.length) { result.hidden = true; result.replaceChildren(); } return; }
    const s = current, o = s.outcome || {}, contained = s.runner?.containment === 'confirmed' || o.containment?.confirmed === true, lost = o.edits_since_last_checkpoint_may_be_lost === true;
    let headline, detail = [], next = null, code = null, tone = 'ok', offerRetry = false;
    if (s.state === 'completed') {
      const d = diffCounts(), c = checksPhrase(), pub = pubInfo.pub;
      if (d && d.files === 0) { headline = `Finished with no file changes; ${c.text}.`; next = 'Nothing to publish. Retry as a new attempt or amend the plan in Ask.'; tone = 'off'; }
      else {
        const stats = d ? `${plural(d.files, 'file')}${d.additions !== null ? `, +${d.additions} −${d.deletions}` : ''}` : 'see the Diff tab';
        headline = `Changes ready for review: ${stats}, ${c.text}.`; tone = c.tone === 'ok' ? 'ok' : 'warn';
        next = pub?.state === 'published' ? `Draft PR #${pub.pull_request_number ?? '?'} is on GitHub. Review it there; OpenDevEngine never merges.` : pub ? 'A draft pull request is in progress; see the PR tab.' : 'Review the diff and checks, then create a draft pull request.';
      }
    } else if (s.state === 'cancelled') {
      headline = `${s.stop_reason && s.stop_reason !== 'user_requested' ? `Stopped: ${SESSION_TEXT[s.stop_reason] || human(s.stop_reason)}` : 'Stopped by you'}; ${contained ? 'the container was removed.' : 'container removal is not confirmed yet.'}`;
      if (s.stop_reason && s.stop_reason !== 'user_requested') code = s.stop_reason;
      next = 'Retry as a new attempt to run the same saved plan again.'; tone = contained ? 'off' : 'danger';
    } else if (s.state === 'interrupted') {
      headline = SESSION_TEXT[s.error_code] && s.error_code !== 'runtime_interrupted' ? SESSION_TEXT[s.error_code] : 'OpenDevEngine stopped while this session ran.';
      detail.push(contained ? 'The container was cleaned up.' : 'Container cleanup is not confirmed.');
      if (lost) detail.push('Edits after the last checkpoint may be lost.');
      code = s.error_code || s.stop_reason || null; next = 'Retry as a new attempt to run the same saved plan again.'; tone = 'danger'; offerRetry = true;
    } else {
      const known = /^[a-z_]{1,80}$/.test(s.error_code || '') ? s.error_code : null;
      headline = `${s.state === 'blocked' ? 'This session was blocked' : 'This session failed'}: ${(known && SESSION_TEXT[known]) || (known ? `${human(known)}.` : 'no reason was recorded.')}`;
      if (!contained) detail.push(s.runner?.containment === 'failed' ? 'The container is not confirmed stopped; use Request stop again.' : 'Container cleanup is not confirmed.');
      if (lost) detail.push('Edits after the last checkpoint may be lost.');
      code = known; next = 'Fix the cause, then retry as a new attempt.'; tone = 'danger'; offerRetry = true;
    }
    // Once a newer attempt exists, pointing to it replaces the advice to retry.
    if (s.successor_id && s.state !== 'completed') next = null;
    const message = finalAgentMessage();
    focusReplacement(result, () => {
      result.hidden = false; result.className = `session-section session-result is-${tone}`;
      const h = el('h2', 'Result'); h.className = 'sr-only'; result.replaceChildren(h);
      result.append(el('p', [headline, ...detail].join(' '), 'session-result-headline'));
      if (next) result.append(el('p', next, 'session-result-next'));
      if (code) result.append(el('small', `Code: ${code}`, 'session-result-code'));
      if (offerRetry && !s.successor_id) { const b = button(result, 'Retry', retryAction); b.classList.add('btn', 'btn-primary'); b.dataset.focus = 'result-retry'; b.dataset.retry = '1'; b.disabled = retryLocked(); b.setAttribute('aria-label', 'Retry as a new attempt'); }
      if (s.successor_id) { const p = el('p', 'A newer attempt exists: ', 'session-result-next'); link(p, 'open the next attempt', `#/sessions/${enc(s.successor_id)}`); result.append(p); }
      if (message) {
        const box = el('figure', undefined, 'agent-message agent-final'); const cap = el('figcaption', "Agent's final message"); const body = renderMarkdown(message);
        box.append(cap, body); result.append(box);
      }
    });
  }
  function renderEvents() {
    focusReplacement(activity, () => keepOpen(activity, () => {
      activity.replaceChildren(el('h2', 'Activity'));
      const list = el('ol', undefined, 'timeline'); let group = null, setup = null, setupDone = false, lastAgentText = null;
      // The result card shows the final message in full; here it is clamped to a short preview.
      const finalText = current && terminal.has(current.state) ? finalAgentMessage() : null;
      const entry = (e, d) => {
        const li = el('li', undefined, `timeline-entry${d.tone ? ` is-${d.tone}` : ''}${d.message ? ' has-message' : ''}`); li.dataset.seq = e.seq;
        const mark = el('span', undefined, 'timeline-mark'); mark.setAttribute('aria-hidden', 'true');
        const when = el('time', Number.isFinite(e.created_at) ? relativeTime(e.created_at) : '', 'timeline-time');
        when.setAttribute('title', `${time(e.created_at)} · #${e.seq}`); if (Number.isFinite(e.created_at)) when.setAttribute('datetime', new Date(e.created_at).toISOString());
        li.append(mark, el('span', d.label, 'timeline-label'), when);
        if (d.message) { const box = el('div', undefined, `agent-message${d.agent ? '' : ' is-note'}${d.message === finalText ? ' is-clamped' : ''}`); box.append(renderMarkdown(d.message)); li.append(box); }
        return li;
      };
      const folded = (key, label, items, openByDefault = false) => {
        const details = el('details', undefined, 'activity-group'), title = el('summary', label); title.dataset.focus = key; if (openByDefault) details.setAttribute('open', '');
        const inner = el('ol', undefined, 'timeline'); inner.append(...items); details.append(title, inner);
        const li = el('li', undefined, 'timeline-group'); li.append(details); list.append(li);
      };
      const span = (first, last) => duration(Math.max(0, Math.round(((last.created_at || 0) - (first.created_at || 0)) / 1000)));
      // Start-up steps fold into one "Set up" group; consecutive tool steps and checkpoints into "Worked for …".
      const closeSetup = () => { if (!setup) return; folded('setup', `Set up · ${span(setup.first, setup.last)}${setup.model ? ` · ${setup.model}` : ''}`, setup.items, !setupDone && ['queued', 'provisioning'].includes(current?.state)); setup = null; };
      const close = () => {
        if (!group) return;
        const {items, first, last, paths, steps} = group;
        folded(`group-${first.seq}`, `Worked for ${span(first, last)} · ${steps} ${steps === 1 ? 'step' : 'steps'}${sourceAllowed && paths.size ? ` in ${paths.size} ${paths.size === 1 ? 'file' : 'files'}` : ''}`, items); group = null;
      };
      for (const e of [...events.values()].sort((a, b) => a.seq - b.seq)) {
        const d = describeEvent(e, sourceAllowed);
        if (!setupDone && (d.setup || d.checkpoint)) { setup ||= {items: [], first: e, last: e, model: null}; setup.items.push(entry(e, d)); setup.last = e; if (d.model) setup.model = d.model; continue; }
        if (!setupDone) { setupDone = true; closeSetup(); }
        if (d.work || (d.checkpoint && group)) {
          group ||= {items: [], first: e, last: e, paths: new Set(), steps: 0, started: false};
          group.items.push(entry(e, d)); group.last = e;
          // A started/finished pair is one step; a finished event without its start still counts once.
          if (e.type === 'tool.started' || e.type === 'tool.denied') { group.steps += 1; group.started = e.type === 'tool.started'; } else if (e.type === 'tool.finished') { if (!group.started) group.steps += 1; group.started = false; }
          if (d.path) group.paths.add(d.path); continue;
        }
        close();
        // The final result usually repeats the agent's last message; show that text once.
        if (d.result && d.message && d.message === lastAgentText) d.message = null;
        if (d.agent) lastAgentText = d.message;
        list.append(entry(e, d));
      }
      closeSetup(); close(); activity.append(list); if (!events.size) activity.append(el('p', 'No saved activity yet.', 'detail-muted'));
    }));
  }
  function renderMessages() {
    messageRows.replaceChildren(el('h2', 'Your messages'));
    for (const m of [...history.values()].sort((a,b) => a.created_at - b.created_at || a.id.localeCompare(b.id))) {
      const item = el('section', undefined, 'session-message');
      item.append(el('h3', `${m.question_id ? 'Answer to the agent' : 'Guidance'} · ${human(m.delivery)}`), el('p', sourceAllowed && typeof m.content === 'string' ? m.content : 'Content redacted'), el('p', `${time(m.created_at)}${m.reason ? ` · ${human(m.reason)}` : ''}`, 'detail-muted')); messageRows.append(item);
    }
    if (pendingMessage) messageRows.append(el('p', 'A send outcome is unknown. Retry the same content and target to reconcile; the client request key is preserved.', 'session-warning'));
    if (history.size) messageRows.append(el('p', 'Queued, delivered and acknowledged are distinct server states; queued does not mean the agent has read it.', 'detail-muted'));
    messageRows.hidden = !history.size && !pendingMessage;
  }
  function renderQuestion() {
    const q = current.pending_question;
    const selected = responseTo.value;
    responseTo.replaceChildren();
    const general = el('option', current.capabilities?.follow_up ? 'Guidance for the agent' : 'Guidance (delivery may be unsupported)'); general.value = ''; responseTo.append(general);
    const asking = q && sourceAllowed && !q.redacted;
    if (asking) {
      const option = el('option', "Answer to the agent's question"); option.value = q.id; responseTo.append(option);
      if (selected === q.id || (!targetEdited && !compose.value.trim())) responseTo.value = q.id;
      else responseTo.value = selected;
    } else responseTo.value = '';
    if (pendingMessage) {
      if (pendingMessage.question_id && ![...responseTo.querySelectorAll('option')].some(o => o.value === pendingMessage.question_id)) { const option = el('option', 'Reconcile reply to the original question'); option.value = pendingMessage.question_id; responseTo.append(option); }
      responseTo.value = pendingMessage.question_id || '';
    }
    const over = !pendingMessage && (terminal.has(current.state) || current.stop_requested);
    compose.disabled = messageSending || !sourceAllowed; compose.readOnly = Boolean(over);
    responseTo.disabled = compose.disabled || Boolean(pendingMessage);
    send.disabled = compose.disabled || Boolean(over);
    // The composer shows only while the session can take input; an unsent draft stays visible, read-only.
    form.hidden = Boolean(over) && !compose.value.trim();
    targetLabel.hidden = responseTo.querySelectorAll('option').length < 2;
    labelComposer();
    renderEnded(over);
    focusReplacement(questionArea, () => {
      questionArea.replaceChildren();
      if (!asking) return;
      const card = el('section', undefined, 'question-card'); questionArea.append(card);
      card.append(el('h2', 'The agent is asking'), el('p', q.prompt || 'Question text unavailable'));
      const choices = el('div', undefined, 'question-choices'); card.append(choices);
      // The session contract does not define offered choices yet; render them only when the server sends them.
      for (const choice of Array.isArray(q.options) ? q.options.filter(o => typeof o === 'string' && o.trim()) : []) {
        const b = button(choices, choice, b => sendMessage(b, choice, q.id, false)); b.dataset.focus = `choice-${choice}`;
        b.disabled = send.disabled || Boolean(pendingMessage);
      }
      const reply = button(choices, 'Reply…', () => { if (compose.disabled) return; responseTo.value = q.id; targetEdited = true; labelComposer(); compose.focus(); }); reply.dataset.focus = 'reply';
      reply.disabled = send.disabled;
    });
  }
  function labelComposer() {
    const answering = Boolean(responseTo.value);
    const text = answering ? "Answer the agent's question" : 'Send guidance';
    if (composeLabel.textContent !== text) composeLabel.textContent = text;
    compose.setAttribute('placeholder', answering ? 'Type your answer…' : 'Guide the agent while it works…');
  }
  function renderEnded(over) {
    focusReplacement(ended, () => {
      const freshnessNotice = ended.querySelector('.base-advanced-notice');
      ended.replaceChildren(); ended.hidden = !over;
      if (!over) return;
      if (freshnessNotice && !current.successor_id) ended.append(freshnessNotice);
      const s = current, text = terminal.has(s.state) ? 'This session has ended.' : 'Stop requested; the agent no longer takes messages.';
      ended.append(el('span', `${text}${!form.hidden ? ' Your unsent text is kept below, read-only.' : ''}`));
      const cardRetry = ['interrupted', 'failed', 'blocked'].includes(s.state);
      if (terminal.has(s.state) && !s.successor_id && !cardRetry) { const b = button(ended, 'Retry', retryAction); b.dataset.focus = 'ended-retry'; b.dataset.retry = '1'; b.disabled = retryLocked(); b.setAttribute('aria-label', 'Retry as a new attempt'); }
    });
  }
  function sendMessage(b, content, question_id, fromComposer) {
    return mutate(b, async t => {
      if (!content) throw new Error('Enter a message.');
      if (pendingMessage && (pendingMessage.content !== content || (pendingMessage.question_id || null) !== question_id)) throw new Error('Retry the unchanged pending message and target to reconcile its unknown outcome.');
      pendingMessage ||= {content, question_id, client_request_id:globalThis.crypto.randomUUID()}; storage.set(`session-message-${currentID}`, pendingMessage);
      if (!fromComposer) compose.value = content;
      const submittedAttempt = pendingMessage, openedID = currentID; messageSending = true; renderQuestion();
      try { const result = await request(`/api/sessions/${enc(currentID)}/messages`, 'POST', submittedAttempt); if (!live(t)) return; if (!result.id || !result.delivery) throw new Error('Message outcome unknown; retry the same request.'); history.set(result.id,{...result,content:sourceAllowed ? submittedAttempt.content : null}); pendingMessage = null; storage.set(`session-message-${currentID}`, null); compose.value = ''; targetEdited = false; renderMessages(); say(`Message ${human(result.delivery)}${result.reason ? ` · ${human(result.reason)}` : ''}.`); }
      catch(e) { if (live(t) && e.status >= 400 && e.status < 500) { pendingMessage = null; storage.set(`session-message-${openedID}`, null); } throw e; }
      finally { if (live(t)) { messageSending = false; renderQuestion(); } }
    });
  }
  // One unified diff as lines: +/− stay in the text, so colour is never the only signal.
  function diffBlock(text) {
    const pre = el('pre', undefined, 'diff-lines'), code = el('code');
    for (const line of String(text).replace(/\n$/, '').split('\n')) {
      const kind = /^(\+\+\+|---) /.test(line) ? 'meta' : line.startsWith('@@') ? 'hunk' : line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : line.startsWith('\\') ? 'meta' : 'ctx';
      code.append(el('span', `${line}\n`, `diff-line diff-${kind}`));
    }
    pre.append(code); return pre;
  }
  const REJECT_TEXT = {excluded_generated: 'generated file, left out', excluded_binary: 'binary file, left out', too_large: 'too large to keep', excluded_path: 'excluded by path policy', symlink: 'symbolic link, left out', outside_scope: 'outside the plan’s writable scope'};
  function renderDiff(data) {
    const signature = JSON.stringify([sourceAllowed, data]); if (signature === diffSignature) return; diffSignature = signature;
    lastDiff = sourceAllowed && data?.available ? data : null;
    focusReplacement(patches, () => keepOpen(patches, () => {
      patches.replaceChildren(el('h2', 'Diff'));
      if (!sourceAllowed || !data.available) { patches.append(el('p', `Diff unavailable: ${human(!sourceAllowed ? 'source_unavailable' : data.reason || 'not_captured')}.`)); return; }
      const files = Array.isArray(data.files) ? data.files : [], known = files.every(f => Number.isFinite(f.additions) && Number.isFinite(f.deletions));
      const repo = repos(current || {}).find(r => r.repository_id === data.repository_id) || primaryRepo(current || {}), sha = shortSha(data.base_sha || repo?.base_sha);
      const totals = [plural(files.length, 'file'), files.length && known ? `+${files.reduce((n, f) => n + f.additions, 0)} −${files.reduce((n, f) => n + f.deletions, 0)}` : null, sha ? `base ${repo?.base_branch || 'branch'}@${sha}` : null].filter(Boolean).join(' · ');
      const total = el('p', totals, 'diff-totals'); if (data.base_sha) total.setAttribute('title', `Base ${data.base_sha}`); patches.append(total);
      if (data.edits_since_last_checkpoint_may_be_lost) patches.append(el('p', 'Edits since the last verified checkpoint may be lost.', 'session-warning'));
      if (data.hidden_by_current_exclusions) patches.append(el('p', `${plural(data.hidden_by_current_exclusions, 'file')} hidden by current exclusions.`, 'detail-muted'));
      if (!files.length) patches.append(el('p', 'The available checkpoint contains no visible file changes.'));
      // Small diffs open by default; large ones start folded so the tab stays scannable.
      const lineCount = files.reduce((n, f) => n + (typeof f.diff === 'string' ? f.diff.split('\n').length : 0), 0), small = files.length <= 5 && lineCount <= 400;
      for (const file of files) {
        const details = el('details', undefined, 'diff-file'), title = el('summary'); title.dataset.focus = file.path;
        title.append(el('span', human(file.status), `diff-status is-${/^[a-z]+$/.test(file.status || '') ? file.status : 'unknown'}`), el('code', file.path, 'diff-path'));
        if (Number.isFinite(file.additions) && Number.isFinite(file.deletions)) title.append(el('span', `+${file.additions} −${file.deletions}`, 'diff-count'));
        if (small) details.setAttribute('open', '');
        details.append(title, file.binary ? el('p', 'Binary change; text diff unavailable', 'detail-muted') : typeof file.diff === 'string' && file.diff ? diffBlock(file.diff) : el('p', 'Text diff unavailable', 'detail-muted'));
        details.append(el('p', `Mode ${file.mode || file.new_mode || 'unknown'}${file.mode_changed ? ` (was ${file.old_mode || 'unknown'})` : ''} · ${file.bytes ?? file.size ?? 'unknown'} bytes`, 'detail-muted diff-file-meta'));
        patches.append(details);
      }
      const rejected = Array.isArray(data.rejected) ? data.rejected : [];
      if (rejected.length) {
        const box = el('section', undefined, 'diff-rejected'); box.setAttribute('aria-label', 'Left out of the diff');
        box.append(el('h3', `${plural(rejected.length, 'path')} left out`)); const list = el('ul');
        for (const r of rejected) { const li = el('li'); li.append(el('code', typeof r?.path === 'string' ? r.path : 'unknown path'), el('span', ` — ${REJECT_TEXT[r?.reason] || human(r?.reason || 'rejected by artifact policy')}`)); list.append(li); }
        box.append(list); patches.append(box);
      }
      const more = el('details', undefined, 'diff-details'), label = el('summary', 'Checkpoint details'); label.dataset.focus = 'diff-meta'; more.append(label);
      const metaBox = el('div'); more.append(metaBox); lines(metaBox, [['Base commit', data.base_sha || 'unknown'], ['Captured', time(data.captured_at)], ['Diff hash', data.diff_hash || 'unknown'], ['Artifact', data.artifact_id || 'unknown'], ['Visible files', String(files.length)]]);
      patches.append(more);
    }));
    renderResult();
  }
  function renderChecks(data) {
    const signature = JSON.stringify([sourceAllowed, data]); if (signature === checksSignature) return; checksSignature = signature;
    focusReplacement(checks, () => keepOpen(checks, () => {
      const items = Array.isArray(data.items) ? data.items : [];
      checks.replaceChildren(el('h2', 'Checks'), el('p', data.fully_validated === true ? `All recorded checks passed${items.length ? ` (${items.length} of ${items.length})` : ''}.` : 'Not fully validated. Review failed, unavailable and not-run checks.', data.fully_validated === true ? 'checks-summary is-ok' : 'checks-summary session-warning'));
      if (!items.length) checks.append(el('p', 'No validation runs recorded.'));
      for (const c of items) {
        const box = el('section', undefined, 'check-item');
        const command = sourceAllowed ? Array.isArray(c.command) ? c.command.join(' ') : c.command?.command || c.command?.argv?.join(' ') || c.command?.shell || 'Command unavailable' : 'Command redacted';
        const ran = Number.isFinite(c.started_at) && Number.isFinite(c.finished_at) ? duration((c.finished_at - c.started_at) / 1000) : null;
        const head = el('h3', undefined, 'check-head'); head.append(badge(c.status === 'passed' ? 'ok' : c.status === 'not_run' ? 'off' : c.status === 'unavailable' ? 'warn' : 'danger', human(c.status)), el('span', ` Check ${scalar(c.ordinal)}`), el('small', ` · ${c.required ? 'required' : 'optional'}${ran ? ` · ${ran}` : ''}${Number.isSafeInteger(c.exit_code) ? ` · exit ${c.exit_code}` : ''}`, 'detail-muted'));
        const cmd = el('pre', undefined, 'check-command'); cmd.append(el('code', command));
        box.append(head, cmd);
        if (c.status === 'not_run' && c.environment?.reason === 'not_approved') box.append(el('p', 'Skipped: not approved in the plan.', 'detail-muted'));
        if (sourceAllowed && typeof c.command?.expected === 'string' && c.command.expected) box.append(el('p', `Expected: ${c.command.expected}`, 'detail-muted'));
        box.append(el('h4', 'Output'), el('pre', !sourceAllowed || c.output_redacted ? 'Output redacted' : c.output ?? 'Output unavailable', 'check-output'));
        const env = c.environment || {}, more = el('details', undefined, 'check-env'), label = el('summary', 'Environment'); label.dataset.focus = `env-${scalar(c.ordinal)}`; more.append(label);
        more.append(el('p', `Image ${env.image_id || env.image || 'unknown'} · Network ${env.network || 'unknown'} · Working directory ${env.cwd || env.working_directory || 'unknown'} · Source ${human(c.source)} · Exit ${c.exit_code ?? 'not recorded'} · ${time(c.started_at)} → ${time(c.finished_at)}`));
        box.append(more); checks.append(box);
      }
    }));
  }
  async function loadEvents(token) {
    const page = await request(`/api/sessions/${enc(currentID)}/events?after=${cursor}&limit=200`);
    if (!live(token)) return;
    if (!Array.isArray(page.items) || !Number.isSafeInteger(page.next_seq) || page.next_seq < cursor) throw new Error('Unsupported event replay response.');
    if (page.source_available === false) { if (sourceAllowed) clearSource(); sourceAllowed = false; }
    for (const e of page.items) if (e.session_id === currentID && Number.isSafeInteger(e.seq) && e.seq > 0) events.set(e.seq, e);
    cursor = page.next_seq; renderEvents(); moreEvents.hidden = !page.has_more; renderResult();
  }
  async function loadMessages(token, append = false) {
    const page = await request(`/api/sessions/${enc(currentID)}/messages?limit=50${append && messageCursor ? `&cursor=${enc(messageCursor)}` : ''}`);
    if (!live(token)) return;
    if (!Array.isArray(page.items)) throw new Error('Unsupported message history response.');
    if (page.source_available === false) { if (sourceAllowed) clearSource(); sourceAllowed = false; }
    for (const m of page.items) history.set(m.id, m);
    if (append || messageCursor === null) messageCursor = page.next_cursor;
    moreMessages.hidden = !messageCursor;
    if (pendingMessage && page.items.some(m => m.client_request_id === pendingMessage.client_request_id)) { pendingMessage = null; storage.set(`session-message-${currentID}`, null); compose.value = ''; }
    renderMessages();
  }
  function showPurged() {
    sourceAllowed = false; plan = {}; lastChecks = null; clearSource();
    summary.replaceChildren(); result.replaceChildren(); publication.replaceChildren();
    ended.replaceChildren(); stop.hidden = stop.disabled = true; retry.hidden = retry.disabled = true;
    renderHeader(); say('');
  }
  async function refresh(token = epoch) {
    if (!live(token) || refreshBusy || !currentID) return;
    refreshBusy = true;
    try {
      const data = await request(`/api/sessions/${enc(currentID)}`);
      if (!live(token)) return;
      if (data.id !== currentID || typeof data.state !== 'string') throw new Error('Unsupported session response.');
      onAvailable(); current = data; lastConfirmed = Date.now();
      if (data.purged) {
        showPurged();
        return;
      }
      if (plan === null) { plan = {loading: true}; request(`/api/plans/${enc(data.plan_id)}?version_id=${enc(data.plan_version_id)}`).then(p => { if (!live(token) || current?.purged) return; plan = p?.id === data.plan_id && p.version?.id === data.plan_version_id ? p : {}; if (typeof plan.title === 'string' && plan.title.trim()) planTitles.set(plan.id, plan.title.trim()); renderSummary(); }, () => { if (live(token)) { plan = {}; renderProgress(); } }); }
      if (data.source_available === false) { if (sourceAllowed) clearSource(); sourceAllowed = false; }
      else if (!sourceAllowed) { sourceAllowed = true; events.clear(); history.clear(); cursor = 0; messageCursor = null; }
      renderSummary(); renderQuestion();
      const results = await Promise.allSettled([loadEvents(token), loadMessages(token), request(`/api/sessions/${enc(currentID)}/diff`), request(`/api/sessions/${enc(currentID)}/validations`)]);
      if (!live(token)) return;
      if (results.some(r => r.status === 'fulfilled' && r.value?.source_available === false)) { if (sourceAllowed) clearSource(); sourceAllowed = false; renderQuestion(); }
      if (results[3].status === 'fulfilled') { lastChecks = results[3].value; renderChecks(results[3].value); } else checks.replaceChildren(el('h2', 'Checks'), el('p', `Checks unavailable: ${results[3].reason.message}`));
      if (results[2].status === 'fulfilled') renderDiff(results[2].value); else renderDiff({available:false,reason:results[2].reason.code || 'request_failed'});
      renderSummary();
      const failure = results.find(r => r.status === 'rejected');
      const tip = `Saved activity through #${cursor}. Closing this page does not stop background work.`;
      // A partial failure is not "Live": the indicator degrades and Reconnect stays available.
      if (failure) setConnection('reconnecting', 'some saved state unavailable', tip); else setConnection('live', '', tip);
      if (failure) say(`Some saved state is unavailable: ${failure.reason.message}. Reconnect retries from event #${cursor}.`, true);
      else if (!sourceAllowed) say('Source unavailable. Previously displayed source has been cleared.', true);
      else if (statusEphemeral && (terminal.has(current.state) || !current.stop_requested)) say('');
    } catch (e) {
      if (!live(token)) return;
      // Source cannot be shown without a live policy check; the last confirmed state and phase stay visible.
      clearSource(false); renderProgress();
      if (e.status === undefined && lastConfirmed) { setConnection('reconnecting', `last update ${relativeTime(lastConfirmed)}`); say('Source details stay hidden until the connection is confirmed.', true); }
      else { setConnection('offline'); say(`Session unavailable: ${e.message}. Reconnect to inspect current saved state.`, true); }
    }
    finally { if (epoch === token) { refreshBusy = false; schedule(token); } }
  }
  async function mutate(b, fn) {
    if (!live(epoch)) return;
    const token = epoch, focused = document.activeElement === b;
    b.disabled = true;
    try { await fn(token); } catch (e) { if (live(token)) say(e.message); }
    finally { if (live(token)) { b.disabled = b === stop ? stopLocked() : b.dataset?.retry || b === retry ? retryLocked() : b === send ? sendLocked() : false; if (focused && (document.activeElement === document.body || !document.activeElement?.isConnected)) b.focus(); } }
  }
  // Retry reuses one idempotency key until the server gives a definite answer; every retry control shares it.
  const retryAction = (b, acknowledged_base) => mutate(b, async t => {
    const openedID = currentID; retryKey ||= globalThis.crypto.randomUUID(); storage.set(`session-retry-${currentID}`, retryKey);
    try { const answer = await request(`/api/sessions/${enc(currentID)}/retry`, 'POST', {idempotency_key:retryKey, ...(acknowledged_base ? {acknowledged_base} : {})}); if (!live(t)) return; if (!answer.session_id) throw new Error('Retry outcome unknown; retry the same request.'); location.hash = `#/sessions/${enc(answer.session_id)}`; }
    catch(e) {
      if (live(t) && e.code === 'base_advanced') {
        renderBaseAdvanced(ended, e.details || [], {nameFor: repoName,
          retainLabel: 'Retry on the saved base', regenerateLabel: 'Open plan',
          onRetain: ack => retryAction(b, ack),
          onRegenerate: () => { location.hash = `#/plans/${enc(current.plan_id)}`; },
          onError: error => say(error.message)});
        return;
      }
      if (live(t) && e.status >= 400 && e.status < 500) { retryKey = null; storage.set(`session-retry-${openedID}`, null); }
      // Another tab or an earlier click created the next attempt: reload so the page links to it.
      if (live(t) && e.code === 'retry_exists') { say(e.message); await refresh(t); return; }
      throw e; }
  });
  function filtersActive() { return Boolean(listFilters && (listFilters.query.value.trim() || listFilters.state.value || listFilters.repository.value || listFilters.date.value)); }
  function renderRepositoryFilter() {
    const select = listFilters?.repository; if (!select) return;
    const ids = [...new Set([...listItems.values()].flatMap(s => repos(s).map(r => r.repository_id)).filter(Boolean))].sort((a, b) => repoName(a).localeCompare(repoName(b)));
    const chosen = select.value; select.replaceChildren();
    const all = el('option', 'All repositories'); all.value = ''; select.append(all);
    for (const id of ids) { const option = el('option', repoName(id, true)); option.value = id; select.append(option); }
    select.value = ids.includes(chosen) ? chosen : '';
  }
  function renderList() {
    const query = (listFilters?.query.value || '').trim().toLowerCase(), state = listFilters?.state.value || '', repo = listFilters?.repository.value || '', date = listFilters?.date.value ? new Date(listFilters.date.value).getTime() : null;
    let visible = 0;
    focusReplacement(activity, () => {
      activity.replaceChildren();
      for (const s of listItems.values()) {
        const ids = repos(s).map(r => r.repository_id), title = planTitle(s) || 'Saved plan';
        const haystack = `${title} ${s.id} ${s.plan_version_id} ${s.phase} ${s.state} ${ids.join(' ')} ${ids.map(id => `${repoName(id)} ${repoName(id, true)}`).join(' ')}`.toLowerCase();
        if ((query && !haystack.includes(query)) || (repo && !ids.includes(repo)) || (state === 'active' && terminal.has(s.state)) || (state === 'terminal' && !terminal.has(s.state)) || (date !== null && s.created_at < date)) continue;
        ++visible;
        const b = phaseBadge(s), row = el('li', undefined, `session-row${s.state === 'waiting_for_user' ? ' needs-input' : ''}`);
        const top = el('div', undefined, 'session-row-top'); top.append(badge(b.tone, b.label));
        const a = link(top, title, `#/sessions/${enc(s.id)}`); a.className = 'session-row-title'; a.setAttribute('title', `${title} · ${s.id}`); a.dataset.focus = `row-${s.id}`;
        const facts = el('p', undefined, 'session-row-meta'), part = node => { if (facts.childNodes.length) facts.append(sep()); facts.append(node); return node; };
        const main = primaryRepo(s);
        if (main) part(el('span', repoName(main.repository_id), 'session-row-repo')).setAttribute('title', `${repoName(main.repository_id, true)} (${main.repository_id})`);
        part(el('span', `attempt ${s.attempt ?? '?'}`));
        if (Number.isFinite(s.created_at)) part(el('span', relativeTime(s.created_at))).setAttribute('title', `Created ${time(s.created_at)}`);
        if (s.purged) part(el('span', `Content deleted on ${time(s.purged_at)}`));
        const seconds = elapsedSeconds(s); if (seconds !== null) part(el('span', terminal.has(s.state) ? `ran ${duration(seconds)}` : `running ${duration(seconds)}`));
        const sha = shortSha(main?.base_sha); if (sha) part(el('code', `${main.base_branch || 'base'}@${sha}`)).setAttribute('title', `Base ${main.base_sha}`);
        row.append(top, facts); activity.append(row);
      }
    });
    listEmpty.replaceChildren(); listEmpty.hidden = true;
    if (!listItems.size) { listEmpty.hidden = false; listEmpty.append(el('p', 'No sessions yet. A session starts from a ready, saved plan.'), el('p', 'Ask a question about a repository, turn the answer into a plan, then start a session from it.', 'detail-muted')); }
    else if (!visible) { listEmpty.hidden = false; listEmpty.append(el('p', 'No loaded sessions match these filters.')); }
    say(filtersActive() && listItems.size ? `${visible} of ${plural(listItems.size, 'loaded session')} match.${listCursor ? ' Load more to search older sessions.' : ''}` : '');
  }
  async function list(token, append = false) {
    say('Loading sessions…');
    try {
      const page = await request(`/api/sessions?limit=25${append && listCursor ? `&cursor=${enc(listCursor)}` : ''}`);
      if (!live(token)) return;
      if (!Array.isArray(page.items)) throw new Error('Unsupported sessions response.'); onAvailable();
      if (!append) listItems = new Map();
      for (const s of page.items) listItems.set(s.id, s);
      const all = [...listItems.values()];
      publishRecent(all);
      listCursor = page.next_cursor; moreEvents.hidden = !listCursor; renderRepositoryFilter(); renderList();
      resolveNames(all, token, () => { publishRecent([...listItems.values()]); renderRepositoryFilter(); renderList(); });
    } catch (e) {
      if (!live(token)) return;
      if (e.status === 404) {
        // Nothing here can work without the session service; leave only the explanation and the way back.
        for (const control of listControls) control.remove();
        moreEvents.hidden = true; listEmpty.hidden = true;
        say("Sessions aren't available in this build. Plans you make in Ask are saved and can start a session once it is.");
      } else say(`Sessions unavailable: ${e.message}`);
    }
  }
  function toolbarField(parent, label, type, cls) {
    const wrap = el('label', undefined, `session-filter ${cls || ''}`.trim()); wrap.append(el('span', label, 'sr-only'));
    const input = el(type === 'select' ? 'select' : 'input'); if (type !== 'select') input.type = type; wrap.append(input); parent.append(wrap); return input;
  }
  function open() {
    clearTimeout(timer); ++epoch; refreshBusy = false; publicationArea?.dispose(); publicationArea = null; pubInfo = {mode: 'waiting', pub: null}; active = /^#\/sessions(?:\/[^/]+)?$/.test(location.hash); if (!active) return;
    const token = epoch; try { currentID = location.hash.startsWith('#/sessions/') ? decodeURIComponent(location.hash.slice(11)) : null; } catch { currentID = null; }
    const openedID = currentID;
    panel.replaceChildren(); panel.classList.toggle('session-detail', location.hash.startsWith('#/sessions/')); panel.classList.toggle('session-index', !location.hash.startsWith('#/sessions/'));
    status = el('p', '', 'session-status'); status.setAttribute('role', 'status'); statusEphemeral = false; conn = null; reconnect = null;
    events.clear(); history.clear(); cursor = 0; messageCursor = null; sourceAllowed = true; current = null; messageSending = false; targetEdited = false; diffSignature = null; checksSignature = null; lastDiff = null;
    if (!currentID) {
      const bar = el('form', undefined, 'session-toolbar'); bar.setAttribute('role', 'search'); bar.setAttribute('aria-label', 'Filter sessions');
      const query = toolbarField(bar, 'Search sessions', 'search', 'is-search'); query.setAttribute('placeholder', 'Search plans, repositories, ids…');
      const state = toolbarField(bar, 'Session state', 'select'); for (const [value, text] of [['', 'All states'], ['active', 'Active'], ['terminal', 'Finished']]) { const option = el('option', text); option.value = value; state.append(option); }
      const repository = toolbarField(bar, 'Repository', 'select');
      const more = el('details', undefined, 'session-filter-more'); const moreLabel = el('summary', 'Date'); moreLabel.setAttribute('aria-label', 'Date filter'); more.append(moreLabel); bar.append(more);
      const dateWrap = el('label', 'Created on or after', 'session-filter is-date'); const date = el('input'); date.type = 'date'; dateWrap.append(date); more.append(dateWrap);
      const reload = button(bar, 'Reload', () => list(token)); reload.classList.add('btn', 'btn-ghost', 'session-reload'); reload.setAttribute('aria-label', 'Reload sessions');
      const top = el('div', undefined, 'session-index-top'); top.append(bar); const ask = link(top, 'Create a plan in Ask', '#/ask'); ask.className = 'session-ask-link';
      panel.append(top, status); listControls = [bar];
      listFilters = {query, state, repository, date}; bar.addEventListener('input', renderList); bar.addEventListener('change', renderList); bar.addEventListener('submit', e => e.preventDefault());
      activity = el('ul', undefined, 'session-list'); activity.setAttribute('aria-label', 'Sessions'); listEmpty = el('div', undefined, 'session-empty'); listEmpty.hidden = true;
      panel.append(activity, listEmpty); moreEvents = button(panel, 'More sessions', () => list(token, true)); moreEvents.classList.add('btn'); moreEvents.hidden = true; list(token); return;
    }
    plan = null; lastChecks = null; lastConfirmed = null;
    // Header spans the page; below it the thread (main) and a details pane the page owns so it cannot be dismissed.
    header = el('header', undefined, 'session-header');
    const crumb = el('p', undefined, 'session-crumb'); link(crumb, '← All sessions', '#/sessions'); header.append(crumb);
    const titleRow = el('div', undefined, 'session-title-row'); titleNode = el('h2', 'Session', 'session-title'); titleNode.setAttribute('tabindex', '-1');
    phase = el('span', undefined, 'session-phase'); conn = el('span', undefined, 'session-live'); conn.setAttribute('role', 'status');
    titleRow.append(titleNode, phase, conn); meta = el('p', undefined, 'session-meta'); header.append(titleRow, meta);
    const nav = el('div', undefined, 'session-actions'); header.append(nav);
    stop = button(nav, 'Stop session', b => mutate(b, async t => { const answer = await request(`/api/sessions/${enc(currentID)}/stop`, 'POST', {}); if (!live(t)) return; say(`Stop ${human(answer.state || 'requested')}. Waiting for verified process and container termination.`); await refresh(t); })); stop.disabled = true; stop.classList.add('btn', 'btn-danger', 'session-stop');
    stop.setAttribute('title', 'Stopping ends the agent and its container; saved checkpoints stay.');
    retryKey = storage.get(`session-retry-${currentID}`);
    retry = button(nav, 'Retry as a new attempt', retryAction); retry.disabled = true; retry.hidden = true; retry.classList.add('btn');
    prAction = button(nav, 'Create draft PR…', () => { selectTab(3); const area = publication; const target = area.querySelector('[data-focus="create"]') || area.querySelector('[data-focus="pr-link"]') || area.querySelector('h2'); target?.focus(); }); prAction.hidden = true; prAction.classList.add('btn'); prAction.setAttribute('aria-controls', 'session-tabpanel-3');
    reconnect = button(nav, 'Reconnect', () => refresh(token)); reconnect.classList.add('btn', 'btn-ghost'); reconnect.hidden = true;
    phaseRail = el('div', undefined, 'session-phase-rail'); phaseRail.setAttribute('aria-label', 'Session execution progress'); header.append(phaseRail, status);
    const main = el('div', undefined, 'session-main'), pane = el('aside', undefined, 'session-pane'); pane.setAttribute('aria-label', 'Session details');
    panel.append(header, main, pane); setConnection('connecting');
    result = el('section', undefined, 'session-section session-result'); result.hidden = true; result.setAttribute('aria-label', 'Result'); main.append(result);
    activity = el('section', undefined, 'session-section session-activity'); activity.append(el('h2', 'Activity'), el('p', 'Loading saved activity…', 'detail-muted')); main.append(activity);
    moreEvents = button(main, 'Load more activity', b => mutate(b, () => loadEvents(token))); moreEvents.hidden = true; moreEvents.classList.add('btn');
    messageRows = el('section', undefined, 'session-section session-messages'); messageRows.hidden = true; main.append(messageRows);
    moreMessages = button(main, 'More messages', b => mutate(b, () => loadMessages(token, true))); moreMessages.hidden = true; moreMessages.classList.add('btn');
    questionArea = el('div', undefined, 'session-question'); main.append(questionArea);
    ended = el('div', undefined, 'session-ended'); ended.hidden = true; main.append(ended);
    form = el('form', undefined, 'session-composer'); main.append(form);
    const label = el('label', undefined, 'session-composer-field'); composeLabel = el('span', 'Send guidance', 'session-composer-label'); compose = el('textarea'); compose.maxLength = 8000; compose.rows = 3; compose.setAttribute('placeholder', 'Guide the agent while it works…'); label.append(composeLabel, compose);
    targetLabel = el('label', undefined, 'session-composer-target'); targetLabel.append(el('span', 'Message target', 'detail-muted')); responseTo = el('select'); targetLabel.append(responseTo); targetLabel.hidden = true;
    responseTo.addEventListener('change', () => { targetEdited = true; labelComposer(); });
    form.append(label, targetLabel);
    const row = el('div', undefined, 'session-composer-actions'); form.append(row);
    row.append(el('p', 'Guidance stays within the saved plan; changing the goal, writable scope or grants needs an amended plan.', 'detail-muted'));
    send = el('button', 'Send', 'button btn btn-primary'); send.type = 'submit'; row.append(send); send.disabled = true;
    pendingMessage = storage.get(`session-message-${currentID}`); if (pendingMessage) compose.value = pendingMessage.content || '';
    form.addEventListener('submit', e => { e.preventDefault(); if (send.disabled || !form.isConnected || !live(token)) return; sendMessage(send, compose.value.trim(), responseTo.value || null, true); });
    const tabList = el('div', undefined, 'session-tabs'); tabList.setAttribute('role', 'tablist'); tabList.setAttribute('aria-label', 'Session details'); pane.append(tabList);
    progress = el('section', undefined, 'session-section'); patches = el('section', undefined, 'session-section session-side'); checks = el('section', undefined, 'session-section session-side'); publication = el('section', undefined, 'session-section');
    patches.append(el('h2', 'Diff')); checks.append(el('h2', 'Checks'));
    const tabs = [['Progress', progress], ['Diff', patches], ['Checks', checks], ['PR', publication]].map(([name, body], i) => {
      const tab = el('button', name, 'session-tab'); tab.type = 'button'; tab.id = `session-tab-${i}`; body.id = `session-tabpanel-${i}`;
      tab.setAttribute('role', 'tab'); tab.setAttribute('aria-controls', body.id); body.setAttribute('role', 'tabpanel'); body.setAttribute('aria-labelledby', tab.id);
      tabList.append(tab); pane.append(body); return [tab, body];
    });
    summary = el('div', undefined, 'session-run'); pane.append(summary);
    const select = i => tabs.forEach(([tab, body], j) => { tab.setAttribute('aria-selected', String(i === j)); tab.setAttribute('tabindex', i === j ? '0' : '-1'); body.hidden = i !== j; });
    selectTab = select;
    tabs.forEach(([tab], i) => {
      tab.addEventListener('click', () => select(i));
      tab.addEventListener('keydown', event => { const next = {ArrowRight: (i + 1) % tabs.length, ArrowLeft: (i + tabs.length - 1) % tabs.length, Home: 0, End: tabs.length - 1}[event.key]; if (next === undefined) return; event.preventDefault(); select(next); tabs[next][0].focus(); });
    });
    publicationArea = createPublicationArea({section: publication, sessionID: currentID, request, pollMs, isLive: () => live(token), onChange: info => {
      const before = pubInfo; pubInfo = info;
      if (!current || (before.mode === info.mode && before.pub?.state === info.pub?.state && before.pub?.pull_request_state === info.pub?.pull_request_state && before.pub?.pull_request_number === info.pub?.pull_request_number)) return;
      const b = phaseBadge(current, lastChecks, info.pub); phase.replaceChildren(badge(b.tone, b.label)); renderPrAction(); renderResult();
    }});
    select(0); renderProgress();
    request('/api/sessions?limit=25').then(page => { if (!live(token) || !Array.isArray(page?.items)) return; publishRecent(page.items); resolveNames(page.items, token, () => { publishRecent(page.items); if (current) renderHeader(); }); }, () => {});
    refresh(token);
  }
  window.addEventListener('hashchange', open);
  return {panel, open, refresh: () => refresh(epoch), close() { active = false; ++epoch; clearTimeout(timer); publicationArea?.dispose(); }, focus() { host.querySelector('h1')?.focus(); }};
}

const runnerFields = [
  ['memory_mib', 'Container memory (MiB)', 512, 128, 4096], ['workspace_mib', 'Workspace disk (MiB)', 64, 1, 1024], ['tmp_mib', 'Temporary disk (MiB)', 16, 1, 256], ['cpus', 'CPU cores', 1, 1, 4], ['pids', 'Process limit', 64, 16, 512], ['wall_seconds', 'Runner deadline (seconds)', 600, 10, 3600], ['max_files', 'Maximum files', 2000, 1, 20000], ['max_file_bytes', 'Maximum file size (bytes)', 262144, 1, 16777216], ['max_output_bytes', 'Maximum command output (bytes)', 65536, 1024, 1048576],
];
const agentFields = [
  ['wall_seconds', 'Agent deadline (seconds)', 180, 1, 3600], ['max_turns', 'Maximum agent turns', 12, 1, 40], ['max_budget_usd', 'Maximum spending (USD)', 1, 0, 10], ['max_output_bytes', 'Maximum agent output (bytes)', 524288, 1024, 1048576],
];
function field(parent, label, value, type = 'text') { const wrap = el('label', label, 'setting-field'); const input = el('input'); input.type = type; input.value = value ?? ''; wrap.append(input); parent.append(wrap); return input; }
function checkText(check) { return !check ? 'Not tested. A real bounded synthetic coding test is required.' : `Synthetic coding test: ${human(check.status)}${check.error_code ? ` · ${human(check.error_code)}` : ''} · Checked ${time(check.checked_at)} · CLI ${check.cli_version || 'unknown'} · Resolved image ${check.image_id || 'unknown'}`; }
// Settings → Coding agent: the commit identity for draft PRs (GET/PATCH /api/settings/publication).
// An unrouted 404 hides the card; the API reports a content-free `validation` code, mapped inline.
const AUTHOR_EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
function createPublicationIdentity(request) {
  const box = el('section', undefined, 'setting-box publication-identity'); box.hidden = true;
  let generation = 0, saving = false, dirty = false;
  const headingID = 'publication-identity-title';
  function fieldWithError(form, label, value, type, id) {
    const wrap = el('div', undefined, 'publication-identity-field');
    const input = field(wrap, label, value, type); input.id = `${id}-input`;
    const error = el('p', '', 'field-error'); error.id = `${id}-error`; error.setAttribute('aria-live', 'polite');
    input.setAttribute('aria-describedby', error.id); wrap.append(error); form.append(wrap);
    input.addEventListener('input', () => { dirty = true; if (error.textContent) { error.textContent = ''; input.removeAttribute('aria-invalid'); } });
    return [input, error];
  }
  function setError(input, error, text) { error.textContent = text; if (text) input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid'); }
  function render(value) {
    dirty = false;
    const title = el('h3', 'Pull request author'); title.id = headingID; box.setAttribute('aria-labelledby', headingID);
    box.replaceChildren(title, el('p', 'Draft pull request commits use this name and email as author and committer. OpenDevEngine never reads them from your local Git configuration.'));
    const form = el('form', undefined, 'settings-form'); form.noValidate = true; box.append(form);
    const [name, nameError] = fieldWithError(form, 'Commit author name', value?.commit_name || '', 'text', 'publication-identity-name'); name.maxLength = 100; name.setAttribute('autocomplete', 'name');
    const [email, emailError] = fieldWithError(form, 'Commit author email', value?.commit_email || '', 'email', 'publication-identity-email'); email.maxLength = 254; email.setAttribute('autocomplete', 'email');
    const save = el('button', 'Save pull request author', 'button'); save.type = 'submit'; form.append(save);
    const status = el('p', value?.commit_name && value?.commit_email ? '' : 'Not set. Draft pull requests are blocked until an author is saved.', 'page-status'); status.setAttribute('role', 'status'); form.append(status);
    const check = () => {
      const n = name.value.trim(), m = email.value.trim();
      setError(name, nameError, n && n.length <= 100 ? '' : 'Enter the name to show as the commit author.');
      setError(email, emailError, m.length >= 3 && m.length <= 254 && AUTHOR_EMAIL.test(m) ? '' : 'Enter a valid email address, such as you@example.com.');
      return !nameError.textContent && !emailError.textContent;
    };
    form.addEventListener('submit', async e => {
      e.preventDefault(); if (saving || !form.isConnected) return;
      if (!check()) { status.textContent = 'Fix the highlighted fields.'; (nameError.textContent ? name : email).focus(); return; }
      // Fields are read-only while the save is in flight, so nothing typed can be silently overwritten.
      const sent = {commit_name: name.value.trim(), commit_email: email.value.trim()}, typed = [name.value, email.value];
      saving = true; save.disabled = true; name.readOnly = email.readOnly = true; form.setAttribute('aria-busy', 'true'); status.textContent = 'Saving pull request author…'; const token = generation;
      try {
        const saved = await request('/api/settings/publication', 'PATCH', sent);
        if (token !== generation) return;
        if (typeof saved?.commit_name !== 'string' || typeof saved?.commit_email !== 'string') throw new Error('Save outcome unknown. Save again to confirm');
        if (name.value === typed[0] && email.value === typed[1]) { name.value = saved.commit_name; email.value = saved.commit_email; dirty = false; status.textContent = 'Pull request author saved. It applies to draft pull requests published from now on.'; }
        else status.textContent = 'Pull request author saved. Newer edits in these fields are not saved yet.';
      } catch (error) {
        if (token !== generation) return;
        if (error.code === 'validation' || error.code === 'bad_request') {
          // The server does not say which field failed; point at whichever one our rules flag, else both.
          if (check()) { setError(name, nameError, 'OpenDevEngine rejected this name. Use plain text without control characters.'); setError(email, emailError, 'OpenDevEngine rejected this email. Use a single plain address.'); }
          status.textContent = 'Not saved. Fix the highlighted fields.'; name.focus();
        } else status.textContent = error.status === undefined ? `${error.message} Save again to confirm the author.` : `Not saved: ${error.message}.`;
      } finally { if (token === generation) { saving = false; save.disabled = false; name.readOnly = email.readOnly = false; form.removeAttribute('aria-busy'); } }
    });
  }
  async function load() {
    // Typed, unsaved values survive a background reload of the Coding agent section.
    if (saving || (dirty && box.isConnected && !box.hidden)) return;
    const token = ++generation;
    try { const value = await request('/api/settings/publication'); if (token !== generation) return; box.hidden = false; render(value); }
    catch (e) {
      if (token !== generation) return; saving = false;
      if (e.status === 404) { box.hidden = true; box.replaceChildren(); return; }
      box.hidden = false; box.replaceChildren(el('h3', 'Pull request author'), el('p', `Pull request author unavailable: ${e.message}. Reload execution settings to try again.`));
    }
  }
  return {element: box, load};
}
export function createExecutionSettings({request = executionRequest, onAvailable = () => {}, isVisible = () => true, pollMs = 2000} = {}) {
  const element = el('section', undefined, 'connected-panel execution-settings');
  const heading = el('h2', 'Execution profiles'); heading.setAttribute('tabindex', '-1');
  const status = el('p'); status.setAttribute('role','status');
  const rows = el('div'), editor = el('section', undefined, 'setting-box'), settingsHost = el('div'), identity = createPublicationIdentity(request);
  element.append(heading, status);
  button(element, 'Reload execution settings', () => refresh());
  element.append(el('p', 'Execution uses the installed Claude Code CLI with its normal CLI login. Sign in through that CLI on this machine before testing. Never paste CLI tokens, keys or authentication files here. Docker and a locally provisioned compatible image are required; runtime tests never install a toolchain or pull images automatically.'), el('p', 'Claude Code sends prompts and permitted source to its external model destination. Workspace local-only policy blocks this adapter, including synthetic tests. For repository execution, turn off local-only in Settings → Privacy and explicitly allow source sharing on this execution profile. Source consent does not grant repository access or publication permission.'), settingsHost, identity.element, rows, editor);
  let generation = 0, mutating = false, unknownWrite = false, pageCursor = null, profiles = new Map(), timer, pendingTests = new Map(), editorDirty = false;
  const current = token => token === generation;
  function preserveFocus(fn) { const restore = inside(element, document.activeElement); fn(); if (restore && isVisible() && (document.activeElement === document.body || !document.activeElement?.isConnected)) heading.focus(); }
  function schedule() { clearTimeout(timer); if (pendingTests.size) { timer = setTimeout(pollTests, pollMs); timer?.unref?.(); } }
  async function pollTests() {
    const token = generation;
    try {
      const observed = [];
      for (const [id, versionID] of pendingTests) {
        const version = await request(`/api/execution-profile-versions/${enc(versionID)}`);
        if (!current(token)) return;
        observed.push(`${versionID}: ${checkText(version.check)}`);
        const profile = profiles.get(id);
        if (profile?.active_version_id === versionID) { profile.check = version.check; profile.version.check = version.check; }
        if (!['queued','running'].includes(version.check?.status)) pendingTests.delete(id);
      }
      if (current(token)) { preserveFocus(renderRows); status.textContent = `${pendingTests.size ? 'Synthetic coding test is still running.' : 'Synthetic test finished; evidence covers only the checked immutable version.'} ${observed.join(' · ')}`; }
    } catch (e) { if (current(token)) status.textContent = `Test status unavailable: ${e.message}. Use Reload execution settings to reconnect; do not assume success.`; }
    finally { if (current(token)) schedule(); }
  }
  async function mutation(b, fn) {
    if (unknownWrite) { status.textContent = 'A previous write has an unknown outcome. Reload execution settings and inspect saved versions before another change.'; return; }
    if (mutating) return; mutating = true; ++generation; clearTimeout(timer); b.disabled = true; status.textContent = 'Saving execution settings…';
    const focused = inside(element, document.activeElement);
    try { await fn(); }
    catch(e) { unknownWrite = !e.status || e.status >= 500; status.textContent = unknownWrite ? `${e.message}. Reload execution settings and inspect saved versions before another change.` : e.message; }
    finally { mutating = false; b.disabled = false; if (focused && isVisible() && (document.activeElement === document.body || !document.activeElement?.isConnected)) heading.focus(); schedule(); }
  }
  function renderRows() {
    rows.replaceChildren();
    for (const profile of profiles.values()) {
      const v = profile.version, config = v.config || {}, box = el('section', undefined, 'setting-box');
      box.append(el('h3', profile.name), el('p', `${human(v.adapter)} · ${human(v.auth_mode)} · Version ${v.version} (${v.id})`), el('p', `Image ${config.image || 'unknown'} · External source ${config.allow_external_source ? 'allowed by this profile; workspace policy still applies' : 'sharing disabled'}`), el('p', limitsText(config)), el('p', checkText(profile.check)));
      const capabilities = profile.check?.capabilities || {};
      box.append(el('p', `Verified capabilities: ${Object.entries(capabilities).filter(([,v]) => v === true).map(([k]) => human(k)).join(', ') || 'none reported'}. Unreported capabilities are unavailable.`));
      button(box, `Edit execution profile ${profile.name}`, () => { if (!mutating && (!editorDirty || window.confirm('Discard unsaved execution profile edits?'))) { renderEditor(profile); editor.querySelector('input')?.focus(); } });
      const test = button(box, `Test execution profile ${profile.name}`, b => mutation(b, async () => {
        // No source or credentials are submitted by the UI. This is an explicit model invocation.
        const result = await request(`/api/execution-profiles/${enc(profile.id)}/test`, 'POST', {});
        if (!result.version_id || !result.job_id) throw new Error('Test outcome unknown. Reload saved profile checks before testing again.');
        pendingTests.set(profile.id, result.version_id); if (profile.active_version_id === result.version_id) profile.check = {status:result.status || 'queued'};
        preserveFocus(renderRows); status.textContent = `Synthetic coding test ${human(result.status || 'queued')} · Job ${result.job_id} · Immutable version ${result.version_id}. Polling its saved immutable-version check; usage and cost unknown.`;
      }));
      test.disabled = pendingTests.has(profile.id) || ['queued','running'].includes(profile.check?.status);
      button(box, `Delete execution profile ${profile.name}`, () => {
        if (mutating) return;
        const confirm = el('div'); confirm.append(el('p', 'Deletion prevents future use; immutable session history remains.'));
        button(confirm, `Confirm delete execution profile ${profile.name}`, b => mutation(b, async () => { await request(`/api/execution-profiles/${enc(profile.id)}`, 'DELETE', {}); profiles.delete(profile.id); pendingTests.delete(profile.id); preserveFocus(renderRows); status.textContent = 'Execution profile deleted. Existing history remains.'; }));
        button(confirm, 'Cancel execution profile deletion', () => { confirm.remove(); }); box.append(confirm);
      }); rows.append(box);
    }
    if (!profiles.size) rows.append(el('p', 'No execution profiles. Create one below, then run its synthetic coding test.'));
    if (pageCursor) button(rows, 'More execution profiles', () => refresh(true));
  }
  function renderEditor(profile = null) {
    editorDirty = false; const v = profile?.version, config = v?.config || {};
    editor.replaceChildren(el('h3', profile ? `Edit execution profile ${profile.name}` : 'Create execution profile'));
    const form = el('form', undefined, 'settings-form');
    const name = field(form, 'Execution profile name', profile?.name || ''); name.required = true; name.maxLength = 100;
    form.append(el('p', 'Adapter: Claude Code · Authentication: normal installed CLI login. API-key and managed authentication profiles are unavailable.'));
    const image = field(form, 'Locally provisioned container image (tag or digest)', config.image || ''); image.required = true; image.maxLength = 255;
    form.append(el('p', 'Choose an image already present in Docker with the tools your plan needs. Network is disabled inside the container. Missing dependencies produce unavailable checks; no host fallback is used. Memory must cover workspace disk + temporary disk + at least 64 MiB.'));
    const agentModel = field(form, 'Claude model (optional)', config.agent_model || ''); agentModel.maxLength = 80; agentModel.placeholder = 'Default from your Claude Code settings';
    form.append(el('p', 'Leave blank to use the model your Claude Code login defaults to, or enter an alias such as sonnet or opus, or a full model id. Saved with this immutable profile version.', 'field-hint'));
    const consent = field(form, 'Allow sending repository source to the external coding model', '', 'checkbox'); consent.checked = config.allow_external_source === true;
    const values = {};
    for (const [kind, title, specs] of [['runner_limits','Container resource limits',runnerFields], ['agent_limits','Agent limits',agentFields]]) {
      const group = el('fieldset'); group.append(el('legend', title)); values[kind] = {};
      for (const [key,label,def,min,max] of specs) { const input = field(group, label, config[kind]?.[key] ?? def, 'number'); input.required = true; input.min = String(min); input.max = String(max); input.step = key === 'max_budget_usd' ? 'any' : '1'; values[kind][key] = input; }
      form.append(group);
    }
    const save = el('button', profile ? 'Save execution profile version' : 'Create execution profile', 'button'); save.type = 'submit'; form.append(save);
    if (profile) button(form, 'Cancel execution profile edit', () => renderEditor());
    form.addEventListener('input', () => { editorDirty = true; }); form.addEventListener('change', () => { editorDirty = true; });
    form.addEventListener('submit', e => { e.preventDefault(); if (save.disabled || mutating || !form.isConnected) return;
      const runner_limits = Object.fromEntries(Object.entries(values.runner_limits).map(([k,input]) => [k,Number(input.value)]));
      const agent_limits = Object.fromEntries(Object.entries(values.agent_limits).map(([k,input]) => [k,Number(input.value)]));
      if (!Number.isFinite(agent_limits.max_budget_usd) || agent_limits.max_budget_usd <= 0) { status.textContent = 'Maximum spending must be a positive number up to 10 USD.'; return; }
      if (runner_limits.memory_mib < runner_limits.workspace_mib + runner_limits.tmp_mib + 64) { status.textContent = 'Increase container memory to cover workspace + temporary disk + 64 MiB.'; return; }
      mutation(save, async () => {
        const saved = await request(profile ? `/api/execution-profiles/${enc(profile.id)}` : '/api/execution-profiles', profile ? 'PATCH' : 'POST', {name:name.value.trim(),adapter:'claude_code',auth_mode:'cli_login',image:image.value.trim(),agent_model:agentModel.value.trim()||null,allow_external_source:consent.checked,runner_limits,agent_limits});
        if (!saved.id || !saved.version) throw new Error('Save outcome unknown. Reload saved execution profiles before creating another version.');
        profiles.set(saved.id, saved); preserveFocus(renderRows); renderEditor(); status.textContent = 'Execution profile version saved. Run its synthetic coding test before selecting it for a session.';
      });
    }); editor.append(form);
  }
  function renderSettings(settings, privacy) {
    settingsHost.replaceChildren(); const form = el('form', undefined, 'settings-form');
    form.append(el('p', `Workspace local-only: ${typeof privacy.settings?.local_only === 'boolean' ? privacy.settings.local_only ? 'enabled — remote execution and synthetic tests are blocked' : 'disabled — profile source consent is still required' : 'unknown — check Settings → Privacy before testing'}.`));
    const count = field(form, 'Maximum active implementation sessions', settings.max_active_sessions, 'number'); count.min = '1'; count.max = '4'; count.step = '1'; count.required = true;
    form.append(el('p', 'Applies to future launches; changing this limit does not terminate active work.'));
    const save = el('button', 'Save execution concurrency', 'button'); save.type = 'submit'; form.append(save);
    form.addEventListener('submit', e => { e.preventDefault(); if (!form.isConnected) return; mutation(save, async () => { const saved = await request('/api/settings/execution','PATCH',{max_active_sessions:Number(count.value)}); count.value = saved.max_active_sessions; status.textContent = 'Execution concurrency saved for future launches.'; }); }); settingsHost.append(form);
  }
  async function refresh(append = false) {
    if (mutating) { status.textContent = 'A settings mutation is pending. Wait before reloading saved profiles.'; return; }
    const token = ++generation; clearTimeout(timer); status.textContent = 'Loading execution settings…';
    if (!append) identity.load();
    try {
      const results = await Promise.allSettled([request(`/api/execution-profiles?limit=50${append && pageCursor ? `&cursor=${enc(pageCursor)}` : ''}`), request('/api/settings/execution'), request('/api/settings/model-roles')]);
      if (!current(token)) return;
      if (results[0].status === 'rejected') throw results[0].reason;
      const page = results[0].value;
      if (!Array.isArray(page.items)) throw new Error('Unsupported execution-profile list.'); onAvailable(); unknownWrite = false;
      if (!append) { profiles.clear(); pendingTests.clear(); }
      for (const profile of page.items) { profiles.set(profile.id,profile); if (['queued','running'].includes(profile.check?.status)) pendingTests.set(profile.id, profile.active_version_id); }
      pageCursor = page.next_cursor;
      preserveFocus(() => { renderRows(); if (results[1].status === 'fulfilled') renderSettings(results[1].value,results[2].status === 'fulfilled' ? results[2].value : {}); else settingsHost.replaceChildren(el('p', `Execution concurrency unavailable: ${results[1].reason.message}`)); if (!editorDirty) renderEditor(); });
      status.textContent = 'Execution profiles loaded. Cached checks are evidence for their immutable version; reads never start a model. Synthetic testing may incur model cost.';
    } catch(e) { if (current(token)) status.textContent = e.status === 404 ? 'Execution profiles are unavailable in this build. Model-provider setup remains separate.' : `Execution settings unavailable: ${e.message}`; }
    finally { if (current(token)) schedule(); }
  }
  return {element, refresh, close() { ++generation; clearTimeout(timer); }};
}
