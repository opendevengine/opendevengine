"""Ollama request bodies: structured output turns thinking off; plain chat is left alone."""
import unittest

from opendevengine.providers.adapters import OllamaAdapter, OpenAICompatibleAdapter

MESSAGES = [{'role': 'user', 'content': 'synthetic'}]
SCHEMA = {'type': 'object', 'properties': {'steps': {'type': 'array'}}, 'required': ['steps']}


class OllamaThinkTests(unittest.TestCase):
    def test_structured_output_disables_thinking(self):
        body = OllamaAdapter().chat_body('qwen3:4b', MESSAGES, schema=SCHEMA, max_tokens=1024, context_limit=8192)
        self.assertEqual(SCHEMA, body['format'])
        self.assertIs(False, body['think'])

    def test_plain_chat_keeps_the_model_default(self):
        body = OllamaAdapter().chat_body('qwen3:4b', MESSAGES, schema=None, max_tokens=1024, context_limit=8192)
        self.assertNotIn('think', body)
        self.assertNotIn('format', body)

    def test_openai_compatible_bodies_are_unchanged(self):
        body = OpenAICompatibleAdapter().chat_body('m', MESSAGES, schema=SCHEMA, max_tokens=1024, context_limit=8192)
        self.assertNotIn('think', body)


if __name__ == '__main__':
    unittest.main()
