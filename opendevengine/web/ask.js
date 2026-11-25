import { limitsText } from "./sessions.js";
import { createComposer } from "./composer.js";
import { renderThread } from "./thread.js";
import { renderPlanDocument, renderBaseAdvanced } from "./plan-view.js";
import { rememberRepositories } from "./repo-source.js";
import { relativeTime } from "./ui.js";
import { openSource } from "./code-viewer.js";
import { renderSetup } from "./setup.js";
// Persistent Ask consumes the frozen AskAPI; source and model output are always text.
const el = (tag, text) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; return n; };
const enc = encodeURIComponent;
// Advisory only: a model sometimes writes an English sentence where a validation command belongs.
const looksLikeProse = text => { const t = String(text || '').trim(); return /\.\s+[A-Za-z]/.test(t) || (t.endsWith('.') && t.split(/\s+/).length >= 5); };
const destinationText = d => d && typeof d === 'object' ? `${d.kind || 'unknown'}${d.host ? ` · ${d.host}` : d.disclosure ? ` · ${d.disclosure}` : ''}` : 'unknown';
const coverageText = c => c && typeof c === 'object' ? [Number.isFinite(c.files) ? `${c.files} files` : null, Number.isFinite(c.chunks) ? `${c.chunks} chunks` : null, c.mode ? c.mode.replaceAll('_',' ') : null, c.partial === true ? 'partial coverage' : null].filter(Boolean).join(' · ') || 'unknown' : 'unknown';

const NO_SHELL = {setHeader() {}, setRecent() {}};
const onAskRoute = () => /^(#?\/?|#\/(ask|plans)(\/.*)?)$/.test(location.hash);
export function titleFrom(text) {
  const clean = String(text).replace(/\s+/g, ' ').trim();
  if (clean.length <= 60) return clean;
  const cut = clean.slice(0, 60);
  return (cut.lastIndexOf(' ') > 30 ? cut.slice(0, cut.lastIndexOf(' ')) : cut) + '…';
}
export async function createAskPanel({host, request = http, onMount = () => {}, shell = NO_SHELL, sidePanel = null}) {
  let listing, initialHistoryError;
  try {
    listing = await request('/api/conversations');
    if (!Array.isArray(listing?.items)) throw new Error('Unsupported conversation history response.');
  } catch (error) {
    if (error.status === 404) return null; // Only a missing API uses legacy Ask.
    initialHistoryError = error;
  }
  const panel = el('main'); panel.className = 'persistent-ask';
  const title=el('h1','Ask · saved planning');title.id='persistent-ask-title';title.setAttribute('tabindex','-1');panel.setAttribute('aria-labelledby',title.id);
  const status = el('p'); status.setAttribute('role', 'status');
  const header = value => { if (onAskRoute()) shell.setHeader(value); };
  const knownNames = new Map(), scopeEvents = new Map(), staleScope = new Map(); let composerHandle = null;
  const pendingNotice=el('div');
  const body = el('div'); const source = el('aside'); source.setAttribute('aria-label', 'Snapshot source');
  const historyNotice = el('div'); historyNotice.className = 'history-notice';
  panel.append(title, status, historyNotice, pendingNotice, body, source); host.append(panel);
  let sourceEpoch=0, planEditor=null, planStart=null, planNotice=null, planDraft=false;
  const markPlanDirty = () => { planDraft=true; draft=true; if(planStart)planStart.disabled=true; if(planNotice)planNotice.textContent='Unsaved plan edits. Save a new immutable version or explicitly discard before starting.'; };
  let epoch = 0, current = null, draft = false, pending = false, timer, acceptedHash = location.hash;
  let sendAttempt = null;
  try { sendAttempt = JSON.parse(window.sessionStorage?.getItem('ask-pending-send') || 'null'); } catch {}
  const persistAttempt = () => { try { if(sendAttempt) window.sessionStorage?.setItem('ask-pending-send',JSON.stringify(sendAttempt)); else window.sessionStorage?.removeItem('ask-pending-send'); } catch {} }; const launchKeys = new Map(); const promptDrafts=new Map(); const scopeDrafts=new Map();
  const say = error => { status.textContent = error instanceof Error ? error.message : error; };
  async function regenerateSavedPlan(version, conversationId) {
    const c = version.content;
    const conversation = conversationId ? {id: conversationId} : await request('/api/conversations', 'POST', {repository_ids: c.repositories.map(r => r.repository_id)});
    await request(`/api/conversations/${enc(conversation.id)}/messages`, 'POST', {content:c.user_request, intent:'plan', client_request_id:globalThis.crypto.randomUUID()});
    location.hash = `#/ask/${enc(conversation.id)}`;
    await load();
  }
  const inside = (parent, child) => { for (let n=child;n;n=n.parentNode) if(n===parent)return true; return false; };
  const button = (parent, text, fn) => {
    const b = el('button', text); b.type = 'button';
    const token = inside(body,parent) || inside(source,parent) || inside(planEditor,parent) ? epoch : null;
    b.addEventListener('click', async () => {
      if(b.disabled || !b.isConnected || (token!==null && token!==epoch))return;
      const focused=document.activeElement===b, hash=location.hash;
      b.disabled=true;
      try { await fn(b); } catch(e) { if(token===null || hash===location.hash)say(e); }
      finally {
        b.disabled=b===planStart ? planDraft || b.dataset.ready!=='true' || b.dataset.evidence!=='true' : false;
        if(focused && location.hash===hash && (document.activeElement===document.body || document.activeElement===b || !document.activeElement?.isConnected)){
          const target=b.isConnected ? b : [...body.querySelectorAll('button')].find(n=>n.textContent===text && !n.hidden && !n.disabled);
          if(target)target.focus();
          else {const heading=body.querySelector('h3');if(heading){heading.setAttribute('tabindex','-1');heading.focus();}}
        }
      }
    }); parent.append(b); return b;
  };
  const link = (parent, text, hash) => { const a = el('a', text); a.setAttribute('href', hash); parent.append(a); };
  const field = (parent, label, value, dirty=true) => { const wrap = el('label', label); const input = el('textarea'); input.value = value || ''; if(dirty)input.addEventListener('input', () => { draft = true; }); wrap.append(input); parent.append(wrap); return input; };
  const guard = () => (!draft && !planDraft) || window.confirm('Discard unsaved Ask or plan edits?');
  function discardDraft(){if(current){promptDrafts.delete(current.id);scopeDrafts.delete(current.id);try{window.sessionStorage?.removeItem(`ask-draft:${current.id}`);}catch{}}else{try{window.sessionStorage?.removeItem('ask-draft:new');}catch{}}draft=false;planDraft=false;}
  function renderPending(){
    pendingNotice.replaceChildren();if(!sendAttempt)return;
    pendingNotice.append(el('p',`Unconfirmed request for ${sendAttempt.conversation}. Retry the same request to reconcile it, or explicitly discard the pending client attempt. Discard does not cancel a request the server may already have accepted.`));
    link(pendingNotice,'Open pending conversation',`#/ask/${enc(sendAttempt.conversation)}`);
    button(pendingNotice,'Discard pending request',()=>{sendAttempt=null;persistAttempt();renderPending();say('Pending client attempt discarded. Draft text is preserved; any accepted server request is unchanged.');});
  }
  window.addEventListener('beforeunload', e => { if (draft) { e.preventDefault(); e.returnValue = ''; } });
  let historyGeneration=0, historyItems=[], historyLoaded=false, wasOnAsk=null;
  // Recent belongs to the visible area: a late history answer while Sessions (or any non-Ask page) is
  // showing must not replace that area's list. Matches app.js routeFromHash's Ask routes.
  const askRouteActive=()=>/^(#\/?)?$|^#\/ask(\/|$)|^#\/plans\//.test(location.hash);
  function publishHistory(){
    historyLoaded=true;
    if(!askRouteActive())return;
    shell.setRecent(historyItems,{heading:'Recent',onMore:historyNext?()=>refreshHistory(historyNext):null});
  }
  // Every arrival on Ask from another page (Sessions, Settings, Repositories…) restores the kept
  // conversation history, since that page may have replaced the rail. Moves within Ask leave it alone.
  window.addEventListener('hashchange',()=>{const onAsk=askRouteActive();if(onAsk&&wasOnAsk===false&&historyLoaded)publishHistory();wasOnAsk=onAsk;});
  wasOnAsk=askRouteActive();
  async function refreshHistory(cursor, supplied) {
    const generation=++historyGeneration;
    try {
      const page = supplied || await request(`/api/conversations${cursor ? `?cursor=${enc(cursor)}`:''}`);
      if(generation!==historyGeneration)return;
      if(!Array.isArray(page?.items))throw new Error('Unsupported conversation history response.');
      const seen=new Set(cursor?historyItems.map(item=>item.href):[]);
      if(!cursor)historyItems=[];
      for (const c of page.items){const href=`#/ask/${enc(c.id)}`;if(seen.has(href))continue;seen.add(href);
        historyItems.push({href,title:c.title || `Untitled · ${relativeTime(c.updated_at ?? c.created_at ?? Date.now())}`,meta:relativeTime(c.updated_at ?? c.created_at ?? Date.now())});}
      historyNext=page.next_cursor || null;
      publishHistory();
      historyNotice.replaceChildren();
    } catch(error) {
      if(generation!==historyGeneration)return;
      historyNotice.replaceChildren(el('p',`Conversation history unavailable: ${error.message}`));
      button(historyNotice,'Retry conversation history',()=>refreshHistory());
    }
  }
  let historyNext=null;
  async function cite(c, token, trigger) {
    if (sidePanel) {
      const context = contexts.get(c.repository_id);
      const name = context?.name ?? null;
      const github = String(c.repository_id).startsWith('gh:') && name && name.includes('/') ? `https://github.com/${name}` : null;
      openSource(sidePanel, c, {request, repoName: name, branch: context?.active_snapshot?.branch ?? null, githubUrl: github});
      return;
    }
    const sourceToken=++sourceEpoch; source.replaceChildren(el('p', 'Loading exact snapshot…'));
    try {
      const q = new URLSearchParams(); for (const key of ['repository_id','snapshot_id','path','start_line','end_line']) q.set(key, c[key]);
      const s = await request(`/api/context/source?${q}`);
      if (token !== epoch || sourceToken!==sourceEpoch) return;
      source.replaceChildren(el('h3', `${s.repository_id} · ${s.revision} · ${s.path}`), el('pre', s.lines.map((line, i) => `${s.first_line + i}  ${line}`).join('\n')));
      button(source, 'Close source', () => {sourceEpoch++;source.replaceChildren();if(trigger?.isConnected)trigger.focus();});
    } catch (e) { if (token === epoch && sourceToken===sourceEpoch) source.replaceChildren(el('p', `Source unavailable: ${e.message}`)); }
  }
  function citations(parent, list, token) { for (const c of list || []) button(parent, `${c.repository_id} · ${c.path}:${c.start_line}–${c.end_line} @ ${c.revision}`, trigger => cite(c, token, trigger)); }
  const contexts = new Map();
  async function repoNames(ids, token) {
    const results = await Promise.allSettled(ids.map(id => contexts.has(id) ? Promise.resolve(contexts.get(id)) : request(`/api/repositories/${enc(id)}/context`)));
    if (token !== epoch) return [];
    return ids.map((id, i) => {
      const value = results[i].status === 'fulfilled' ? results[i].value : null;
      if (value) contexts.set(id, value);
      const name = String(value?.name || id).split('/').pop();
      knownNames.set(id, name);
      return {id, name, missing: !value};
    });
  }
  // Returns {updated, base}: base is the server scope the edit was applied to (fresh after a conflict).
  async function updateScope(c, ids) {
    const patch = (revision, next) => request(`/api/conversations/${enc(c.id)}`, 'PATCH', {repository_ids: next, expected_revision: revision});
    try { return {updated: await patch(c.scope_revision, ids), base: c.repository_ids || []}; }
    catch (error) {
      if (error.status !== 409) throw error;
      // Re-apply only this edit on top of the other tab's change.
      const mine = c.repository_ids || [];
      const added = ids.filter(id => !mine.includes(id)), removed = mine.filter(id => !ids.includes(id));
      const fresh = await request(`/api/conversations/${enc(c.id)}`);
      const base = fresh.repository_ids || [];
      const next = [...base.filter(id => !removed.includes(id)), ...added.filter(id => !base.includes(id))];
      try { return {updated: await patch(fresh.scope_revision, next), base}; }
      catch (again) { if (again.status === 409) throw new Error("Someone changed this conversation's repositories. Reload to see the latest."); throw again; }
    }
  }
  async function enrichPlans(c, token) {
    return Promise.all((c.plans || []).map(async p => {
      const base = {id: p.id, title: p.title || 'Saved plan', version: null, steps: null, ready: false, reason: 'Plan details unavailable', created_at: p.created_at ?? Infinity, latest: p.latest_version_id};
      try {
        const full = await request(`/api/plans/${enc(p.id)}`);
        if (token !== epoch) return base;
        const v = full.version;
        return {...base, version: v.version, steps: v.content?.steps?.length ?? 0, ready: v.readiness?.state === 'ready',
          reason: v.readiness?.state === 'ready' ? '' : (v.readiness?.blocking_reasons || []).join(', ') || v.readiness?.state || ''};
      } catch { return base; }
    }));
  }
  async function conversation(id, token) {
    const c = await request(`/api/conversations/${enc(id)}`); if (token !== epoch) return;
    current = c;
    const chips = await repoNames(c.repository_ids || [], token); if (token !== epoch) return;
    header({title: c.title || 'Untitled conversation', pills: chips.map(chip => ({text: chip.name}))});
    const heading = el('h3', c.title || 'Untitled conversation'); heading.className = 'conversation-title sr-only';
    body.replaceChildren(heading);
    for (const chip of chips.filter(chip => chip.missing)) body.append(el('p', `${chip.id} · repository context unavailable. It stays in scope; remove it or reload.`));
    const info = el('details'); info.className = 'conversation-details'; info.append(el('summary', 'Details'));
    for (const chip of chips.filter(chip => !chip.missing)) { const r = contexts.get(chip.id); info.append(el('p', `${chip.name} · ${r.status} · ${coverageText(r.active_snapshot?.coverage)}${r.active_snapshot?.revision ? ` · base ${String(r.active_snapshot.revision).slice(0, 7)}` : ''} · changes ${r.implementation_enabled ? 'allowed' : 'not allowed'}`)); }
    body.append(info);
    const modelRoles = request('/api/settings/model-roles');
    modelRoles.then(settings => { if (token !== epoch) return; const r = settings.roles?.ask || {};
      info.append(el('p', `Answering model: ${r.model || 'unknown'} · ${destinationText(r.destination)} · local-only ${settings.settings?.local_only === true ? 'on' : settings.settings?.local_only === false ? 'off' : 'unknown'}`));
      if (r.model) header({title: c.title || 'Untitled conversation', pills: [...chips.map(chip => ({text: chip.name})), {text: `${r.model} · ${r.destination?.kind || 'unknown'}`, tone: 'ok'}]}); }, () => {});
    let cursor = c.messages_next_cursor;
    const more = button(body, 'Load more saved messages', async () => { const page = await request(`/api/conversations/${enc(id)}/messages?cursor=${enc(cursor)}`); if(token!==epoch)return; c.messages = [...page.items, ...c.messages]; cursor = page.next_cursor; more.hidden = !cursor; draw(); }); more.hidden = !cursor;
    const threadHost = el('div'); threadHost.className = 'chat-thread'; body.append(threadHost);
    let routing = null, plans = [], stale = staleScope.get(id) || null;
    const events = scopeEvents.get(id) || [];
    const handlers = {
      onCitation: (citation, trigger) => cite(citation, token, trigger),
      onViewPlan: planId => { const p = plans.find(x => x.id === planId); location.hash = `#/plans/${enc(planId)}${p?.latest ? `/${enc(p.latest)}` : ''}`; },
      onStartSession: async (planId, {fix = false} = {}) => {
        const p = plans.find(x => x.id === planId);
        if (fix) {
          if (!guard()) return;
          discardDraft();
          acceptedHash = `#/plans/${enc(planId)}${p?.latest ? `/${enc(p.latest)}` : ''}`;
          location.hash = `#/plans/${enc(planId)}${p?.latest ? `/${enc(p.latest)}` : ''}`;
          await load();
          [...body.querySelectorAll('button')].find(n => n.textContent === 'Edit plan')?.click();
          const section = [...(planEditor?.querySelectorAll('details') || [])].find(n => n.querySelector('summary')?.textContent === 'Execution and publication');
          if (section) { section.setAttribute('open', ''); section.querySelector('summary')?.focus(); }
          return;
        }
        if (!p?.ready || stale?.planId === planId || token !== epoch) return;
        try {
          if (draft || planDraft) throw new Error('Save edits before starting.');
          const fresh = await request(`/api/plans/${enc(planId)}${p.latest ? `?version_id=${enc(p.latest)}` : ''}`);
          if (token !== epoch) return;
          const v = fresh.version;
          if (v.readiness?.state !== 'ready') throw new Error('Readiness changed. Open the plan and review what needs fixing.');
          if (p.latest && v.id !== p.latest) throw new Error('Plan version changed. Reload the conversation before starting.');
          if (!v.id || !v.content_hash) throw new Error('Plan details unavailable. Open the saved plan and try again.');
          if (!launchKeys.has(v.id)) {
            let key; try { key = window.sessionStorage?.getItem(`ask-launch-${v.id}`); } catch {}
            key ||= globalThis.crypto.randomUUID(); launchKeys.set(v.id, key);
            try { window.sessionStorage?.setItem(`ask-launch-${v.id}`, key); } catch {}
          }
          const launch = async (acknowledged_base) => {
            if (token !== epoch) return;
            if (draft || planDraft) throw new Error('Save edits before starting.');
            const result = await request('/api/sessions', 'POST', {plan_version_id:v.id, content_hash:v.content_hash, idempotency_key:launchKeys.get(v.id), ...(acknowledged_base ? {acknowledged_base} : {})});
            if (!result.session_id) throw new Error('Session outcome unknown. Retry Start session to reconcile.');
            launchKeys.delete(v.id); try { window.sessionStorage?.removeItem(`ask-launch-${v.id}`); } catch {}
            if (token !== epoch) return;
            location.hash = `#/sessions/${enc(result.session_id)}`;
          };
          try { await launch(); } catch (error) {
            if (error.code === 'base_advanced') {
              renderBaseAdvanced(threadHost, error.details || [], {nameFor:id => knownNames.get(id) || id,
                onRetain: ack => launch(ack), onRegenerate: () => regenerateSavedPlan(v, id), onError:say});
              return;
            }
            if (error.status >= 400 && error.status < 500) {
              launchKeys.delete(v.id); try { window.sessionStorage?.removeItem(`ask-launch-${v.id}`); } catch {}
            }
            throw error;
          }
        } catch (error) { if (token === epoch) say(`Couldn't start session: ${String(error.message || error).replaceAll('_', ' ')}. Retry Start session or open the plan to review its settings.`); }
      },
      onRegenerate: () => { if (composer.value.trim()) return [...composer.root.querySelectorAll('button')].find(b => b.textContent === 'Generate plan')?.click(); say('Describe the change again, then choose Generate plan to make a new version with the current repositories.'); composer.focus(); },
      onRetry: async runId => { try { await request(`/api/answer-runs/${enc(runId)}/retry`, 'POST', {}); if (token === epoch) { await load(); composerHandle?.focus(); } } catch (error) { if (token === epoch) say(error); } },
      onPickRoute: async repositoryId => {
        const keep = {routing, pendingQuestion};
        try {
          await scopeTo([repositoryId]); routing = null;
          await resumePending();
        } catch (error) {
          if (token !== epoch) return;
          ({routing, pendingQuestion} = keep); draw(); say(error);
        }
      },
      onChooseRepos: () => [...composer.root.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === 'Add repositories')?.click(),
    };
    const draw = () => renderThread(threadHost, {now: Date.now(), messages: c.messages || [], runs: c.runs || [], plans, routing, scopeEvents: events, staleScopeSincePlan: stale}, handlers);
    let pendingQuestion = null;
    // Sends the question that waited for repositories as it reads now: edited text wins, cleared text sends nothing.
    async function resumePending() {
      if (!pendingQuestion) return;
      const {intent} = pendingQuestion, text = composer.value.trim();
      pendingQuestion = null;
      try { window.sessionStorage?.removeItem(`ask-route-intent:${id}`); } catch {}
      if (!text) { draw(); return; }
      await send(text, intent);
      if (composer.value.trim() === text) composer.setValue('');
    }
    async function scopeTo(ids) {
      const {updated, base} = await updateScope(c, ids); if (token !== epoch) return;
      const before = new Map((await repoNames(base, token)).map(chip => [chip.id, chip.name])); if (token !== epoch) return;
      Object.assign(c, {scope_revision: updated.scope_revision, repository_ids: updated.repository_ids});
      const after = await repoNames(c.repository_ids, token); if (token !== epoch) return;
      const added = after.filter(chip => !before.has(chip.id)), removed = [...before].filter(([key]) => !c.repository_ids.includes(key));
      for (const chip of added) events.push({kind: 'added', name: chip.name, at: Date.now()});
      for (const [, name] of removed) events.push({kind: 'removed', name, at: Date.now()});
      scopeEvents.set(id, events);
      if ((added.length || removed.length) && plans.length) { stale = {planId: plans.at(-1).id, addedNames: [...(stale?.addedNames || []), ...added.map(chip => chip.name)], removedNames: [...(stale?.removedNames || []), ...removed.map(([,name])=>name)]}; staleScope.set(id, stale); }
      composer.setChips(after);
      header({title: c.title || 'Untitled conversation', pills: after.map(chip => ({text: chip.name}))});
      rememberRepositories(c.repository_ids);
      draw();
    }
    async function send(content, intent) {
      // A skipped send must throw: the composer treats a normal return as sent and clears the text.
      if(pending)throw new Error('Still sending the previous question.');
      if(token!==epoch || !content.trim())return;
      if (!(c.repository_ids || []).length) {
        const r = await request('/api/route', 'POST', {prompt: content, conversation_id: id, pinned_repository_ids: []}); if (token !== epoch) return;
        const status = r.status ?? r.state;
        routing = {state: status, candidates: (r.candidates || []).filter(x => status !== 'resolved' || (r.selected_repository_ids || []).includes(x.repository_id))};
        // The routing card explains what to do; the silent error only keeps the text in the composer.
        if (status !== 'resolved' || !(r.selected_repository_ids || []).length) { pendingQuestion = {text: content, intent}; try { window.sessionStorage?.setItem(`ask-route-intent:${id}`, intent); } catch {} draw(); throw Object.assign(new Error(status === 'no_match' ? 'No repository matched. Choose repositories with +.' : 'Choose a repository to continue.'), {silent: true}); }
        await scopeTo(r.selected_repository_ids);
      }
      if(sendAttempt && (sendAttempt.content!==content || sendAttempt.intent!==intent || sendAttempt.conversation!==id)) throw new Error('Resolve the uncertain request by retrying the same content and action before submitting another.');
      sendAttempt ||= {conversation:id,content,intent,client_request_id:globalThis.crypto.randomUUID()}; persistAttempt(); pending=true;
      renderPending();
      try {
        await request(`/api/conversations/${enc(id)}/messages`,'POST',{content,intent,client_request_id:sendAttempt.client_request_id});
      } catch(error) {
        if(error.status>=400 && error.status<500){sendAttempt=null;persistAttempt();renderPending();}
        throw error;
      } finally {pending=false;}
      sendAttempt=null;persistAttempt();renderPending();
      // Text typed while the request was in flight becomes the draft the rebuilt composer starts with,
      // unless the user has since left the conversation (leaving discards drafts).
      const typed=token!==epoch || composer.value.trim()===content?'':composer.value;
      try{if(typed.trim())window.sessionStorage?.setItem(`ask-draft:${id}`,typed);else window.sessionStorage?.removeItem(`ask-draft:${id}`);}catch{}
      if(token===epoch){draft=Boolean(typed.trim());say('');await load();}await refreshHistory();
    }
    const activeRuns = () => (c.runs || []).filter(r=>['queued','running'].includes(r.state));
    const composer = createComposer({request, draftKey: `ask-draft:${id}`, label: 'Question or planning request',
      initialText: sendAttempt?.conversation === id ? sendAttempt.content : null,
      actions: [{label: 'Ask', intent: 'answer', variant: 'primary'}, {label: 'Generate plan', intent: 'plan'}],
      onInput: text => { draft = Boolean(text.trim()); },
      onSend: ({text, intent}) => send(text, intent),
      onScopeChange: async ids => {
        await scopeTo(ids);
        if (token !== epoch || !pendingQuestion || !ids.length) return;
        // Repositories chosen for a question that had none: retire the routing card and send it.
        routing = null; say(''); draw();
        try { await resumePending(); } catch (error) { if (token === epoch && !error.silent) say(error); }
      },
      onCancel: async () => { if (token !== epoch) return; for (const run of activeRuns()) await request(`/api/answer-runs/${enc(run.id)}/cancel`, 'POST', {}); if (token === epoch && !draft) { await load(); composerHandle?.focus(); } }});
    composer.setChips(chips);
    // A question was waiting for repositories when the page was left: restore the choice and its text.
    const waiting = c.pending_route;
    if (waiting && !(c.repository_ids || []).length) {
      routing = {state: waiting.status, candidates: waiting.candidates || []};
      let intent = 'answer'; try { intent = window.sessionStorage?.getItem(`ask-route-intent:${id}`) || 'answer'; } catch {}
      pendingQuestion = {text: waiting.prompt, intent};
      if (!composer.value.trim()) composer.setValue(waiting.prompt);
    }
    modelRoles.then(settings => { if (token === epoch) gateModels(composer, settings); }, () => {});
    if (composer.value.trim()) draft = true;
    body.append(composer.root);
    composerHandle = composer;
    draw();
    enrichPlans(c, token).then(list => { if (token !== epoch) return; plans = list; draw(); });
    const active = activeRuns().length > 0;
    composer.setBusy(active);
    let autosend=null;try{autosend=JSON.parse(window.sessionStorage?.getItem('ask-autosend')||'null');}catch{}
    if(autosend?.conversation===id){try{window.sessionStorage?.removeItem('ask-autosend');}catch{}send(autosend.text,autosend.intent).catch(error=>{composer.setValue(autosend.text);if(error.silent)composer.announce(error.message);else say(error);});}
    async function poll() {
      try {
        const runs=await Promise.all(activeRuns().map(r=>request(`/api/answer-runs/${enc(r.id)}`)));
        if(token!==epoch)return;
        if(runs.some(r=>['queued','running'].includes(r.state))){draw();timer=setTimeout(poll,1500);}
        else {
          const focused=document.activeElement;
          const editing=['INPUT','TEXTAREA','SELECT'].includes(focused?.tagName) || focused?.isContentEditable;
          const within=inside(body,focused);
          if(!draft && !editing){await load();if(within && token+1===epoch){const heading=body.querySelector('h3');if(heading){heading.setAttribute('tabindex','-1');heading.focus();}}}
          else {
            // Keep the message box (and anything typed) in place; only refresh the thread.
            const fresh=await request(`/api/conversations/${enc(id)}`);if(token!==epoch)return;
            Object.assign(c,{messages:fresh.messages,runs:fresh.runs,plans:fresh.plans,messages_next_cursor:fresh.messages_next_cursor});
            composer.setBusy(activeRuns().length>0);draw();
            enrichPlans(c,token).then(list=>{if(token!==epoch)return;plans=list;draw();});
          }
        }
      } catch(e){if(token===epoch)say(`Status unavailable: ${e.message}. Reload saved conversation to reconnect.`);}
    }
    if(active) timer=setTimeout(poll,1500);
  }
  // An unassigned role would only fail after sending; disable its action up front instead.
  function gateModels(composer, settings) {
    const missing = [['answer', 'ask', 'answering questions'], ['plan', 'planning', 'planning']]
      .filter(([, role]) => settings?.roles?.[role]?.status === 'unassigned');
    composer.setUnavailable(missing.map(([intent]) => intent), {
      message: `No model is set for ${missing.map(([, , words]) => words).join(' or ')}.`,
      link: {text: 'Choose a model in Settings', href: '#/settings/providers'},
    });
  }
  function landing() {
    header({title: 'Ask'});
    const heading = el('h3', 'What are we working on?'); heading.className = 'landing-title';
    const setupHost = el('div'); setupHost.className = 'setup-host';
    body.replaceChildren(setupHost, heading);
    const token = epoch;
    let composer = null, roles = null;
    const gate = settings => { roles = settings; if (composer && token === epoch) gateModels(composer, settings); };
    renderSetup(setupHost, {request, onRoles: gate}).catch(() => setupHost.replaceChildren());
    composer = createComposer({request, draftKey: 'ask-draft:new', label: 'Question or planning request',
      actions: [{label: 'Ask', intent: 'answer', variant: 'primary'}, {label: 'Generate plan', intent: 'plan'}],
      onInput: text => { draft = Boolean(text.trim()); },
      onScopeChange: async () => {},
      onCancel: async () => {},
      onSend: async ({text, repositoryIds, intent}) => {
        const created = await request('/api/conversations', 'POST', {title: titleFrom(text), repository_ids: repositoryIds});
        rememberRepositories(repositoryIds);
        try { window.sessionStorage?.removeItem('ask-draft:new'); window.sessionStorage?.setItem('ask-autosend', JSON.stringify({conversation: created.id, intent, text})); } catch {}
        draft = false; acceptedHash = `#/ask/${enc(created.id)}`; location.hash = acceptedHash; await load(); await refreshHistory();
      }});
    const hint = el('p', 'Pick repositories with + or type @, or just ask and OpenDevEngine suggests them. Ask plans changes; it never edits your code.'); hint.className = 'landing-hint';
    body.append(composer.root, hint);
    composerHandle = composer;
    if (roles) gate(roles);
    else request('/api/settings/model-roles').then(settings => { if (!roles) gate(settings); }, () => {});
  }
  async function plan(id, versionId, token) {
    const p = await request(`/api/plans/${enc(id)}${versionId ? `?version_id=${enc(versionId)}` : ''}`); if(token!==epoch)return;
    planDraft=false;
    const editor=el('section');editor.className='plan-editor';
    const v=p.version, c=v.content;
    await repoNames((c?.repositories || []).map(r=>r.repository_id), token); if(token!==epoch)return;
    const nameFor=repository=>knownNames.get(repository) || repository;
    const doc=renderPlanDocument(v,{nameFor,onCitation:(citation,trigger)=>cite(citation,token,trigger)});
    // Identifiers and the execution/publication controls are folded so editable fields lead.
    const info=el('details');info.className='plan-editor-more';info.append(el('summary','Plan metadata'));
    const impl=el('details');impl.className='plan-editor-more';impl.append(el('summary','Execution and publication'));
    editor.replaceChildren(el('h3',`Edit plan · version ${v.version}`),el('p','Saving creates a new version. This version never changes.'),info);
    info.append(el('p',`Version ${v.id} · SHA256 ${v.content_hash}`));
    planEditor=editor;
    if(sidePanel){
      const actions=el('div');actions.className='plan-doc-actions';
      button(actions,'Edit plan',()=>sidePanel.openTab({id:`plan:${id}`,title:`Edit · ${p.title}`,render:host=>host.append(editor),canClose:()=>{if(!guard())return false;discardDraft();return true;}}));
      if(p.conversation_id)link(actions,'Back to conversation',`#/ask/${enc(p.conversation_id)}`);
      body.replaceChildren(actions,doc);
    }else body.replaceChildren(doc,editor);
    const generation=v.generation || {};info.append(el('p',`Plan model: ${generation.model || 'unknown'} · Destination: ${destinationText(generation.destination)} · Usage: ${generation.usage ? Object.entries(generation.usage).filter(([key,value])=>['input_tokens','output_tokens','total_tokens'].includes(key)&&Number.isFinite(value)).map(([key,value])=>`${key.replaceAll('_',' ')} ${value}`).join(' · ') || 'unknown':'unknown'} · Cost: ${typeof generation.usage?.estimated_cost==='number' ? `${generation.usage.cost_status || 'estimated'} ${generation.usage.estimated_cost} USD`:'unknown'}`));
    if(p.conversation_id)link(info,'Conversation',`#/ask/${enc(p.conversation_id)}`);
    if(v.parent_version_id)link(info,'Previous version',`#/plans/${enc(id)}/${enc(v.parent_version_id)}`);
    link(info,'Latest version',`#/plans/${enc(id)}/${enc(p.latest_version_id)}`);
    info.append(el('p',`Readiness: ${v.readiness.state} · Blocking: ${v.readiness.blocking_reasons.join(', ') || 'none'} · Warnings: ${v.readiness.warnings.join(', ') || 'none'}`),el('p',`Source freshness: ${Object.entries(v.readiness.source_freshness || {}).map(([repository,value])=>`${repository}: ${String(value).replaceAll('_',' ')}`).join(' · ') || 'unknown'}`));
    if(!c){editor.append(el('p','Plan content unavailable or redacted.'));return;}
    info.append(el('p',`Scope: ${c.repositories.map(r=>`${r.repository_id} (${r.role}) @ ${r.revision} · ${r.snapshot_id}`).join('; ')}`),el('p',`Execution profile: ${c.execution_context.execution_profile_id || 'not configured'} · Publication: ${c.publication.policy}`));
    citations(info,c.citations,token);
    const execution=v.readiness.execution; let displayedProfile=null, profileEvidenceReady=!execution?.profile_version_id;
    const consentLine=el('p',execution?.profile_version_id?'Source consent: checking the pinned profile…':'Source consent: execution profile evidence unavailable.');
    const executionBox=el('section');executionBox.className='setting-box';executionBox.append(el('h4','Execution preflight for this saved version'));
    if(execution){
      executionBox.append(el('p',`Profile ${execution.profile_id || 'unknown'} · Immutable version ${execution.profile_version_id || 'unknown'}`),el('p',`Image ${execution.image_id || 'unknown'} · CLI ${execution.cli_version || 'unknown'} · Destination ${destinationText(execution.destination)}`),el('p',limitsText(execution.limits)),el('p',`Verified capabilities: ${Object.entries(execution.capabilities || {}).filter(([,value])=>value===true).map(([key])=>key.replaceAll('_',' ')).join(', ') || 'none reported'}`));
    }else executionBox.append(el('p','Execution preflight is unavailable. Review server readiness below; configure and test an execution profile in Settings.'));
    executionBox.append(consentLine);impl.append(executionBox);
    if(execution?.profile_version_id)request(`/api/execution-profile-versions/${enc(execution.profile_version_id)}`).then(record=>{if(token!==epoch)return;if(record.id!==execution.profile_version_id||!record.config)throw new Error('Pinned profile evidence unavailable.');displayedProfile={id:record.id,image:record.config.image,allow_external_source:record.config.allow_external_source,profile_deleted:record.profile_deleted};profileEvidenceReady=true;consentLine.textContent=`Source consent: ${record.config.allow_external_source===true?'explicitly allowed for this external coding model':'disabled'} · Profile image ${record.config.image || 'unknown'}`;if(planStart){planStart.dataset.evidence='true';planStart.disabled=planDraft||v.readiness.state!=='ready';}},()=>{if(token===epoch)consentLine.textContent='Pinned profile evidence unavailable. Reload before starting.';});
    const edited=structuredClone(c); const readers=[];
    planEditor=editor; editor.addEventListener('input',markPlanDirty); editor.addEventListener('change',markPlanDirty);
    const profileLabel=el('label','Execution profile for the next immutable version');const profileSelect=el('select');profileSelect.disabled=true;profileLabel.append(profileSelect);impl.append(profileLabel);
    const profileStatus=el('p','Loading execution profiles…');impl.append(profileStatus);
    const publicationLabel=el('label','Publication policy for the next immutable version');const publicationSelect=el('select');
    for(const [value,text] of [['manual_draft','Manual draft PR — explicitly choose publication after review'],['on_success_draft','Automatic draft PR after success — authorizes a remote branch and draft PR']]){const option=el('option',text);option.value=value;publicationSelect.append(option);}publicationSelect.value=c.publication.policy;publicationLabel.append(publicationSelect);impl.append(publicationLabel);
    publicationSelect.addEventListener('change',()=>{edited.publication.policy=publicationSelect.value;markPlanDirty();say('Publication policy changed in draft. Save an immutable version before starting.');});
    impl.append(el('p',c.publication.policy==='on_success_draft'?'This saved version authorizes a remote branch and draft pull request after the server confirms the required success and publication checks. Publication capability and policy are rechecked by the server.':'This saved version requires an explicit publication action after review. Starting implementation alone does not authorize a remote branch or pull request.'));

    const unassigned=el('option','Unassigned');unassigned.value='';profileSelect.append(unassigned);
    const selectedProfile=c.execution_context.execution_profile_id;
    if(selectedProfile){const opt=el('option',`Saved profile ${selectedProfile}`);opt.value=selectedProfile;profileSelect.append(opt);}profileSelect.value=selectedProfile || '';
    profileSelect.addEventListener('change',()=>{edited.execution_context.execution_profile_id=profileSelect.value || null;markPlanDirty();profileStatus.textContent='Profile changed in draft. Save a new immutable version to recalculate execution readiness.';});
    // A failed or late profile read must not discard the saved plan or user edits.
    (async()=>{try{let cursor=null;const seen=new Set(selectedProfile?[selectedProfile]:[]);do{const page=await request(`/api/execution-profiles?limit=100${cursor?`&cursor=${enc(cursor)}`:''}`);if(token!==epoch)return;if(!Array.isArray(page.items))throw new Error('Unsupported execution profile response.');for(const profile of page.items){if(seen.has(profile.id)){const existing=[...profileSelect.querySelectorAll('option')].find(o=>o.value===profile.id);if(existing)existing.textContent=`${profile.name} · v${profile.version.version} · test ${profile.check?.status || 'not tested'}`;continue;}seen.add(profile.id);const option=el('option',`${profile.name} · v${profile.version.version} · test ${profile.check?.status || 'not tested'}`);option.value=profile.id;profileSelect.append(option);}cursor=page.next_cursor;}while(cursor);if(token!==epoch)return;profileSelect.disabled=false;profileStatus.textContent='Choose an execution profile, then save a new version. The server pins its exact version and rechecks policy and readiness before launch.';}catch(e){if(token===epoch)profileStatus.textContent=`Execution profile selection unavailable: ${e.message}. Saved plan fields remain editable.`;}})();
    for(const r of c.repositories) button(impl,`Retain pinned base for ${r.repository_id}`,()=>{edited.execution_context.base_choices[r.repository_id]='retain';markPlanDirty();say('Pinned base choice retained in draft; save a new version. Regenerate a plan from the conversation to use current context.');});
    for(const key of ['goal','user_request']){const f=field(editor,key.replaceAll('_',' '),c[key]);readers.push(()=>edited[key]=f.value);}
    for(const key of ['requirements','risks','assumptions']){const f=field(editor,`${key} (one per line)`,c[key].join('\n'));readers.push(()=>edited[key]=f.value.split('\n').filter(x=>x.trim()));}
    for(const [key, fields] of [['steps',['title','description']],['validation',['command','expected']],['open_questions',['text','answer']]]) {
      const group=el('section');group.append(el('h4',key.replaceAll('_',' ')));editor.append(group);
      const rows=el('div');group.append(rows);
      const renderRows=()=>{rows.replaceChildren();edited[key].forEach((item,i)=>{
        const row=el('section'); rows.append(row);
        for(const k of fields){const f=field(row,`${i+1}. ${k}`,item[k]);f.addEventListener('input',()=>{edited[key][i][k]=f.value || (k==='answer'?null:'');});
          if(key==='validation'&&k==='command'){const warn=el('p','This looks like a sentence, not a command.');warn.className='session-warning';const show=()=>{warn.hidden=!looksLikeProse(f.value);};show();f.addEventListener('input',show);row.append(warn);}}
        if(key==='steps'){const f=field(row,`${i+1}. paths (one per line)`,item.paths.join('\n'));f.addEventListener('input',()=>{item.paths=f.value.split('\n').filter(Boolean);});}
        if(key==='validation'){const label=el('label','Validation source');const select=el('select');for(const value of ['proposed','observed']){const option=el('option',value);option.value=value;select.append(option);}select.value=item.source;select.addEventListener('change',()=>{item.source=select.value;markPlanDirty();});label.append(select);row.append(label);
          // Only a check the user ticks here is ever run, as shell, in the session container.
          const approve=el('label','Run this check (runs the command as shell in the session container)');const box=el('input');box.type='checkbox';box.checked=item.approved===true;box.addEventListener('change',()=>{item.approved=box.checked;markPlanDirty();});approve.append(box);row.append(approve);}
        if(key==='open_questions'){const label=el('label','Blocking question');const check=el('input');check.type='checkbox';check.checked=item.blocking;check.addEventListener('change',()=>{item.blocking=check.checked;markPlanDirty();});label.append(check);row.append(label);}
        button(row,'Remove entry',()=>{edited[key].splice(i,1);markPlanDirty();renderRows();});
      });};renderRows();
      button(group,`Add ${key==='open_questions'?'question':key==='steps'?'step':'validation'}`,()=>{edited[key].push(key==='steps'?{title:'',description:'',paths:[],citation_ids:[]}:key==='validation'?{command:'',expected:'',source:'proposed',citation_ids:[],approved:false}:{id:globalThis.crypto.randomUUID(),text:'',blocking:true,answer:null});markPlanDirty();renderRows();});
    }
    editor.append(el('h4','Findings'),el('pre',c.findings.map(f=>f.text).join('\n')));
    button(editor,'Save immutable version',async()=>{readers.forEach(read=>read());const controls=editor.querySelectorAll('textarea, input, select');controls.forEach(n=>n.disabled=true);let saved;try{saved=await request(`/api/plans/${enc(id)}/versions`,'POST',{content:edited,parent_version_id:v.id});}finally{controls.forEach(n=>n.disabled=false);}if(token!==epoch)return;draft=false;planDraft=false;location.hash=`#/plans/${enc(id)}/${enc(saved.version.id)}`;});
    button(editor,'Discard plan edits',()=>{if(!window.confirm('Discard unsaved plan edits and reload the saved version?'))return;draft=false;planDraft=false;return load();});
    const exportDisplay=el('section');editor.append(exportDisplay);
    button(editor,'Export this saved version',async()=>{const result=await request(`/api/plans/${enc(id)}/export?version_id=${enc(v.id)}`);if(token!==epoch)return;if(result.content_hash!==v.content_hash || result.plan_version_id!==v.id)throw new Error('Export version/hash mismatch.');exportDisplay.replaceChildren(el('p',`Export ${result.plan_version_id} · ${result.content_hash}`),el('pre',result.markdown));});
    const start=button(impl,'Start session',async()=>{
      if(draft || planDraft)throw new Error('Save edits before starting.');
      if(!profileEvidenceReady)throw new Error('Reload the pinned execution profile evidence before starting.');
      const fresh=await request(`/api/plans/${enc(id)}?version_id=${enc(v.id)}`);if(token!==epoch)return;
      if(draft || planDraft)throw new Error('Save edits before starting.');
      if(fresh.version.readiness.state!=='ready'||fresh.version.content_hash!==v.content_hash)throw new Error('Readiness changed; reload the saved version.');
      const projection = x => x ? {profile_id:x.profile_id,profile_version_id:x.profile_version_id,image_id:x.image_id,cli_version:x.cli_version,destination:x.destination,allow_external_source:x.allow_external_source,limits:x.limits,capabilities:x.capabilities} : null;
      if(JSON.stringify(projection(fresh.version.readiness.execution))!==JSON.stringify(projection(execution)))throw new Error('Execution settings changed. Reload this saved version to review the exact profile version, image, destination and source consent before starting.');
      if(displayedProfile){const record=await request(`/api/execution-profile-versions/${enc(execution.profile_version_id)}`);if(token!==epoch)return;const now={id:record.id,image:record.config?.image,allow_external_source:record.config?.allow_external_source,profile_deleted:record.profile_deleted};if(JSON.stringify(now)!==JSON.stringify(displayedProfile))throw new Error('Execution source consent changed. Reload and review before starting.');}
      if(draft || planDraft)throw new Error('Save edits before starting.');
      if(!launchKeys.has(v.id)){let key;try{key=window.sessionStorage?.getItem(`ask-launch-${v.id}`);}catch{}key ||= globalThis.crypto.randomUUID();launchKeys.set(v.id,key);try{window.sessionStorage?.setItem(`ask-launch-${v.id}`,key);}catch{}}
      const launch = async (acknowledged_base) => {
        if(token!==epoch)return;
        if(draft || planDraft)throw new Error('Save edits before starting.');
        const result=await request('/api/sessions','POST',{plan_version_id:v.id,content_hash:v.content_hash,idempotency_key:launchKeys.get(v.id),...(acknowledged_base ? {acknowledged_base} : {})});
        if(token!==epoch)return;if(!result.session_id)throw new Error('Session outcome unknown. Retry Start with the same saved version to reconcile.');location.hash=`#/sessions/${enc(result.session_id)}`;
      };
      try { await launch(); } catch(e){if(e.code==='base_advanced'){
        renderBaseAdvanced(impl,e.details || [],{nameFor:id=>knownNames.get(id) || id,onRetain:ack=>launch(ack),onRegenerate:()=>regenerateSavedPlan(v,p.conversation_id),onError:say});return;
      }if(e.status>=400&&e.status<500){launchKeys.delete(v.id);try{window.sessionStorage?.removeItem(`ask-launch-${v.id}`);}catch{}}throw e;}
    });
    planStart=start; start.dataset.ready=String(v.readiness.state==='ready'); start.dataset.evidence=String(profileEvidenceReady); start.disabled=v.readiness.state!=='ready'||!profileEvidenceReady;start.title=v.readiness.blocking_reasons.join(', ');
    const reasons=el('p',`Start session: ${v.readiness.state==='ready'?'server readiness ready':v.readiness.blocking_reasons.join(', ') || v.readiness.state}`);planNotice=reasons; reasons.id='ask-launch-reasons';impl.append(reasons);editor.append(impl);start.setAttribute('aria-describedby',reasons.id);
  }
  async function load() {
    clearTimeout(timer); const token=++epoch; sourceEpoch++; current=null; planEditor=null; planStart=null; planNotice=null; sidePanel?.close({force:true});
    // Retire controls immediately: the URL must never operate on the previous record.
    source.replaceChildren();body.replaceChildren(el('p','Loading saved Ask…'));
    const parts=location.hash.split('/');
    try {
      if(parts[1]==='plans' && parts[2])await plan(decodeURIComponent(parts[2]),parts[3] ? decodeURIComponent(parts[3]):null,token);
      else if(parts[1]==='ask' && parts[2])await conversation(decodeURIComponent(parts[2]),token);
      else landing();
    } catch(error) {
      if(token!==epoch)return;
      body.replaceChildren(el('h3',parts[1]==='plans'?'Plan unavailable':'Conversation unavailable'),el('p',error.message));
      button(body,'Retry saved page',()=>load());link(body,'Start a new conversation','#/ask');
      say(`Saved page unavailable: ${error.message}`);
    }
  }
  window.addEventListener('hashchange',()=>{if(location.hash===acceptedHash)return;if(!guard()){window.history.replaceState(null,'',acceptedHash);return;}discardDraft();acceptedHash=location.hash;load();});
  const handle={panel, focus:()=>title.focus(), refreshHistory:()=>refreshHistory(), current:()=>current, canNavigate:()=>{if(location.hash===acceptedHash)return true;if(!guard()){window.history.replaceState(null,'',acceptedHash);return false;}discardDraft();return true;}};
  onMount(handle);
  renderPending();
  if(initialHistoryError){historyNotice.replaceChildren(el('p',`Conversation history unavailable: ${initialHistoryError.message}`));button(historyNotice,'Retry conversation history',()=>refreshHistory());}
  else await refreshHistory(null,listing);
  await load();
  return handle;
}
async function http(path,method='GET',body) {
  const response=await fetch(path,{method,headers:{'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});
  let value;
  try {value=await response.json();}catch(error){if(response.ok)throw error;}
  if(!response.ok){const error=new Error(typeof value?.error==='string'?value.error:value?.error?.message || value?.message || value?.code || `HTTP ${response.status}`);error.status=response.status;error.code=value?.code || value?.error?.code;error.details=value?.details || value?.error?.details;throw error;}
  return value;
}
