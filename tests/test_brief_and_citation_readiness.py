import tempfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from review.models import (
    Author,
    Manuscript,
    Organization,
    ReadinessAssessment,
    Venue,
    VenueAgentConfig,
    VenueSubmission,
)
from review.services.author_agents import run_semantic_readiness, run_venue_assessment


MANUSCRIPT_BODY = (
    b'# Agentic Operations\n\n'
    b'## Abstract\nThis study evaluates AI agents in manufacturing operations.\n\n'
    b'## Methods\nWe compare a controlled pilot with the prior manual workflow.\n\n'
    b'## Results\nThe pilot reduced handling time in the observed workflow.\n\n'
    b'## References\nSmith J. A study of agents. Journal of Operations, 2023.\n'
)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='flexee-v8-tests-'))
class CitationReadinessTests(TestCase):
    def _manuscript(self):
        author = Author.objects.create(
            email='citation-author@example.com',
            name='Citation Author',
            password_hash='x',
            email_verified=True,
        )
        manuscript = Manuscript.objects.create(
            author_account=author,
            author_name=author.name,
            author_email=author.email,
            title='Agentic Operations',
            manuscript_type='research_article',
            disclosure='AI was used for copy editing only.',
            manuscript_filename='agentic-operations.md',
            manuscript_file=SimpleUploadedFile(
                'agentic-operations.md',
                MANUSCRIPT_BODY,
                content_type='text/markdown',
            ),
            manuscript_bytes=len(MANUSCRIPT_BODY),
            manuscript_sha256='a' * 64,
        )
        ReadinessAssessment.objects.create(
            manuscript=manuscript,
            status='completed',
            engine_version='mechanical-v1',
            summary={
                'word_count': 42,
                'blocking_issues': 0,
                'warnings': 0,
                'ready_for_matching': True,
            },
            findings=[],
        )
        return manuscript

    @patch('review.services.author_agents.ai_available', return_value=False)
    @patch('review.services.author_agents._citation_checks')
    def test_semantic_readiness_reports_sampled_citation_integrity(
        self,
        mock_citations,
        _mock_ai_available,
    ):
        manuscript = self._manuscript()
        mock_citations.return_value = {
            'total_references': 4,
            'checked': 2,
            'results': [
                {'citation': 'Real paper', 'status': 'verified'},
                {'citation': 'Invented paper', 'status': 'not found'},
            ],
        }

        assessment = run_semantic_readiness(manuscript)

        self.assertEqual(assessment.status, 'completed')
        self.assertTrue(assessment.summary['ready_for_matching'])
        self.assertEqual(assessment.summary['citation_integrity']['checked'], 2)

        finding = next(
            item for item in assessment.findings
            if item.get('code') == 'citation_integrity'
        )
        self.assertEqual(finding['status'], 'warning')
        self.assertEqual(finding['source']['type'], 'external')
        self.assertEqual(assessment.summary['warnings'], 1)

    @patch('review.services.author_agents.ai_available', return_value=False)
    @patch(
        'review.services.author_agents._citation_checks',
        side_effect=RuntimeError('Crossref unreachable'),
    )
    def test_semantic_readiness_survives_crossref_outage(
        self,
        _mock_citations,
        _mock_ai_available,
    ):
        manuscript = self._manuscript()

        assessment = run_semantic_readiness(manuscript)

        self.assertEqual(assessment.status, 'completed')
        self.assertTrue(assessment.summary['ready_for_matching'])
        self.assertTrue(
            assessment.summary['citation_integrity'].get('unavailable')
        )

    @patch('review.services.author_agents.ai_available', return_value=False)
    @patch('review.services.author_agents._citation_checks')
    def test_venue_assessment_reuses_readiness_citations_and_keeps_human_authority(
        self,
        mock_citations,
        _mock_ai_available,
    ):
        manuscript = self._manuscript()
        stored_citations = {
            'total_references': 3,
            'checked': 1,
            'results': [
                {
                    'citation': 'Stored citation',
                    'status': 'verified',
                    'matched_title': 'Stored citation',
                    'score': 1.0,
                    'title_similarity': 1.0,
                    'doi': '10.1000/test',
                }
            ],
        }
        ReadinessAssessment.objects.create(
            manuscript=manuscript,
            status='completed',
            engine_version='author-agents-v1:semantic-readiness:mock',
            summary={
                'ready_for_matching': True,
                'citation_integrity': stored_citations,
            },
            findings=[],
        )
        manuscript.parsed_profile = {
            'semantic': {
                'summary': 'Semantic profile available.',
                'topics': ['AI agents'],
                'methods': ['controlled pilot'],
                'contributions': ['operational evidence'],
                'limitations': ['single site'],
                'coverage': {'complete': True},
            }
        }
        manuscript.save(update_fields=['parsed_profile', 'updated_at'])

        org = Organization.objects.create(
            name='Citation Org',
            organization_type='journal',
        )
        venue = Venue.objects.create(
            organization=org,
            name='Citation Venue',
            slug='citation-venue',
            venue_type='journal',
        )
        config = VenueAgentConfig.objects.create(
            venue=venue,
            version=1,
            active=True,
            aims_scope='Applied AI in operations.',
        )
        submission = VenueSubmission.objects.create(
            manuscript=manuscript,
            venue=venue,
            venue_config=config,
            status='draft',
        )

        result = run_venue_assessment(submission)
        result.refresh_from_db()

        mock_citations.assert_not_called()
        self.assertEqual(
            result.editorial_brief['external_reference_check'],
            stored_citations,
        )
        self.assertTrue(result.editorial_brief['human_decision_required'])
        self.assertIn(
            'human editor',
            result.editorial_brief['decision_authority'].lower(),
        )
