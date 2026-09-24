import os
import json
from unittest.mock import patch
from django.test import TestCase

from review.services.ai_provider import ai_chat_json

class AIProviderDispatcherTests(TestCase):
    @patch('review.services.ai_provider.AI_PROVIDER', 'mock')
    def test_mock_provider_routing(self):
        model, result = ai_chat_json("Test prompt")
        self.assertEqual(model, "mock")
        self.assertEqual(result, "{}")

    @patch('review.services.ai_provider.AI_PROVIDER', 'ollama')
    @patch('review.services.ai_provider.ollama_chat_json')
    def test_ollama_provider_routing(self, mock_ollama):
        mock_ollama.return_value = ("qwen2.5", '{"status": "ok"}')
        model, result = ai_chat_json("Test prompt")
        self.assertEqual(model, "qwen2.5")
        self.assertEqual(result, '{"status": "ok"}')
        mock_ollama.assert_called_once()

    @patch('review.services.ai_provider.AI_PROVIDER', 'anthropic')
    @patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'fake-key'})
    @patch('review.services.ai_provider._anthropic_chat_json')
    def test_anthropic_provider_routing(self, mock_anthropic):
        mock_anthropic.return_value = ("claude-haiku", '{"status": "ok"}')
        model, result = ai_chat_json("Test prompt")
        self.assertEqual(model, "claude-haiku")
        self.assertEqual(result, '{"status": "ok"}')
        mock_anthropic.assert_called_once()
