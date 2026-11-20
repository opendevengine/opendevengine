// Snapshot status and controls; repository access and jobs remain server-owned.
export function createContextPanel({request, isVisible, repositoryId = null}) {
  const doc = document;
  const make = (tag, text, className) => {
    const element = doc.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const panel = make('section', undefined, 'connected-panel');
  const drafts = new Map();
  let generation = 0, pending = false, timer, cursor = null, next = null;
  const previous = [];
  const within = element => {
    for (let node = element; node; node = node.parentNode) if (node === panel) return true;
    return false;
  };
  const status = parent => {
    const element = make('p', '', 'page-status'); element.setAttribute('role', 'status'); parent.append(element); return element;
  };
  const button = (label, action) => {
    const element = make('button', label, 'button'); element.type = 'button';
    element.addEventListener('click', action); return element;
  };
  function poll() {
    clearTimeout(timer);
    if (doc.visibilityState !== 'visible' || !isVisible()) return;
    timer = setTimeout(() => {
      if (!pending && !drafts.size && !within(doc.activeElement)) refresh();
      else poll();
    }, 4000);
    timer.unref?.();
  }
  async function change(control, output, operation) {
    if (pending) { output.textContent = 'Another context update is pending. Wait for it to finish.'; return; }
    const restore = within(doc.activeElement);
    pending = true; ++generation; clearTimeout(timer);
    control.disabled = true; output.textContent = 'Saving…';
    try {
      await operation();
      output.textContent = 'Saved. Reload context to inspect current state.';
      pending = false;
      await refresh();
      if (restore && isVisible() && (doc.activeElement === doc.body || !doc.activeElement?.isConnected)) {
        const heading = panel.querySelector('h2'); heading.setAttribute('tabindex', '-1'); heading.focus();
      }
    } catch (error) { output.textContent = error.message; }
    finally { pending = false; control.disabled = false; poll(); }
  }
  async function refresh() {
    if (pending || !isVisible()) return;
    clearTimeout(timer);
    if (drafts.size) {
      const message = panel.querySelector('[data-context-status]');
      if (message) message.textContent = 'Unsaved exclusions. Save them or discard local edits before refreshing.';
      poll(); return;
    }
    const current = ++generation;
    const heading = make('h2', 'Repository context');
    const output = status(make('div'));
    output.setAttribute('data-context-status', ''); output.textContent = 'Loading context…';
    panel.replaceChildren(heading, output);
    panel.append(button('Reload context', refresh));
    try {
      const query = new URLSearchParams({limit: '25'});
      if (cursor) query.set('cursor', cursor);
      const [result, modelSettings] = await Promise.all([
        repositoryId ? request(`/api/repositories/${encodeURIComponent(repositoryId)}/context`).then(item => ({items: [item], next_cursor: null}))
          : request(`/api/context/repositories?${query}`),
        request('/api/settings/model-roles')
      ]);
      if (current !== generation) return;
      if (!Array.isArray(result.items) || result.items.some(repo => !repo || typeof repo.repository_id !== 'string' || typeof repo.name !== 'string' || typeof repo.eligible !== 'boolean')) throw new Error('Unsupported context response.');
      next = result.next_cursor;
      const embedding = modelSettings.roles?.embedding;
      output.textContent = `${result.items.length} repositories on this page. Background work preserves the previous usable snapshot.`;
      panel.append(make('p', embedding?.version_id
        ? `Embedding destination: ${embedding.destination?.kind || 'unknown'} ${embedding.destination?.host || ''}. Profile ${embedding.profile_name || ''}, version ${embedding.version_id}. Source-sharing policy is enforced before calls.`
        : 'No embedding role assigned. Indexing can use labeled lexical-only retrieval; configure embeddings in Settings.'));
      for (const repo of result.items) {
        const box = make('section', undefined, 'setting-box');
        box.append(make('h3', repo.name), make('p', `${repo.kind} · ${repo.status} · ${repo.eligible ? 'Retrieval enabled' : 'Retrieval excluded'}`));
        const snapshot = repo.active_snapshot;
        if (snapshot) {
          const coverage = snapshot.coverage || {};
          box.append(make('p', `Snapshot ${snapshot.revision} · ${snapshot.mode} · ${coverage.files ?? '?'} files · ${coverage.chunks ?? '?'} chunks${coverage.partial ? ' · Partial coverage' : ''}`));
          const skipped = Object.entries(coverage.skipped || {}).map(([reason, count]) => `${reason}: ${count}`).join(', ');
          if (skipped) box.append(make('p', `Skipped paths: ${skipped}`));
        } else box.append(make('p', 'No usable source snapshot yet. Catalog metadata does not establish source evidence.'));
        const run = repo.active_run;
        if (run) box.append(make('p', `Indexing ${run.state} · ${run.phase} · ${run.progress?.files ?? 0} files · ${run.progress?.chunks ?? 0} chunks${run.error_code ? ` · ${run.error_code}` : ''}${run.retry_at ? ` · Retry ${new Date(run.retry_at).toLocaleString()}` : ''}`));
        const output = status(box);
        const index = button(snapshot ? `Reindex ${repo.name}` : `Index ${repo.name}`, () => change(index, output, async () => {
          await request(`/api/repositories/${encodeURIComponent(repo.repository_id)}/index`, 'POST',
            embedding?.version_id ? {embedding_version_id: embedding.version_id} : {});
        }));
        index.disabled = !repo.eligible || Boolean(run); box.append(index);
        if (run) {
          const operation = run.state === 'paused' ? 'resume' : 'pause';
          const control = button(`${operation === 'resume' ? 'Resume' : 'Pause'} ${repo.name}`, () => change(control, output,
            () => request(`/api/index-jobs/${encodeURIComponent(run.id)}/${operation}`, 'POST', {})));
          box.append(control);
        }
        if (repo.kind !== 'github') {
          const enable = button(`${repo.eligible ? 'Exclude' : 'Enable'} retrieval for ${repo.name}`, () => change(enable, output,
            () => request(`/api/repositories/${encodeURIComponent(repo.repository_id)}/context`, 'PATCH', {index_enabled: !repo.eligible})));
          box.append(enable);
        }
        const configure = button(`Edit exclusions for ${repo.name}`, async () => {
          if (pending) return;
          configure.disabled = true;
          try {
            const settings = await request(`/api/repositories/${encodeURIComponent(repo.repository_id)}/exclusions`);
            if (current !== generation) return;
            if (!Array.isArray(settings.exclusions)) throw new Error('Unsupported exclusion settings.');
            const form = make('form', undefined, 'settings-form');
            const label = make('label', `Excluded path patterns for ${repo.name}`, 'setting-field');
            const input = make('textarea'); input.value = settings.exclusions.join('\n'); label.append(input);
            const save = make('button', 'Save exclusions', 'button'); save.type = 'submit';
            form.append(label, make('p', 'One relative path or glob per line. Current exclusions also hide retained citations.'), save);
            input.addEventListener('input', () => { drafts.set(repo.repository_id, input.value); output.textContent = 'Unsaved exclusions.'; });
            form.addEventListener('submit', event => {
              event.preventDefault();
              if (pending) return;
              const submitted = input.value;
              change(save, output, async () => {
                input.disabled = true; discard.disabled = true;
                try {
                  await request(`/api/repositories/${encodeURIComponent(repo.repository_id)}/exclusions`, 'PATCH',
                    {exclusions: submitted.split('\n').map(line => line.trim()).filter(Boolean)});
                  if (input.value === submitted) drafts.delete(repo.repository_id);
                } finally { input.disabled = false; discard.disabled = false; }
              });
            });
            const discard = button('Discard local exclusion edits', () => { if (pending) return; drafts.delete(repo.repository_id); form.remove(); configure.disabled = false; output.textContent = 'Local edits discarded.'; poll(); });
            form.append(discard);
            box.append(form); input.focus();
          } catch (error) { output.textContent = error.message; configure.disabled = false; }
        });
        box.append(configure);
        const purge = button(`Purge context for ${repo.name}`, () => {
          const confirm = button(`Confirm purge for ${repo.name}`, () => change(confirm, output,
            () => request(`/api/repositories/${encodeURIComponent(repo.repository_id)}/purge`, 'POST', {})));
          output.replaceChildren(make('span', 'Delete cached source, summaries and vectors. Historical citations become unavailable. '), confirm,
            button('Cancel purge', () => { output.textContent = 'Purge cancelled.'; }));
        });
        box.append(purge); panel.append(box);
      }
      if (!result.items.length) panel.append(make('p', 'Connect GitHub or start with an explicit local/demo source.'));
      const prev = button('Previous context page', () => { if (pending || drafts.size) return; cursor = previous.pop() ?? null; refresh(); });
      const more = button('Next context page', () => { if (pending || drafts.size) return; previous.push(cursor); cursor = next; refresh(); });
      prev.disabled = !previous.length; more.disabled = !next; if (!repositoryId) panel.append(prev, more);
    } catch (error) {
      if (current === generation) output.textContent = `Context unavailable: ${error.message}`;
    } finally { if (current === generation) poll(); }
  }
  panel.append(make('h2', 'Repository context'));
  return {element: panel, refresh};
}
