"""Strict validation for provider profile specs, endpoints and call inputs."""
import ipaddress
import json
import math
import os
from urllib.parse import urlsplit

from .errors import ProviderValidationError

ADAPTERS = ('ollama', 'openai_compatible', 'claude_cli')
ROLES = ('ask', 'planning', 'summary', 'embedding')
CAPABILITIES = ('chat', 'structured_output', 'embeddings')
MESSAGE_ROLES = ('system', 'user', 'assistant')
OLLAMA_URL_ENV = 'OPENDEVENGINE_OLLAMA_URL'
DEFAULT_OLLAMA = 'http://127.0.0.1:11434'

SPEC_KEYS = {'name', 'adapter', 'endpoint', 'model', 'config'}
CONFIG_DEFAULTS = {'timeout_seconds': 60, 'max_output_tokens': 1024, 'context_limit': 8192,
                   'allow_external_source': False}
CONFIG_KEYS = set(CONFIG_DEFAULTS) | {'capabilities', 'embedding_dimensions',
                                      'input_price_per_million', 'output_price_per_million'}

MAX_MESSAGES = 200
MAX_MESSAGE_CHARS = 1_000_000
MAX_TOTAL_CHARS = 4_000_000
MAX_EMBED_TEXTS = 256
MAX_EMBED_TOTAL_CHARS = 4_000_000
MAX_SCHEMA_BYTES = 64 * 1024


def fail(field):
    return ProviderValidationError(detail={'field': field})


def _is_int(value):
    return type(value) is int


def _is_number(value):
    return type(value) in (int, float) and abs(value) <= 1e100 and math.isfinite(value)


def _bounded_text(value, field, max_len, *, allow_space=True):
    if not isinstance(value, str) or not value.strip() or len(value) > max_len or value != value.strip():
        raise fail(field)
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise fail(field)
    if not allow_space and any(c.isspace() for c in value):
        raise fail(field)
    return value


def is_loopback_host(host):
    """Literal 127.0.0.0/8, ::1 or exactly ``localhost``; no DNS-dependent names."""
    if host == 'localhost':
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.version == 4:
        return address in ipaddress.ip_network('127.0.0.0/8')
    return address == ipaddress.ip_address('::1')


def parse_ollama_url(value):
    """Normalize an Ollama base URL: http(s), ``localhost`` or a literal IP, no path or credentials."""
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or value != value.strip():
        raise ValueError('Use an address such as http://127.0.0.1:11434.')
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError('Use an address such as http://127.0.0.1:11434.') from None
    scheme, host = parts.scheme.lower(), (parts.hostname or '').lower()
    if scheme not in ('http', 'https') or not host or parts.username is not None or parts.password is not None \
            or parts.path not in ('', '/') or parts.query or parts.fragment:
        raise ValueError('Use an address such as http://127.0.0.1:11434.')
    if host != 'localhost':
        # A literal address cannot be redirected by DNS after the user vouched for it.
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise ValueError('Use an IP address for Ollama, not a host name.') from None
        if address.is_unspecified or address.is_multicast:
            raise ValueError('Use the IP address of the machine running Ollama.')
        if address.is_global:
            # Trusted as local and reached over plain http, so it must be on a private network
            # (LAN, VPN such as Tailscale's 100.64.0.0/10, or link-local), never the public internet.
            raise ValueError('Use a private network address (LAN or VPN such as Tailscale), not a public IP.')
    netloc = f'[{host}]' if ':' in host else host
    if port is not None:
        netloc += f':{port}'
    return f'{scheme}://{netloc}'


def ollama_url():
    """The configured Ollama base URL; an unset or invalid setting means loopback only."""
    try:
        return parse_ollama_url(os.environ[OLLAMA_URL_ENV])
    except (KeyError, ValueError):
        return DEFAULT_OLLAMA


def is_local_host(host):
    """Loopback, or the one address the user configured for Ollama (e.g. their own machine on a VPN)."""
    if is_loopback_host(host):
        return True
    try:
        return ipaddress.ip_address(host) == ipaddress.ip_address(urlsplit(ollama_url()).hostname)
    except ValueError:
        return False


def validate_endpoint(value):
    """Return the normalized endpoint base URL (no trailing slash)."""
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or value != value.strip():
        raise fail('endpoint')
    if any(ord(c) <= 32 or ord(c) == 127 for c in value) or '?' in value or '#' in value or '\\' in value:
        raise fail('endpoint')
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise fail('endpoint') from None
    scheme = parts.scheme.lower()
    host = (parts.hostname or '').lower()
    if scheme not in ('http', 'https') or not host or '@' in parts.netloc:
        raise fail('endpoint')
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise fail('endpoint')
    if scheme == 'http' and not is_local_host(host):
        raise fail('endpoint')
    path = parts.path.rstrip('/')
    if any(segment in ('.', '..') for segment in path.split('/')) or '%' in path:
        raise fail('endpoint')
    netloc = f'[{host}]' if ':' in host else host
    if port is not None:
        netloc += f':{port}'
    return f'{scheme}://{netloc}{path}'


def destination_for(endpoint):
    parts = urlsplit(endpoint)
    host = parts.hostname or ''
    return {'kind': 'local' if is_local_host(host) else 'remote',
            'host': parts.netloc}


def validate_config(config):
    if not isinstance(config, dict):
        raise fail('config')
    unknown = set(config) - CONFIG_KEYS
    if unknown:
        raise fail('config')
    result = dict(CONFIG_DEFAULTS)
    result.update(config)
    timeout = result['timeout_seconds']
    if not _is_number(timeout) or not 1 <= timeout <= 600:
        raise fail('config.timeout_seconds')
    if not _is_int(result['max_output_tokens']) or not 1 <= result['max_output_tokens'] <= 131072:
        raise fail('config.max_output_tokens')
    if not _is_int(result['context_limit']) or not 256 <= result['context_limit'] <= 2_000_000:
        raise fail('config.context_limit')
    if type(result['allow_external_source']) is not bool:
        raise fail('config.allow_external_source')
    caps = result.get('capabilities')
    if (not isinstance(caps, list) or not caps or any(not isinstance(c, str) or c not in CAPABILITIES for c in caps)
            or len(set(caps)) != len(caps)):
        raise fail('config.capabilities')
    if 'structured_output' in caps and 'chat' not in caps:
        raise fail('config.capabilities')
    result['capabilities'] = sorted(caps)
    if 'embeddings' in caps:
        dims = result.get('embedding_dimensions')
        if not _is_int(dims) or not 1 <= dims <= 16384:
            raise fail('config.embedding_dimensions')
    elif 'embedding_dimensions' in result:
        raise fail('config.embedding_dimensions')
    for key in ('input_price_per_million', 'output_price_per_million'):
        if key in result:
            price = result[key]
            if not _is_number(price) or not 0 <= price <= 1_000_000:
                raise fail('config.' + key)
    return result


def validate_name(value):
    return _bounded_text(value, 'name', 100)


def validate_model(value):
    return _bounded_text(value, 'model', 200, allow_space=False)


def validate_spec(spec, *, partial=False):
    """Validate a create spec (all keys) or update spec (subset). Returns normalized dict."""
    if not isinstance(spec, dict):
        raise fail('spec')
    unknown = set(spec) - SPEC_KEYS
    if unknown:
        raise fail('spec')
    if not partial:
        for key in ('name', 'adapter', 'endpoint', 'model', 'config'):
            if key not in spec:
                raise fail(key)
    result = {}
    if 'name' in spec:
        result['name'] = validate_name(spec['name'])
    if 'adapter' in spec:
        if not isinstance(spec['adapter'], str) or spec['adapter'] not in ADAPTERS:
            raise fail('adapter')
        result['adapter'] = spec['adapter']
    if 'endpoint' in spec:
        result['endpoint'] = validate_endpoint(spec['endpoint'])
    if 'model' in spec:
        result['model'] = validate_model(spec['model'])
    if 'config' in spec:
        result['config'] = validate_config(spec['config'])
    return result


def validate_secret_value(value):
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or value != value.strip():
        raise fail('secret_value')
    if any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise fail('secret_value')
    return value


def validate_role(role):
    if not isinstance(role, str) or role not in ROLES:
        raise fail('role')
    return role


def validate_flag(value, field):
    if type(value) is not bool:
        raise fail(field)
    return value


def validate_messages(messages):
    if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
        raise fail('messages')
    total = 0
    clean = []
    for message in messages:
        if not isinstance(message, dict) or set(message) != {'role', 'content'}:
            raise fail('messages')
        role, content = message['role'], message['content']
        if not isinstance(role, str) or role not in MESSAGE_ROLES:
            raise fail('messages')
        if not isinstance(content, str) or len(content) > MAX_MESSAGE_CHARS:
            raise fail('messages')
        total += len(content)
        clean.append({'role': role, 'content': content})
    if total > MAX_TOTAL_CHARS or not any(m['content'].strip() for m in clean):
        raise fail('messages')
    return clean, total


def validate_schema(schema):
    if schema is None:
        return None
    if not isinstance(schema, dict):
        raise fail('schema')
    try:
        encoded = json.dumps(schema, allow_nan=False, sort_keys=True)
    except (TypeError, ValueError, RecursionError):
        raise fail('schema') from None
    if len(encoded.encode('utf-8')) > MAX_SCHEMA_BYTES:
        raise fail('schema')
    return json.loads(encoded)


def validate_texts(texts):
    if not isinstance(texts, list) or not 1 <= len(texts) <= MAX_EMBED_TEXTS:
        raise fail('texts')
    total = 0
    for text in texts:
        if not isinstance(text, str) or not text.strip():
            raise fail('texts')
        total += len(text)
    if total > MAX_EMBED_TOTAL_CHARS:
        raise fail('texts')
    return list(texts), total


def validate_subject(subject):
    if subject is None:
        return None, None
    if not isinstance(subject, dict) or set(subject) != {'type', 'id'}:
        raise fail('subject')
    return _bounded_text(subject['type'], 'subject', 64, allow_space=False), \
        _bounded_text(subject['id'], 'subject', 128, allow_space=False)


def validate_purpose(purpose):
    return _bounded_text(purpose, 'purpose', 64, allow_space=False)


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
