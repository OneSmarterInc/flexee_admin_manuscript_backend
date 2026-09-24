import re
import os

with open(r'd:\flexee\flexee_admin_manuscript_backend\tests\test_review_engine.py', 'r', encoding='utf-8') as f:
    content = f.read()

if "import os" not in content:
    content = content.replace("import json", "import json\nimport os")

new_tests = """
    @patch.dict(os.environ, {"AI_PROVIDER": "ollama", "ENABLE_CLOUD_FALLBACK": "true", "ANTHROPIC_API_KEY": "fake", "OLLAMA_NUM_CTX": "4096", "OLLAMA_NUM_PREDICT": "2000"})
    @patch("review.services.review_engine.ai_chat_json")
    @patch("review.services.review_engine.run_field_agent")
    def test_large_manuscript_cloud_fallback_enabled(self, mock_field, mock_ai_chat):
        from review.services.review_engine import run_review
        mock_field.return_value = ""
        mock_ai_chat.return_value = ("claude-haiku", '{"decision": "PASS_TO_HUMAN", "editor_summary": "Sum", "author_letter": "Let", "items": []}')
        
        # We need chapter structure so chunking or normal review can happen
        # For book, it requires chapter numbers
        lines = []
        for i in range(1, 13):
            lines.append(f"Chapter {i} Title")
            lines.append("word " * 2500)
        text = "\\n".join(lines)
        
        result = run_review(text.encode("utf-8"), "book.md", "book")
        self.assertEqual(result["record"]["mode"], "cloud-full")
        self.assertEqual(result["record"]["provider"], "anthropic")
        mock_ai_chat.assert_called_once()
        self.assertEqual(mock_ai_chat.call_args.kwargs.get("force_provider"), "anthropic")

    @patch.dict(os.environ, {"AI_PROVIDER": "ollama", "ENABLE_CLOUD_FALLBACK": "false", "OLLAMA_NUM_CTX": "4096", "OLLAMA_NUM_PREDICT": "2000"})
    @patch("review.services.review_engine.ai_chat_json")
    @patch("review.services.review_engine.run_field_agent")
    def test_large_manuscript_cloud_fallback_disabled(self, mock_field, mock_ai_chat):
        from review.services.review_engine import run_review
        mock_field.return_value = ""
        mock_ai_chat.return_value = ("qwen2.5", '{"decision": "PASS_TO_HUMAN", "editor_summary": "Sum", "author_letter": "Let", "items": []}')
        
        lines = []
        for i in range(1, 13):
            lines.append(f"Chapter {i} Title")
            lines.append("word " * 2500)
        text = "\\n".join(lines)
        
        result = run_review(text.encode("utf-8"), "book.md", "book")
        self.assertEqual(result["record"]["mode"], "chunked")
        self.assertGreater(result["record"]["chunk_count"], 1)
        self.assertGreater(mock_ai_chat.call_count, 1)
"""

# Append just before the main block
content = content.replace("if __name__ == '__main__':", new_tests + "\nif __name__ == '__main__':")

with open(r'd:\flexee\flexee_admin_manuscript_backend\tests\test_review_engine.py', 'w', encoding='utf-8') as f:
    f.write(content)
