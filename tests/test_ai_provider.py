import os
import json
from unittest.mock import patch
from django.test import TestCase

from review.services.ai_provider import ai_chat_json

class AIProviderDispatcherTests(TestCase):
    @patch.dict(os.environ, {'AI_PROVIDER': 'mock'})
    def test_mock_provider_routing(self):
        model, result = ai_chat_json("Test prompt")
        self.assertEqual(model, "mock")
        self.assertEqual(result, "{}")

    @patch.dict(os.environ, {'AI_PROVIDER': 'ollama'})
    @patch('review.services.ai_provider.ollama_chat_json')
    def test_ollama_provider_routing(self, mock_ollama):
        mock_ollama.return_value = ("qwen2.5", '{"status": "ok"}')
        model, result = ai_chat_json("Test prompt")
        self.assertEqual(model, "qwen2.5")
        self.assertEqual(result, '{"status": "ok"}')
        mock_ollama.assert_called_once()

    @patch.dict(os.environ, {'AI_PROVIDER': 'anthropic', 'ANTHROPIC_API_KEY': 'fake-key'})
    @patch('review.services.ai_provider._anthropic_chat_json')
    def test_anthropic_provider_routing(self, mock_anthropic):
        mock_anthropic.return_value = ("claude-haiku", '{"status": "ok"}')
        model, result = ai_chat_json("Test prompt")
        self.assertEqual(model, "claude-haiku")
        self.assertEqual(result, '{"status": "ok"}')
        mock_anthropic.assert_called_once()

class SharedQwenProviderDispatcherTests(TestCase):
    @patch.dict(os.environ, {'AI_PROVIDER': 'shared_qwen', 'SHARED_QWEN_QUEUE_ENABLED': 'true'})
    @patch('review.services.ai_provider.shared_qwen_chat_json')
    def test_shared_qwen_provider_routing(self, mock_shared):
        mock_shared.return_value = ("qwen2.5-shared", '{"status": "ok"}')
        model, result = ai_chat_json("Test prompt")
        self.assertEqual(model, "qwen2.5-shared")
        self.assertEqual(result, '{"status": "ok"}')
        mock_shared.assert_called_once()

