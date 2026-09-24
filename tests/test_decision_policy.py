import json
import os
import unittest
from unittest.mock import patch

import httpx

from review.services.review_engine import (
    ARTICLE_JUDGMENT,
    DECISION_FAIL,
    DECISION_PASS,
    DECISION_REFER,
    compute_decision,
    judge_with_local_model,
)


class DecisionPolicyTests(unittest.TestCase):
    def test_compute_decision_structural_failure_returns_to_author(self):
        measured = {
            'checks': [
                {'id': 'total_words', 'passed': False, 'detail': 'Too short.', 'advisory': False},
            ]
        }
        judgments = [
            {'id': 'four_parts', 'verdict': 'pass', 'advisory': False},
        ]

        self.assertEqual(compute_decision(measured, judgments), DECISION_FAIL)

    def test_compute_decision_non_advisory_failure_returns_to_author(self):
        measured = {
            'checks': [
                {'id': 'total_words', 'passed': True, 'detail': 'Inside range.', 'advisory': False},
            ]
        }
        judgments = [
            {'id': 'four_parts', 'verdict': 'fail', 'advisory': False},
            {'id': 'ai_authored_signal', 'verdict': 'pass', 'advisory': True},
        ]

        self.assertEqual(compute_decision(measured, judgments), DECISION_FAIL)

    def test_compute_decision_needs_work_refers_to_human(self):
        measured = {
            'checks': [
                {'id': 'total_words', 'passed': True, 'detail': 'Inside range.', 'advisory': False},
            ]
        }
        judgments = [
            {'id': 'four_parts', 'verdict': 'pass', 'advisory': False},
            {'id': 'ai_disclosure', 'verdict': 'needs_work', 'advisory': False},
        ]

        self.assertEqual(compute_decision(measured, judgments), DECISION_REFER)

    def test_compute_decision_clean_review_passes_to_human(self):
        measured = {
            'checks': [
                {'id': 'total_words', 'passed': True, 'detail': 'Inside range.', 'advisory': False},
            ]
        }
        judgments = [
            {'id': 'four_parts', 'verdict': 'pass', 'advisory': False},
            {'id': 'journal_fit', 'verdict': 'pass', 'advisory': False},
        ]

        self.assertEqual(compute_decision(measured, judgments), DECISION_PASS)

    def test_local_model_decision_is_ignored_when_structure_fails(self):
        measured = {
            'total_words': 100,
            'chapters': [],
            'figures': 0,
            'checks': [
                {'id': 'total_words', 'label': 'Length', 'passed': False, 'detail': 'Too short.', 'advisory': False},
            ],
        }
        model_payload = {
            'decision': DECISION_PASS,
            'editor_summary': 'The model incorrectly says this should pass.',
            'author_letter': 'The model incorrectly says this is ready.',
            'items': [
                {'id': item['id'], 'verdict': 'pass', 'evidence': 'Model evidence.', 'gap': ''}
                for item in ARTICLE_JUDGMENT
            ],
        }
        response = httpx.Response(
            200,
            json={'message': {'role': 'assistant', 'content': json.dumps(model_payload)}},
        )

        with patch.dict(os.environ, {'MOCK_AI_REVIEW': 'false', 'OLLAMA_NUM_CTX': '8192', 'OLLAMA_NUM_PREDICT': '900'}), \
             patch('review.services.local_llm.httpx.post', return_value=response):
            _model, _items, decision, editor_summary, _author_letter, _metadata = judge_with_local_model(
                'Short article text.\n\nAI-Use Disclosure: AI was used for editing only.',
                '',
                ARTICLE_JUDGMENT,
                'article',
                'AI was used for editing only.',
                measured,
            )

        self.assertEqual(decision, DECISION_FAIL)
        self.assertIn('Decision: RETURN_TO_AUTHOR', editor_summary)

    def test_local_model_decision_is_ignored_when_required_item_fails(self):
        measured = {
            'total_words': 1700,
            'chapters': [],
            'figures': 0,
            'checks': [
                {'id': 'total_words', 'label': 'Length', 'passed': True, 'detail': 'Inside range.', 'advisory': False},
            ],
        }
        model_items = []
        for item in ARTICLE_JUDGMENT:
            verdict = 'fail' if item['id'] == 'four_parts' else 'pass'
            model_items.append({'id': item['id'], 'verdict': verdict, 'evidence': 'Model evidence.', 'gap': 'Missing required substance.' if verdict == 'fail' else ''})
        response = httpx.Response(
            200,
            json={'message': {'role': 'assistant', 'content': json.dumps({
                'decision': DECISION_PASS,
                'editor_summary': 'The model incorrectly says this should pass.',
                'author_letter': 'The model incorrectly says this is ready.',
                'items': model_items,
            })}},
        )

        with patch.dict(os.environ, {'MOCK_AI_REVIEW': 'false', 'OLLAMA_NUM_CTX': '8192', 'OLLAMA_NUM_PREDICT': '900'}), \
             patch('review.services.local_llm.httpx.post', return_value=response):
            _model, _items, decision, editor_summary, _author_letter, _metadata = judge_with_local_model(
                'Article text.\n\nAI-Use Disclosure: AI was used for editing only.',
                '',
                ARTICLE_JUDGMENT,
                'article',
                'AI was used for editing only.',
                measured,
            )

        self.assertEqual(decision, DECISION_FAIL)
        self.assertIn('Decision: RETURN_TO_AUTHOR', editor_summary)


if __name__ == '__main__':
    unittest.main()
