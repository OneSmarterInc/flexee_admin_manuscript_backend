import unittest
from unittest.mock import patch

import httpx

from review.services.field_agent import (
    _extract_citations,
    _extract_references_section,
    _title_similarity,
    _verify_citation_crossref,
    run_field_agent,
)


class FieldAgentCitationTests(unittest.TestCase):
    def test_extracts_references_section_before_citation_parsing(self):
        manuscript = "\n".join([
            "Chapter 1",
            "This body text mentions Smith (2020), but it is not the references section.",
            "Chapter 2",
            "More body text " * 200,
            "References",
            "1. Vaswani, A., Shazeer, N., et al. (2017). Attention is All You Need. Advances in Neural Information Processing Systems.",
            "2. Brown, T. B. et al. (2020). Language Models are Few-Shot Learners. Advances in Neural Information Processing Systems.",
            "Appendix A",
            "This appendix should not be treated as references.",
        ])

        section = _extract_references_section(manuscript)
        self.assertIn("Attention is All You Need", section)
        self.assertIn("Language Models are Few-Shot Learners", section)
        self.assertNotIn("This appendix", section)
        self.assertNotIn("body text mentions Smith", section)

        total, citations = _extract_citations(manuscript)
        self.assertEqual(total, 2)
        self.assertEqual(len(citations), 2)

    def test_crossref_verified_requires_score_and_title_similarity(self):
        response = httpx.Response(
            200,
            json={
                "message": {
                    "items": [
                        {"title": ["Attention Is All You Need"], "score": 80.0},
                    ]
                }
            },
        )
        citation = "Vaswani, A. et al. (2017). Attention is All You Need. Advances in Neural Information Processing Systems."
        with patch("review.services.field_agent.httpx.get", return_value=response):
            result = _verify_citation_crossref(citation)
        self.assertEqual(result["status"], "verified")
        self.assertGreaterEqual(result["title_similarity"], 0.72)

    def test_crossref_low_score_same_title_is_weak_match(self):
        response = httpx.Response(
            200,
            json={
                "message": {
                    "items": [
                        {"title": ["Attention Is All You Need"], "score": 18.0},
                    ]
                }
            },
        )
        citation = "Vaswani, A. et al. (2017). Attention is All You Need. Advances in Neural Information Processing Systems."
        with patch("review.services.field_agent.httpx.get", return_value=response):
            result = _verify_citation_crossref(citation)
        self.assertEqual(result["status"], "weak match")

    def test_crossref_unrelated_best_match_is_not_found(self):
        response = httpx.Response(
            200,
            json={
                "message": {
                    "items": [
                        {"title": ["Attention Is All You Need"], "score": 75.0},
                    ]
                }
            },
        )
        invented = "Doe, J. (2024). Unicorn Ledgers and Quantum Sandwich Auditing. Journal of Impossible Business Systems."
        with patch("review.services.field_agent.httpx.get", return_value=response):
            result = _verify_citation_crossref(invented)
        self.assertEqual(result["status"], "not found")
        self.assertLess(result["title_similarity"], 0.45)

    def test_crossref_no_items_is_not_found(self):
        response = httpx.Response(200, json={"message": {"items": []}})
        with patch("review.services.field_agent.httpx.get", return_value=response):
            result = _verify_citation_crossref("Missing citation")
        self.assertEqual(result["status"], "not found")

    def test_title_similarity_detects_title_inside_full_citation(self):
        citation = "Vaswani, A. et al. (2017). Attention is All You Need. Advances in Neural Information Processing Systems."
        score = _title_similarity(citation, "Attention Is All You Need")
        self.assertEqual(score, 1.0)

    def test_field_briefing_reports_three_states(self):
        manuscript = "\n".join([
            "References",
            "1. Vaswani, A. et al. (2017). Attention is All You Need. Advances in Neural Information Processing Systems.",
            "2. Brown, T. B. et al. (2020). Language Models are Few-Shot Learners. Advances in Neural Information Processing Systems.",
            "3. Doe, J. (2024). Unicorn Ledgers and Quantum Sandwich Auditing. Journal of Impossible Business Systems.",
        ])
        fake_results = [
            {"status": "verified", "matched_title": "Attention Is All You Need", "score": 80.0, "title_similarity": 1.0},
            {"status": "weak match", "matched_title": "Language Models are Few-Shot Learners", "score": 18.0, "title_similarity": 1.0},
            {"status": "not found", "matched_title": None, "score": 0.0, "title_similarity": 0.0},
        ]

        with patch("review.services.field_agent.random.sample", side_effect=lambda items, size: items[:size]):
            with patch("review.services.field_agent._verify_citation_crossref", side_effect=fake_results):
                briefing = run_field_agent(manuscript)

        self.assertIn("1 verified", briefing)
        self.assertIn("1 weak match", briefing)
        self.assertIn("1 not found", briefing)
        self.assertIn("[verified]", briefing)
        self.assertIn("[weak match]", briefing)
        self.assertIn("[not found]", briefing)


if __name__ == '__main__':
    unittest.main()
