"""Wire-format adapters for Ollama and OpenAI-compatible endpoints (nonstreaming M1 mode).

Adapters only build request bodies and parse response payloads. They never perform I/O,
resolve credentials or apply policy; ``ProviderService`` owns those boundaries.
Parse failures raise ``InvalidOutput``/``EmbeddingMismatch`` without echoing content.
"""
import math

from .errors import EmbeddingMismatch, InvalidOutput


def _token_count(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _vectors(raw, count, dimensions):
    if not isinstance(raw, list) or len(raw) != count:
        raise EmbeddingMismatch()
    if dimensions is None:
        # Only connection tests of an undeclared capability observe dimensions; the batch
        # must still be uniform and non-empty.
        if not raw or not isinstance(raw[0], list) or not raw[0]:
            raise EmbeddingMismatch()
        dimensions = len(raw[0])
    vectors = []
    for vector in raw:
        if not isinstance(vector, list) or len(vector) != dimensions:
            raise EmbeddingMismatch()
        clean = []
        for value in vector:
            if type(value) not in (int, float) or abs(value) > 1e308 or not math.isfinite(value):
                raise EmbeddingMismatch()
            clean.append(float(value))
        vectors.append(clean)
    return vectors


class OllamaAdapter:
    name = 'ollama'
    chat_path = '/api/chat'
    embed_path = '/api/embed'

    def auth_headers(self, secret):
        return {'Authorization': 'Bearer ' + secret} if secret else {}

    def chat_body(self, model, messages, *, schema, max_tokens, context_limit):
        body = {'model': model, 'messages': messages, 'stream': False,
                'options': {'num_predict': max_tokens, 'num_ctx': context_limit}}
        if schema is not None:
            body['format'] = schema
            # Thinking models (e.g. qwen3) otherwise reason first and can spend the whole
            # num_predict budget before emitting JSON; structured replies need no visible reasoning.
            body['think'] = False
        return body

    def parse_chat(self, payload):
        if (not isinstance(payload, dict) or payload.get('done') is False
                or payload.get('done_reason') == 'length'):
            raise InvalidOutput()
        message = payload.get('message')
        if not isinstance(message, dict) or not isinstance(message.get('content'), str):
            raise InvalidOutput()
        usage = {'input_tokens': _token_count(payload.get('prompt_eval_count')),
                 'output_tokens': _token_count(payload.get('eval_count'))}
        return message['content'], usage

    def embed_body(self, model, texts):
        # truncate=False: an over-long input is an error, never a silently different vector.
        return {'model': model, 'input': texts, 'truncate': False}

    def parse_embed(self, payload, count, dimensions):
        if not isinstance(payload, dict):
            raise InvalidOutput()
        if 'embeddings' not in payload:
            raise InvalidOutput()
        vectors = _vectors(payload['embeddings'], count, dimensions)
        return vectors, {'input_tokens': _token_count(payload.get('prompt_eval_count')),
                         'output_tokens': None}


class OpenAICompatibleAdapter:
    name = 'openai_compatible'
    chat_path = '/chat/completions'
    embed_path = '/embeddings'

    def auth_headers(self, secret):
        return {'Authorization': 'Bearer ' + secret} if secret else {}

    def chat_body(self, model, messages, *, schema, max_tokens, context_limit):
        body = {'model': model, 'messages': messages, 'stream': False, 'max_tokens': max_tokens}
        if schema is not None:
            # A model hint only; the service parses JSON and callers validate domain schema.
            body['response_format'] = {'type': 'json_schema',
                                       'json_schema': {'name': 'output', 'schema': schema}}
        return body

    def parse_chat(self, payload):
        if not isinstance(payload, dict):
            raise InvalidOutput()
        choices = payload.get('choices')
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise InvalidOutput()
        if choices[0].get('finish_reason') in ('length', 'content_filter'):
            raise InvalidOutput()
        message = choices[0].get('message')
        if not isinstance(message, dict) or not isinstance(message.get('content'), str):
            raise InvalidOutput()
        usage = payload.get('usage') if isinstance(payload.get('usage'), dict) else {}
        return message['content'], {'input_tokens': _token_count(usage.get('prompt_tokens')),
                                    'output_tokens': _token_count(usage.get('completion_tokens'))}

    def embed_body(self, model, texts):
        return {'model': model, 'input': texts, 'encoding_format': 'float'}

    def parse_embed(self, payload, count, dimensions):
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
            raise InvalidOutput()
        data = payload['data']
        if len(data) != count:
            raise EmbeddingMismatch()
        ordered = [None] * count
        for item in data:
            if not isinstance(item, dict):
                raise InvalidOutput()
            index = item.get('index')
            if type(index) is not int or not 0 <= index < count or ordered[index] is not None:
                raise EmbeddingMismatch()
            ordered[index] = item.get('embedding')
        vectors = _vectors(ordered, count, dimensions)
        usage = payload.get('usage') if isinstance(payload.get('usage'), dict) else {}
        return vectors, {'input_tokens': _token_count(usage.get('prompt_tokens')), 'output_tokens': None}


class ClaudeCliAdapter(OpenAICompatibleAdapter):
    """Claude Code CLI on the user's own login (``claude_cli.ClaudeCliTransport``).

    The service routes this adapter to the CLI transport, never the network transport.
    Responses use the OpenAI chat shape, so parsing is inherited. No embeddings.
    """
    name = 'claude_cli'
    chat_path = '/claude-cli/chat'
    embed_path = '/claude-cli/embed'

    def auth_headers(self, secret):
        return {}

    def chat_body(self, model, messages, *, schema, max_tokens, context_limit):
        body = {'model': model, 'messages': messages}
        if schema is not None:
            body['schema'] = schema
        return body

    def embed_body(self, model, texts):
        raise InvalidOutput()


ADAPTER_IMPLEMENTATIONS = {'ollama': OllamaAdapter(), 'openai_compatible': OpenAICompatibleAdapter(),
                           'claude_cli': ClaudeCliAdapter()}
