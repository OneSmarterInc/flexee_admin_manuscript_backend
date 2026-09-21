import json
import unittest
from unittest.mock import patch

import httpx

from review.services.review_engine import (
    ARTICLE_JUDGMENT,
    _chapter_data,
    structural_checks,
    word_count,
)


class EngineTests(unittest.TestCase):
    def test_word_count(self):
        self.assertEqual(word_count('one two three'), 3)

    def test_ollama_client_uses_local_qwen_and_json_mode(self):
        response = httpx.Response(
            200,
            json={"message": {"role": "assistant", "content": json.dumps({"ok": True})}},
        )
        with patch("review.services.local_llm.httpx.post", return_value=response) as post:
            from review.services.local_llm import ollama_chat_json
            model, content = ollama_chat_json("Return JSON", max_tokens=123)
        self.assertEqual(model, "qwen3:1.7b")
        self.assertEqual(json.loads(content), {"ok": True})
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "qwen3:1.7b")
        self.assertFalse(payload["think"])
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["format"], "json")
        self.assertEqual(payload["options"]["num_predict"], 123)
        self.assertEqual(payload["options"]["num_ctx"], 8192)

    def test_ollama_client_reports_unavailable_server(self):
        from review.services.local_llm import ollama_chat_json
        with patch(
            "review.services.local_llm.httpx.post",
            side_effect=httpx.ConnectError("connection refused"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Could not connect to Ollama"):
                ollama_chat_json("Return JSON")

    def test_article_structural_pass(self):
        text = ' '.join(f'word{i}' for i in range(1700))
        measured = structural_checks(text, 'article')
        self.assertTrue(measured['checks'][0]['passed'])

    def test_book_pdf_running_headers_do_not_create_extra_chapters(self):
        """Regression for PDFs that repeat "Chapter N" at the top of every page."""
        lines = ['Contents']
        # Compact TOC: this must NOT be chosen as the real chapter sequence.
        for number in range(1, 13):
            lines.append(f'Chapter {number} ........ {number + 3}')

        # Actual manuscript body.  Chapter 1 is intentionally lighter; chapters
        # 2-12 sit in the configured Five Zero body band.
        for number in range(1, 13):
            lines.append(f'Chapter {number} Test Chapter {number}')
            target = 1500 if number == 1 else 2200
            first_half = target // 2
            second_half = target - first_half
            lines.append(' '.join(f'c{number}a{i}' for i in range(first_half)))
            # Simulate a repeated running page header from PDF extraction.
            lines.append(f'Chapter {number} Test Chapter {number}')
            lines.append(' '.join(f'c{number}b{i}' for i in range(second_half)))

        lines.extend(['References', 'Reference one', 'Reference two'])
        text = '\n'.join(lines)

        chapters = _chapter_data(text)
        self.assertEqual(len(chapters), 12)
        self.assertEqual([c.get('number') for c in chapters], list(range(1, 13)))
        self.assertEqual(chapters[0]['word_count'], 1500)
        self.assertTrue(all(c['word_count'] == 2200 for c in chapters[1:]))

        measured = structural_checks(text, 'book')
        checks = {item['id']: item for item in measured['checks']}
        self.assertTrue(checks['chapter_count']['passed'])
        self.assertTrue(checks['balance']['passed'])
        self.assertIn('Found 12 chapters', checks['chapter_count']['detail'])

    def test_incomplete_body_beats_complete_table_of_contents(self):
        """A 12-line TOC must not hide an actual body that only has 11 chapters."""
        lines = ['Contents']
        for number in range(1, 13):
            lines.append(f'Chapter {number} ........ {number + 10}')

        for number in range(1, 12):
            lines.append(f'Chapter {number} Body Title')
            target = 1500 if number == 1 else 2200
            lines.append(' '.join(f'body{number}_{i}' for i in range(target)))
            lines.append(f'Chapter {number} Body Title')

        chapters = _chapter_data('\n'.join(lines))
        self.assertEqual(len(chapters), 11)
        self.assertEqual([c.get('number') for c in chapters], list(range(1, 12)))

    def test_chapter_numbers_above_twelve_are_not_counted(self):
        lines = []
        for number in range(1, 13):
            lines.extend([
                f'Chapter {number} Body Title',
                ' '.join(f'w{number}_{i}' for i in range(20)),
            ])
        lines.extend(['References', 'Chapter 13 Not A Five Zero Chapter', 'extra words'])
        chapters = _chapter_data('\n'.join(lines))
        self.assertEqual(len(chapters), 12)
        self.assertEqual(chapters[-1].get('number'), 12)

    def test_markdown_subheadings_are_not_mixed_with_chapters(self):
        lines = ['# Book title']
        for number in range(1, 13):
            lines.append(f'## Section {number}')
            lines.append(' '.join(f'm{number}_{i}' for i in range(20)))
            lines.append(f'### Subheading {number}')
            lines.append('detail words here')
        chapters = _chapter_data('\n'.join(lines))
        self.assertEqual(len(chapters), 12)
        self.assertEqual(chapters[0]['title'], 'Section 1')


if __name__ == '__main__':
    unittest.main()
