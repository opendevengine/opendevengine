"""Read immutable commits without executing repository scripts or following links."""
import os
import re
import subprocess
import selectors
import time
from .safety import excluded


def git(root, *args, max_bytes=1024*1024):
    # Output is bounded while streaming; subprocess has no shell, hooks,
    # prompts, inherited Git overrides, replace objects or optional index locks.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT='0', GIT_NO_REPLACE_OBJECTS='1', GIT_OPTIONAL_LOCKS='0')
    process = subprocess.Popen(['git', '--no-pager', '-c', 'core.hooksPath=/dev/null',
                                '-c', 'core.fsmonitor=false', '-C', root, *args],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env=env)
    output = bytearray()
    deadline = time.monotonic() + 20
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise ValueError('Bounded Git read timed out.')
            block = os.read(process.stdout.fileno(), min(65536, max_bytes + 1 - len(output)))
            if not block:
                break
            output.extend(block)
            if len(output) > max_bytes:
                raise ValueError('Bounded Git output exceeded.')
        if process.wait(timeout=max(0.01, deadline-time.monotonic())):
            raise ValueError('Bounded Git read failed.')
        return bytes(output)
    finally:
        selector.close()
        process.stdout.close()
        if process.poll() is None:
            process.kill()
        process.wait()



def local_snapshot(source, revision, caps):
    root = source['local_path']
    rev = revision or 'HEAD'
    if revision is not None and not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', revision):
        raise ValueError('An immutable revision is required.')
    sha = git(root, 'rev-parse', '--verify', rev + '^{commit}', max_bytes=100).decode().strip()
    if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', sha):
        raise ValueError('Invalid commit.')
    tree = git(root, 'ls-tree', '-r', '-z', sha, max_bytes=4*1024*1024)
    def files():
        entries = tree.split(b'\0')
        for entry in entries:
            if not entry:
                continue
            meta, raw_path = entry.split(b'\t', 1)
            mode, kind, oid = meta.decode().split()
            path = raw_path.decode('utf-8', errors='replace')
            if mode not in ('100644', '100755') or kind != 'blob':
                yield {'path': path, 'kind': 'symlink' if mode == '120000' else 'submodule'}
                continue
            reason = excluded(path, source['exclusions'])
            if reason:
                yield {'path': path, 'kind': reason}
                continue
            if path in source.get('_ingested_paths',()):
                yield {'path':path,'kind':'resumed'}
                continue
            size = int(git(root, 'cat-file', '-s', oid, max_bytes=32))
            if size > caps['file_bytes']:
                yield {'path': path, 'kind': 'file_bytes'}
                continue
            yield {'path': path, 'content': git(root, 'cat-file', 'blob', oid, max_bytes=caps['file_bytes'])}
    return {'revision': sha, 'revision_kind': 'git', 'branch': None, 'files': files()}


def github_snapshot(github, source, revision, caps):
    ident = source['github_repository_id']
    repo = github.get_repository(ident)
    github.require_access(ident)
    token = github.mint_installation_token(ident)['token']
    client = github.client
    sha = revision or client.default_branch_sha(token, repo['owner'], repo['name'], repo['default_branch'])
    if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', sha):
        raise ValueError('Invalid commit.')
    def files():
        pending = [('', sha)]
        visited = 0
        while pending:
            prefix, tree_sha = pending.pop()
            github.require_access(ident)
            tree = client.tree(token, repo['owner'], repo['name'], tree_sha)
            for entry in tree['tree']:
                visited += 1
                if visited > caps['files'] * 4:
                    yield {'path': 'tree-limit', 'kind': 'tree_limit'}
                    return
                path = prefix + entry['path']
                reason = excluded(path, source['exclusions'])
                if reason:
                    yield {'path': path, 'kind': reason}
                elif entry.get('mode') in ('120000', '160000'):
                    yield {'path': path, 'kind': 'symlink' if entry['mode'] == '120000' else 'submodule'}
                elif entry.get('type') == 'tree':
                    pending.append((path + '/', entry['sha']))
                elif entry.get('type') == 'blob':
                    if path in source.get('_ingested_paths',()):
                        yield {'path':path,'kind':'resumed'}
                        continue
                    if type(entry.get('size')) is not int or not 0 <= entry['size'] <= caps['file_bytes']:
                        yield {'path': path, 'kind': 'file_bytes'}
                    else:
                        github.require_access(ident)
                        yield {'path': path, 'content': client.blob(token, repo['owner'], repo['name'], entry['sha'], max_bytes=caps['file_bytes'])}
                else:
                    yield {'path': path, 'kind': 'unsupported'}
    return {'revision': sha, 'revision_kind': 'git', 'branch': repo['default_branch'], 'files': files()}
