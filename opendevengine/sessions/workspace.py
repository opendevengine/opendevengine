"""Pinned GitHub workspace acquisition, trusted base manifest and change review.

Source is read through the existing GitHub tree/blob API boundary with a transient
contents:read installation token held only in memory. No host checkout, Git config,
hooks, credentials or raw .git objects ever enter the agent workspace. Only bounded
regular files are acquired; every policy omission is remembered so a later write or
publisher can never shadow an omitted/excluded original path. Over-budget or
truncated trees fail visibly instead of producing a silent partial repository.
"""
import base64
import difflib
import hashlib
import json
import re

from opendevengine.execution.runner import RunnerError, validate_relative_path
from opendevengine.github.errors import GitHubError
from opendevengine.indexing.safety import excluded, safe_path
from .errors import SessionError
from .redaction import contains_secret

_SHA_RE = re.compile(r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')
REGULAR_MODES = ('100644', '100755')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


class Workspace:
    """Trusted base: file bytes (memory only) plus a persisted content-free manifest."""

    def __init__(self, base_sha, files, manifest):
        self.base_sha, self.files, self.manifest = base_sha, files, manifest


def acquisition_limits(runner_limits):
    workspace_bytes = runner_limits['workspace_mib'] * 1024 * 1024
    return {'max_files': runner_limits['max_files'], 'max_file_bytes': runner_limits['max_file_bytes'],
            # Leave room in the tmpfs for the agent's edits and validation output.
            'max_total_bytes': workspace_bytes * 3 // 4,
            'max_tree_entries': runner_limits['max_files'] * 4 + 64}


def acquire_github(github, repository_id, base_sha, exclusions, limits, *, check_authority):
    """Fetch the exact pinned commit; returns Workspace or raises SessionError.

    ``check_authority()`` raises when access/stop/lease changed; it is called before the
    token is minted, before every tree and blob request, and before returning source.
    """
    if not isinstance(base_sha, str) or not _SHA_RE.match(base_sha):
        raise SessionError('base_revision_not_git')
    check_authority()
    try:
        repo = github.get_repository(repository_id)
        github.require_access(repository_id, 'contents_read')
        token = github.mint_installation_token(repository_id)['token']
    except GitHubError as exc:
        raise SessionError('source_access_denied' if exc.code in ('repository_unavailable', 'capability_missing', 'auth', 'not_found')
                           else 'source_acquisition_failed', details=[exc.code]) from None
    files, entries, omitted, omitted_dirs = {}, {}, {}, {}
    total, visited = 0, 0
    pending = [('', base_sha)]
    try:
        while pending:
            prefix, tree_sha = pending.pop()
            check_authority()
            try:
                tree = github.client.tree(token, repo['owner'], repo['name'], tree_sha)
            except GitHubError as exc:
                # The bounded client rejects truncated or >10000-entry trees as malformed.
                code = 'source_tree_truncated' if exc.code == 'malformed_output' else 'source_acquisition_failed'
                raise SessionError(code, details=[exc.code]) from None
            for entry in tree['tree']:
                visited += 1
                if visited > limits['max_tree_entries']:
                    raise SessionError('source_over_budget', details=['tree_entries'])
                if not isinstance(entry, dict) or not isinstance(entry.get('path'), str):
                    raise SessionError('source_acquisition_failed', details=['malformed_entry'])
                path = prefix + entry['path']
                kind, mode = entry.get('type'), entry.get('mode')
                try:
                    path = validate_relative_path(path)
                except RunnerError:
                    omitted[path[:1024]] = 'unsafe_path'
                    continue
                if not safe_path(path):
                    omitted[path] = 'unsafe_path'
                    continue
                reason = excluded(path, exclusions)
                if reason:
                    (omitted_dirs if kind == 'tree' else omitted)[path] = reason
                    continue
                if mode == '120000':
                    omitted[path] = 'symlink'
                elif mode == '160000' or kind == 'commit':
                    omitted[path] = 'submodule'
                elif kind == 'tree':
                    sha = entry.get('sha')
                    if not isinstance(sha, str) or not _SHA_RE.match(sha):
                        raise SessionError('source_acquisition_failed', details=['malformed_entry'])
                    pending.append((path + '/', sha))
                elif kind == 'blob' and mode in REGULAR_MODES:
                    size, sha = entry.get('size'), entry.get('sha')
                    if type(size) is not int or size < 0 or not isinstance(sha, str) or not _SHA_RE.match(sha):
                        raise SessionError('source_acquisition_failed', details=['malformed_entry'])
                    if size > limits['max_file_bytes']:
                        omitted[path] = 'file_too_large'
                        continue
                    if len(files) >= limits['max_files']:
                        raise SessionError('source_over_budget', details=['max_files'])
                    if total + size > limits['max_total_bytes']:
                        raise SessionError('source_over_budget', details=['max_total_bytes'])
                    check_authority()
                    try:
                        content = github.client.blob(token, repo['owner'], repo['name'], sha, max_bytes=limits['max_file_bytes'])
                    except GitHubError as exc:
                        raise SessionError('source_acquisition_failed', details=[exc.code]) from None
                    if contains_secret(content):
                        omitted[path] = 'sensitive_content'
                        continue
                    files[path] = content
                    total += len(content)
                    entries[path] = {'sha256': sha256(content), 'size': len(content), 'mode': mode, 'blob': sha}
                else:
                    omitted[path] = 'unsupported_entry'
    finally:
        token = None  # never persisted; drop the only reference promptly
    # Runner rejects a file that is also a directory prefix; GitHub trees cannot produce it.
    check_authority()
    manifest = {'base_sha': base_sha, 'files': entries, 'omitted': dict(sorted(omitted.items())),
                'omitted_dirs': dict(sorted(omitted_dirs.items())), 'exclusions': list(exclusions),
                'counts': {'files': len(files), 'bytes': total, 'omitted': len(omitted) + len(omitted_dirs)}}
    return Workspace(base_sha, files, manifest)


def broker_exclusions(manifest):
    """fnmatch patterns for the broker: omitted paths/dirs (escaped) and configured exclusions."""
    def escape(path):
        return ''.join('[' + c + ']' if c in '*?[' else c for c in path)
    patterns = [escape(p) for p in manifest.get('omitted', {})]
    for prefix in manifest.get('omitted_dirs', {}):
        patterns += [escape(prefix), escape(prefix) + '/*']
    for pattern in manifest.get('exclusions', []):
        patterns.append(pattern)
        stripped = pattern.rstrip('/')
        if stripped and not any(c in stripped for c in '*?['):
            patterns.append(stripped + '/*')
    return tuple(dict.fromkeys(patterns))


def publication_rejection(path, manifest):
    """Reason a changed path can never be published, or None. Applied regardless of how
    the change was made (file tool or direct command)."""
    try:
        path = validate_relative_path(path)
    except RunnerError:
        return 'unsafe_path'
    parts = path.split('/')
    if any(p.lower() == '.git' for p in parts):
        return 'git_metadata'
    if any(p.lower() in ('.githooks', '.husky') for p in parts):
        return 'hook_path'
    if path in manifest.get('omitted', {}):
        return 'shadows_omitted'
    for prefix in manifest.get('omitted_dirs', {}):
        if path == prefix or path.startswith(prefix + '/'):
            return 'shadows_omitted'
    reason = excluded(path, manifest.get('exclusions', []))
    if reason:
        return 'excluded_' + reason
    return None


def _text(data):
    if b'\0' in data:
        return None
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return None


def review_changes(workspace, snapshot, modes=None):
    """Compare trusted base manifest with a container snapshot.

    Returns (checkpoint_doc, patch_doc). Rejected paths carry only path+reason; their
    content is never retained. Text diffs are unified; binary files are flagged.
    ``modes`` (current git modes from the runner) makes regular 0644/0755 mode
    changes reviewable/publishable; without it, base modes are assumed unchanged.
    """
    manifest, base = workspace.manifest, workspace.files
    modes = modes or {}

    def base_mode(path):
        return manifest['files'].get(path, {}).get('mode', '100644')

    added = sorted(set(snapshot) - set(base))
    deleted = sorted(set(base) - set(snapshot))
    modified = sorted(p for p in set(base) & set(snapshot)
                      if sha256(base[p]) != sha256(snapshot[p]) or modes.get(p, base_mode(p)) != base_mode(p))
    rejected, kept = [], []
    for status, paths in (('added', added), ('modified', modified), ('deleted', deleted)):
        for path in paths:
            reason = publication_rejection(path, manifest)
            if reason:
                rejected.append({'path': path, 'status': status, 'reason': reason})
            else:
                kept.append((status, path))
    files, changed = [], {}
    for status, path in sorted(kept, key=lambda item: item[1]):
        old_mode = base_mode(path) if status != 'added' else None
        mode = modes.get(path, old_mode or '100644') if status != 'deleted' else old_mode
        before = base.get(path, b'') if status != 'added' else b''
        after = snapshot.get(path, b'') if status != 'deleted' else b''
        item = {'path': path, 'status': status, 'mode': mode, 'old_mode': old_mode,
                'mode_changed': status == 'modified' and mode != old_mode,
                'base_sha256': manifest['files'].get(path, {}).get('sha256'),
                'sha256': sha256(after) if status != 'deleted' else None, 'bytes': len(after)}
        old, new = _text(before), _text(after)
        if old is None or new is None:
            item.update(binary=True, diff='', additions=0, deletions=0)
        else:
            lines = list(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                              fromfile='/dev/null' if status == 'added' else 'a/' + path,
                                              tofile='/dev/null' if status == 'deleted' else 'b/' + path))
            lines = [line if line.endswith('\n') else line + '\n\\ No newline at end of file\n' for line in lines]
            item.update(binary=False, diff=''.join(lines),
                        additions=sum(1 for line in lines if line.startswith('+') and not line.startswith('+++')),
                        deletions=sum(1 for line in lines if line.startswith('-') and not line.startswith('---')))
        files.append(item)
        if status != 'deleted':
            changed[path] = base64.b64encode(after).decode('ascii')
    checkpoint = {'base_sha': workspace.base_sha, 'changed': changed,
                  'deleted': [f['path'] for f in files if f['status'] == 'deleted'],
                  'modes': {f['path']: f['mode'] for f in files}, 'rejected': rejected}
    patch = {'base_sha': workspace.base_sha, 'files': files, 'rejected': rejected,
             'stats': {'files': len(files), 'rejected': len(rejected),
                       'additions': sum(f['additions'] for f in files), 'deletions': sum(f['deletions'] for f in files)}}
    return checkpoint, patch
