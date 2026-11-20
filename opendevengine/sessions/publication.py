"""Content-free verification of persisted runtime captures for trusted publication."""
import base64
import binascii
import json

from opendevengine.execution.runner import validate_relative_path

from .errors import SessionError
from .redaction import contains_secret
from .workspace import REGULAR_MODES, publication_rejection, sha256


def is_omitted(entry):
    """Only newly added generated artifacts are nonblocking."""
    return (isinstance(entry, dict) and entry.get('reason') == 'excluded_generated'
            and entry.get('status') == 'added')


def verify_capture(repo, checkpoint, patch, outcome, limits):
    """Check artifact integrity and the byte/metadata agreement of a capture pair."""
    if checkpoint is None or patch is None:
        raise SessionError('artifacts_missing')
    if checkpoint['content'] is None or patch['content'] is None:
        raise SessionError('artifacts_redacted')
    try:
        budget = limits['service']['artifact_bytes']
        docs = []
        total = 0
        for artifact in (checkpoint, patch):
            raw = bytes(artifact['content'])
            total += len(raw)
            if (total > budget or len(raw) != artifact['byte_count']
                    or sha256(raw) != artifact['sha256']):
                raise SessionError('stale_artifacts')
            docs.append(json.loads(raw))
        cp, pt = docs
        if (outcome.get('last_checkpoint_id') != checkpoint['id']
                or repo['current_diff_hash'] != patch['sha256']
                or checkpoint['created_at'] != patch['created_at']):
            raise SessionError('stale_artifacts')
        manifest = json.loads(repo['base_manifest_json'])
        if not (cp['base_sha'] == pt['base_sha'] == manifest['base_sha'] == repo['base_sha']):
            raise SessionError('stale_artifacts')
        if any(not is_omitted(r) for r in cp['rejected'] + pt['rejected']) or outcome.get('rejected_paths'):
            raise SessionError('rejected_paths')
        if cp['rejected'] != pt['rejected']:
            raise ValueError()
        omitted = [r['path'] for r in cp['rejected']]
        if len(set(omitted)) != len(omitted):
            raise ValueError()
        for path in omitted:
            if (path in manifest['files']
                    or publication_rejection(path, manifest) != 'excluded_generated'):
                raise SessionError('rejected_paths')
        changed, deleted, modes = cp['changed'], cp['deleted'], cp['modes']
        if (not isinstance(changed, dict) or not isinstance(deleted, list) or not isinstance(modes, dict)
                or len(set(deleted)) != len(deleted) or set(changed) & set(deleted)):
            raise ValueError()
        paths = set(changed) | set(deleted)
        if (set(modes) != paths or len(changed) > limits['runner']['max_files']
                or len(deleted) > limits['runner']['max_files']):
            raise ValueError()
        if paths & set(omitted):
            raise ValueError()
        files = {f['path']: f for f in pt['files']}
        if set(files) != paths or len(files) != len(pt['files']):
            raise ValueError()
        total_bytes = 0
        for path in paths:
            if publication_rejection(path, manifest) or validate_relative_path(path) != path:
                raise SessionError('rejected_paths')
            mode, item = modes[path], files[path]
            original = manifest['files'].get(path)
            status = 'deleted' if path in deleted else 'modified' if original else 'added'
            if (mode not in REGULAR_MODES or item['mode'] != mode or item['status'] != status
                    or item['base_sha256'] != (original['sha256'] if original else None)
                    or item['old_mode'] != (original['mode'] if original else None)):
                raise ValueError()
            if status == 'deleted':
                if item['sha256'] is not None or item['bytes'] != 0 or mode != original['mode']:
                    raise ValueError()
            else:
                data = base64.b64decode(changed[path], validate=True)
                total_bytes += len(data)
                if (len(data) > limits['runner']['max_file_bytes']
                        or total_bytes > limits['runner']['workspace_mib'] * 1024 * 1024
                        or sha256(data) != item['sha256'] or len(data) != item['bytes']):
                    raise ValueError()
                if contains_secret(data):
                    raise SessionError('sensitive_content')
        if (pt['stats']['files'] != len(paths) or pt['stats']['rejected'] != len(omitted)
                or pt['stats']['additions'] != sum(f['additions'] for f in files.values())
                or pt['stats']['deletions'] != sum(f['deletions'] for f in files.values())):
            raise ValueError()
        return cp, pt
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, binascii.Error):
        raise SessionError('session_snapshot_invalid') from None
