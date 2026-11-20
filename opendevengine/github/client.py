"""Bounded GitHub-only HTTP and transient App authentication."""
import base64
from dataclasses import dataclass
from datetime import datetime
import json
import re
import socket
from urllib import request, error
from urllib.parse import quote
from .errors import GitHubError


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    headers: dict
    json: object = None
    timeout: int = 20
    max_bytes: int = 8 * 1024 * 1024

    def __repr__(self):
        return 'Request(<redacted>)'


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict
    json: object

    def __repr__(self):
        return 'Response(status=%s)' % self.status


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def transport(req):
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    data = None if req.json is None else json.dumps(req.json).encode()
    raw = request.Request('https://api.github.com' + req.path, data=data,
                          headers=req.headers, method=req.method)
    try:
        result = opener.open(raw, timeout=req.timeout)
    except error.HTTPError as exc:
        result = exc
    with result:
        body = result.read(req.max_bytes + 1)
        if len(body) > req.max_bytes:
            raise GitHubError('malformed_output')
        try:
            parsed = json.loads(body) if body else {}
        except (ValueError, UnicodeError):
            if 200 <= result.code < 300:
                raise GitHubError('malformed_output') from None
            parsed = {}
        return Response(result.code, dict(result.headers), parsed)


def private_key(pem):
    valid_size = False
    if isinstance(pem, str):
        try:
            valid_size = 1 <= len(pem.encode()) <= 16384
        except UnicodeError:
            pass
    if not valid_size:
        raise GitHubError('validation')
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
    except ImportError:
        raise GitHubError('crypto_unavailable') from None
    valid = False
    try:
        key = load_pem_private_key(pem.encode(), password=None)
        valid = isinstance(key, RSAPrivateKey) and key.key_size >= 2048
    except (ValueError, TypeError):
        pass
    if not valid:
        raise GitHubError('validation')
    return key


def jwt(app_id, pem, now):
    key = private_key(pem)
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    def enc(value):
        return base64.urlsafe_b64encode(value).rstrip(b'=')
    header = enc(b'{"alg":"RS256","typ":"JWT"}')
    claims = enc(json.dumps({'iat': now // 1000 - 60, 'exp': now // 1000 + 540,
                             'iss': str(app_id)}, separators=(',', ':')).encode())
    message = header + b'.' + claims
    return (message + b'.' + enc(key.sign(message, padding.PKCS1v15(), hashes.SHA256()))).decode()


def positive_id(value):
    if type(value) is int and 0 < value <= 9223372036854775807:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,18}', value) and int(value) <= 9223372036854775807:
        return value
    raise GitHubError('validation')


def component(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', value) or value in ('.', '..'):
        raise GitHubError('malformed_output')
    return value


class GitHubClient:
    def __init__(self, *, transport=None, clock):
        self.transport, self.clock = transport or globals()['transport'], clock

    def request(self, method, path, token, body=None, *, allow_status=()):
        if not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or any(c in path for c in '\r\n#\\'):
            raise GitHubError('validation')
        failed = None
        try:
            headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28',
                       'Content-Type': 'application/json', 'User-Agent': 'OpenDevEngine'}
            if token is not None:
                headers['Authorization'] = 'Bearer ' + token
            response = self.transport(Request(method, path, headers, body))
        except GitHubError:
            raise
        except (TimeoutError, socket.timeout):
            failed = 'timeout'
        except Exception:
            failed = 'upstream_unavailable'
        if failed:
            raise GitHubError(failed)
        headers = {k.lower(): str(v) for k, v in response.headers.items()}
        if response.status == 429 or (response.status == 403 and (headers.get('x-ratelimit-remaining') == '0' or 'retry-after' in headers)):
            try:
                retry = max(self.clock() + 1000, int(headers.get('x-ratelimit-reset', '0')) * 1000,
                            self.clock() + int(headers.get('retry-after', '0')) * 1000)
            except ValueError:
                retry = self.clock() + 60000
            raise GitHubError('rate_limited', retry_at=retry)
        if response.status == 404:
            raise GitHubError('not_found')
        if response.status in (401, 403):
            raise GitHubError('auth')
        if response.status >= 500:
            raise GitHubError('upstream_unavailable')
        if response.status in allow_status:
            return {'_status': response.status, '_body': response.json}
        if not 200 <= response.status < 300:
            raise GitHubError('malformed_output')
        return response.json

    def convert_manifest(self, code):
        if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', code):
            raise GitHubError('manifest_conversion_failed')
        try:
            value = self.request('POST', '/app-manifests/' + code + '/conversions', None)
        except GitHubError as exc:
            if exc.code == 'rate_limited':
                raise
            raise GitHubError('manifest_conversion_failed') from None
        if not isinstance(value, dict) or not isinstance(value.get('pem'), str) or not isinstance(value.get('slug'), str):
            raise GitHubError('manifest_conversion_failed')
        return {'id': value.get('id'), 'slug': value['slug'], 'pem': value['pem']}

    def app(self, token):
        return self.request('GET', '/app', token)

    def installation(self, token, installation_id, app_id):
        value = self.request('GET', '/app/installations/' + positive_id(installation_id), token)
        if not isinstance(value, dict) or str(value.get('app_id')) != str(app_id) or str(value.get('id')) != str(installation_id):
            raise GitHubError('installation_mismatch')
        if value.get('suspended_at'):
            raise GitHubError('installation_suspended')
        account = value.get('account')
        if not isinstance(account, dict) or account.get('type') not in ('User', 'Organization', 'Enterprise') or value.get('repository_selection', 'selected') not in ('all', 'selected'):
            raise GitHubError('malformed_output')
        component(account.get('login'))
        permissions(value.get('permissions'))
        return value

    def mint_token(self, token, installation_id, *, repository_ids=None, permissions=None):
        body = {}
        if repository_ids is not None:
            body['repository_ids'] = [int(positive_id(v)) for v in repository_ids]
        if permissions is not None:
            body['permissions'] = globals()['permissions'](permissions)
        value = self.request('POST', '/app/installations/' + positive_id(installation_id) + '/access_tokens', token, body)
        if not isinstance(value, dict) or not isinstance(value.get('token'), str) or not value['token'] or not isinstance(value.get('expires_at'), str):
            raise GitHubError('malformed_output')
        valid_expiry = False
        try:
            expiry = datetime.fromisoformat(value['expires_at'].replace('Z', '+00:00'))
            valid_expiry = expiry.tzinfo is not None and self.clock() < int(expiry.timestamp() * 1000) <= self.clock() + 3660000
        except (ValueError, OverflowError, OSError):
            pass
        if not valid_expiry:
            raise GitHubError('malformed_output')
        return {'token': value['token'], 'expires_at': value['expires_at']}

    def repositories(self, token, page):
        value = self.request('GET', '/installation/repositories?per_page=100&page=' + str(page), token)
        if not isinstance(value, dict) or not isinstance(value.get('repositories'), list) or len(value['repositories']) > 100:
            raise GitHubError('malformed_output')
        return value['repositories']

    def default_branch_sha(self, token, owner, name, branch):
        if not isinstance(branch, str) or not 1 <= len(branch) <= 255:
            raise GitHubError('validation')
        value = self.request('GET', '/repos/%s/%s/commits/%s' % (component(owner), component(name), quote(branch, safe='')), token)
        sha = value.get('sha') if isinstance(value, dict) else None
        if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40,64}', sha):
            raise GitHubError('malformed_output')
        return sha

    def tree(self, token, owner, name, sha):
        if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40,64}', sha):
            raise GitHubError('validation')
        value = self.request('GET', '/repos/%s/%s/git/trees/%s' % (component(owner), component(name), sha), token)
        if not isinstance(value, dict) or value.get('truncated') or not isinstance(value.get('tree'), list) or len(value['tree']) > 10000:
            raise GitHubError('malformed_output')
        return value

    def blob(self, token, owner, name, sha, *, max_bytes=1024 * 1024):
        if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40,64}', sha) or type(max_bytes) is not int or not 1 <= max_bytes <= 4 * 1024 * 1024:
            raise GitHubError('validation')
        value = self.request('GET', '/repos/%s/%s/git/blobs/%s' % (component(owner), component(name), sha), token)
        try:
            if value['encoding'] != 'base64' or type(value['size']) is not int or not 0 <= value['size'] <= max_bytes:
                raise ValueError()
            if not isinstance(value['content'], str):
                raise ValueError()
            decoded = base64.b64decode(value['content'].replace('\n', ''), validate=True)
            if len(decoded) != value['size'] or len(decoded) > max_bytes:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise GitHubError('malformed_output') from None
        return decoded

    # Publication methods keep object validation here, at the GitHub boundary.
    def commit(self, token, owner, name, sha):
        if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40,64}', sha):
            raise GitHubError('validation')
        value = self.request('GET', '/repos/%s/%s/git/commits/%s' %
                             (component(owner), component(name), sha), token)
        tree = value.get('tree') if isinstance(value, dict) else None
        tree_sha = tree.get('sha') if isinstance(tree, dict) else None
        if not isinstance(tree_sha, str) or not re.fullmatch('[0-9a-f]{40,64}', tree_sha):
            raise GitHubError('malformed_output')
        return {'sha': sha, 'tree_sha': tree_sha}

    def compare(self, token, owner, name, base, head):
        for value in (base, head):
            if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{40,64}', value):
                raise GitHubError('validation')
        value = self.request('GET', '/repos/%s/%s/compare/%s...%s?per_page=300' %
                             (component(owner), component(name), base, head), token)
        files = value.get('files') if isinstance(value, dict) else None
        total = value.get('total_commits') if isinstance(value, dict) else None
        if (not isinstance(files, list) or len(files) > 300 or type(total) is not int
                or any(not isinstance(f, dict) or not isinstance(f.get('filename'), str)
                       or (f.get('previous_filename') is not None and not isinstance(f['previous_filename'], str))
                       for f in files)):
            raise GitHubError('malformed_output')
        return {'files': files, 'complete': len(files) < 300 and value.get('status') in ('ahead', 'identical')}

    def create_blob(self, token, owner, name, content):
        if not isinstance(content, (bytes, bytearray)) or len(content) > 4 * 1024 * 1024:
            raise GitHubError('validation')
        value = self.request('POST', '/repos/%s/%s/git/blobs' % (component(owner), component(name)), token,
                             {'content': base64.b64encode(bytes(content)).decode(), 'encoding': 'base64'})
        return self._object_sha(value)

    @staticmethod
    def _object_sha(value):
        sha = value.get('sha') if isinstance(value, dict) else None
        if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40,64}', sha):
            raise GitHubError('malformed_output')
        return sha

    def create_tree(self, token, owner, name, base_tree, entries):
        if not isinstance(base_tree, str) or not re.fullmatch('[0-9a-f]{40,64}', base_tree):
            raise GitHubError('validation')
        if (not isinstance(entries, list) or not 1 <= len(entries) <= 1000
                or any(not isinstance(e, dict) or set(e) != {'path','mode','type','sha'}
                       or not isinstance(e['path'], str) or not 1 <= len(e['path']) <= 1024
                       or e['mode'] not in ('100644','100755') or e['type'] != 'blob'
                       or (e['sha'] is not None and not re.fullmatch('[0-9a-f]{40,64}', e['sha'])) for e in entries)):
            raise GitHubError('validation')
        value = self.request('POST', '/repos/%s/%s/git/trees' % (component(owner), component(name)), token,
                             {'base_tree': base_tree, 'tree': entries})
        return self._object_sha(value)

    def create_commit(self, token, owner, name, message, tree_sha, parent_sha, identity):
        if (not isinstance(message, str) or not 1 <= len(message) <= 4096
                or any(not isinstance(v, str) or not re.fullmatch('[0-9a-f]{40,64}', v) for v in (tree_sha,parent_sha))
                or not isinstance(identity, dict) or set(identity) != {'name','email','date'}
                or any(not isinstance(v, str) or not v for v in identity.values())):
            raise GitHubError('validation')
        body = {'message': message, 'tree': tree_sha, 'parents': [parent_sha],
                'author': identity, 'committer': identity}
        value = self.request('POST', '/repos/%s/%s/git/commits' % (component(owner), component(name)), token, body)
        return self._object_sha(value)

    def get_ref(self, token, owner, name, branch):
        if not isinstance(branch, str) or not 1 <= len(branch) <= 255:
            raise GitHubError('validation')
        value = self.request('GET', '/repos/%s/%s/git/ref/heads/%s' %
                             (component(owner), component(name), quote(branch, safe='')), token)
        obj = value.get('object') if isinstance(value, dict) else None
        return self._object_sha(obj)

    def create_ref(self, token, owner, name, branch, sha):
        if (not isinstance(branch, str) or not 1 <= len(branch) <= 255 or branch.startswith('/') or branch.endswith('/')
                or '..' in branch or not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40,64}', sha)):
            raise GitHubError('validation')
        value = self.request('POST', '/repos/%s/%s/git/refs' % (component(owner), component(name)), token,
                             {'ref': 'refs/heads/' + branch, 'sha': sha}, allow_status=(422,))
        if value.get('_status') == 422:
            return False
        self._object_sha(value.get('object', value))
        return True

    def pull_requests(self, token, owner, name, head, base):
        if any(not isinstance(v, str) or not 1 <= len(v) <= 255 for v in (head,base)):
            raise GitHubError('validation')
        query = '?state=all&head=%s&base=%s&per_page=100' % (quote(head, safe=''), quote(base, safe=''))
        value = self.request('GET', '/repos/%s/%s/pulls%s' % (component(owner), component(name), query), token)
        if not isinstance(value, list) or len(value) > 100:
            raise GitHubError('malformed_output')
        return value

    def create_pull_request(self, token, owner, name, title, body, head, base):
        if (not isinstance(title, str) or not 1 <= len(title) <= 256 or not isinstance(body, str) or len(body) > 65536
                or any(not isinstance(v, str) or not 1 <= len(v) <= 255 for v in (head,base))):
            raise GitHubError('validation')
        return self.request('POST', '/repos/%s/%s/pulls' % (component(owner), component(name)), token,
                            {'title': title, 'body': body, 'head': head, 'base': base, 'draft': True})

    def pull_request(self, token, owner, name, number):
        return self.request('GET', '/repos/%s/%s/pulls/%s' %
                            (component(owner), component(name), positive_id(number)), token)


def permissions(value):
    if not isinstance(value, dict) or len(value) > 100 or any(not isinstance(k, str) or not re.fullmatch('[a-z_]{1,80}', k) or v not in ('read', 'write', 'admin') for k, v in value.items()):
        raise GitHubError('malformed_output')
    return value
