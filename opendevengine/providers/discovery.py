"""Discover models in the configured Ollama server; never contacts any other host."""
import json
from urllib.parse import urlsplit
from .transport import TransportFailure, TransportRequest
from .validation import is_local_host, is_trusted_endpoint, ollama_url
EMBEDDING_FAMILIES = {'bert', 'nomic-bert'}


def _base(base_url):
    if base_url is None:
        base_url = ollama_url()
    if not isinstance(base_url, str) or len(base_url) > 200:
        raise ValueError('Invalid discovery address.')
    parts = urlsplit(base_url)
    if parts.scheme not in ('http', 'https') or not (is_local_host(parts.hostname or '') or is_trusted_endpoint(base_url)) \
            or parts.path not in ('', '/') or parts.query or parts.fragment or parts.username:
        raise ValueError('Discovery is limited to a local Ollama address.')
    return base_url.rstrip('/')


def _call(transport, method, url, body=None, timeout=10.0):
    data = None if body is None else json.dumps(body).encode()
    response = transport.send(TransportRequest(method, url, {'Content-Type': 'application/json'}, data,
                                               timeout=timeout, max_response_bytes=1024 * 1024))
    if response.status != 200:
        raise TransportFailure('status %d' % response.status)
    return json.loads(response.body)


def discover_ollama(base_url=None, *, transport):
    base = _base(base_url)
    try:
        tags = _call(transport, 'GET', base + '/api/tags')
        running = _call(transport, 'GET', base + '/api/ps')
    except (TransportFailure, ValueError):
        return {'reachable': False, 'base_url': base, 'models': [], 'error_code': 'provider_unreachable'}
    loaded = {m.get('name'): m for m in running.get('models', []) if isinstance(m, dict)}
    models = []
    for item in tags.get('models', []):
        if not isinstance(item, dict) or not isinstance(item.get('name'), str):
            continue
        name, details = item['name'], item.get('details') or {}
        families = set(details.get('families') or []) | {details.get('family')}
        live = loaded.get(name)
        models.append({'name': name, 'family': details.get('family'), 'parameter_size': details.get('parameter_size'),
                       'context_length': live.get('context_length') if live else None,
                       'embedding': bool(families & EMBEDDING_FAMILIES) or 'embed' in name,
                       'loaded': live is not None, 'gpu': (live.get('size_vram', 0) > 0) if live else None})
    return {'reachable': True, 'base_url': base, 'models': models, 'error_code': None}


def probe_embedding_dimensions(base_url, model, *, transport):
    if not isinstance(model, str) or not 1 <= len(model) <= 200:
        raise ValueError('Invalid model.')
    base = _base(base_url)
    try:
        value = _call(transport, 'POST', base + '/api/embed', {'model': model, 'input': 'dimension probe'}, timeout=60.0)
    except (TransportFailure, json.JSONDecodeError) as error:
        raise ValueError('Model did not return an embedding.') from error
    vectors = value.get('embeddings')
    if not isinstance(vectors, list) or not vectors or not isinstance(vectors[0], list) or not vectors[0]:
        raise ValueError('Model did not return an embedding.')
    return len(vectors[0])
