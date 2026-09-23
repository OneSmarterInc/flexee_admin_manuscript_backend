import json
import tempfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from review.models import EvidenceFinding, Manuscript, Organization, Venue, VenueAgentConfig, VenueMatch


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='flexee-author-agent-tests-'))
class AuthorAgentApiTests(TestCase):
    def _create_manuscript(self):
        upload = SimpleUploadedFile(
            'agent-paper.md',
            (
                b'# Agentic Operations\n\n'
                b'## Abstract\nThis study evaluates AI agents in manufacturing operations.\n\n'
                b'## Methods\nWe compare a controlled pilot with the prior manual workflow.\n\n'
                b'## Results\nThe pilot reduced handling time in the observed workflow.\n\n'
                b'## Limitations\nThe study uses one manufacturing site.\n\n'
                b'## References\nOne reference.\n\n'
                b'## Data Availability\nAggregate data are available on request.\n'
            ),
            content_type='text/markdown',
        )
        response = self.client.post('/api/author/manuscripts/', {
            'title': 'Agentic Operations',
            'author': 'Test Author',
            'email': 'author@example.com',
            'manuscript_type': 'research_article',
            'abstract': 'This study evaluates AI agents in manufacturing operations.',
            'keywords': 'AI agents, manufacturing, operations',
            'disclosure': 'AI was used for copy editing only.',
            'attestation': 'true',
            'manuscript': upload,
        })
        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()
        manuscript = Manuscript.objects.get(id=payload['manuscript']['id'])
        manuscript._access_token = payload['access_token']
        return manuscript

    def _auth(self, manuscript):
        return {'HTTP_X_MANUSCRIPT_TOKEN': manuscript._access_token}

    def _create_venues(self):
        org = Organization.objects.create(name='Agent Test Publisher', organization_type='journal')
        first = Venue.objects.create(
            organization=org,
            name='Applied AI Review',
            slug='agent-applied-ai-review',
            venue_type='journal',
        )
        VenueAgentConfig.objects.create(
            venue=first,
            version=1,
            aims_scope='Applied AI studies with operational evidence.',
            article_types=['Research article'],
            accepted_methods=['Controlled pilot', 'Case study'],
            quality_threshold='Contribution and methods must be clear enough for human peer review.',
            reviewer_criteria=['Applied AI', 'Operations management'],
            policies={'data_availability': 'Required when data support empirical claims.'},
            reporting_standards=['State limitations and data availability.'],
            current_demand={'topics': ['AI agents', 'enterprise AI']},
        )
        second = Venue.objects.create(
            organization=org,
            name='Systems Conference',
            slug='agent-systems-conference',
            venue_type='conference',
        )
        VenueAgentConfig.objects.create(
            venue=second,
            version=1,
            aims_scope='Enterprise systems conference papers.',
            article_types=['Conference paper'],
            accepted_methods=['Case study'],
        )
        return first, second

    def _run_mechanical(self, manuscript):
        response = self.client.post(
            f'/api/author/manuscripts/{manuscript.id}/readiness/run/',
            **self._auth(manuscript),
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['readiness']

    @patch('review.services.author_agents.ollama_chat_json')
    def test_semantic_readiness_persists_grounded_profile_and_evidence(self, mock_chat):
        manuscript = self._create_manuscript()
        self._run_mechanical(manuscript)
        mock_chat.return_value = ('mock-qwen', json.dumps({
            'summary': 'The chunk describes an applied manufacturing AI-agent study.',
            'topics': ['AI agents', 'manufacturing operations'],
            'methods': ['controlled pilot comparison'],
            'contributions': ['operational evidence about AI-agent use'],
            'limitations': ['single manufacturing site'],
            'findings': [
                {
                    'code': 'limitations_visible',
                    'label': 'Limitations are stated',
                    'status': 'pass',
                    'detail': 'A single-site limitation is explicitly stated.',
                    'line_start': 10,
                    'line_end': 10,
                }
            ],
        }))

        response = self.client.post(
            f'/api/author/manuscripts/{manuscript.id}/readiness/semantic/',
            **self._auth(manuscript),
        )
        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()['readiness']

        self.assertEqual(payload['status'], 'completed')
        self.assertIn('semantic-readiness:mock-qwen', payload['engine_version'])
        self.assertTrue(payload['summary']['semantic_advisory_only'])
        self.assertIn('semantic_profile', payload['summary'])

        manuscript.refresh_from_db()
        self.assertEqual(manuscript.parsed_profile['semantic_model'], 'mock-qwen')
        self.assertIn('AI agents', manuscript.parsed_profile['semantic']['topics'])
        self.assertTrue(
            EvidenceFinding.objects.filter(
                manuscript=manuscript,
                finding_type='agent:readiness:limitations_visible',
                source_type='manuscript',
            ).exists()
        )

    @patch('review.services.author_agents.ollama_chat_json')
    def test_semantic_matching_explains_each_venue_without_overriding_policy_gate(self, mock_chat):
        manuscript = self._create_manuscript()
        first, second = self._create_venues()
        self._run_mechanical(manuscript)
        manuscript.parsed_profile = {
            **manuscript.parsed_profile,
            'semantic': {
                'summary': 'Applied AI agents in manufacturing operations.',
                'topics': ['AI agents', 'manufacturing operations'],
                'methods': ['controlled pilot comparison'],
                'contributions': ['operational implementation evidence'],
                'limitations': ['single site'],
                'coverage': {'complete': True},
            },
        }
        manuscript.save(update_fields=['parsed_profile', 'updated_at'])

        gate = self.client.post(
            f'/api/author/manuscripts/{manuscript.id}/matches/run/',
            **self._auth(manuscript),
        )
        self.assertEqual(gate.status_code, 201, gate.content)
        before = {m.venue.slug: m.eligibility for m in VenueMatch.objects.filter(manuscript=manuscript).select_related('venue')}

        mock_chat.side_effect = [
            ('mock-qwen', json.dumps({
                'fit_summary': 'The manuscript topic and method align with the configured applied-AI scope.',
                'reasons': [{
                    'text': 'The manuscript focuses on applied AI in operations.',
                    'venue_fields': ['aims_scope', 'current_demand'],
                    'manuscript_terms': ['AI agents', 'manufacturing operations'],
                }],
                'gaps': [{
                    'text': 'Confirm the venue data-availability policy in the final packet.',
                    'venue_fields': ['policies'],
                    'manuscript_terms': ['data availability'],
                }],
            })),
            ('mock-qwen', json.dumps({
                'fit_summary': 'The subject is related, but the declared manuscript type conflicts with the configured conference-paper type.',
                'reasons': [{
                    'text': 'The work concerns enterprise AI systems.',
                    'venue_fields': ['aims_scope'],
                    'manuscript_terms': ['AI agents'],
                }],
                'gaps': [{
                    'text': 'The configured venue accepts conference papers, not the declared research article type.',
                    'venue_fields': ['article_types'],
                    'manuscript_terms': ['research article'],
                }],
            })),
        ]

        response = self.client.post(
            f'/api/author/manuscripts/{manuscript.id}/matches/semantic/',
            data='{}',
            content_type='application/json',
            **self._auth(manuscript),
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIn('does not rank venues', response.json()['note'])

        after = {m.venue.slug: m for m in VenueMatch.objects.filter(manuscript=manuscript).select_related('venue')}
        self.assertEqual(after[first.slug].eligibility, before[first.slug])
        self.assertEqual(after[second.slug].eligibility, before[second.slug])
        self.assertIn('applied-ai', after[first.slug].fit_summary.lower())
        self.assertTrue(after[first.slug].evidence)

    @patch('review.services.author_agents.ollama_chat_json')
    def test_venue_assessment_creates_editorial_brief_and_evidence_packet(self, mock_chat):
        manuscript = self._create_manuscript()
        venue, _ = self._create_venues()
        manuscript.parsed_profile = {
            'semantic': {
                'summary': 'Applied AI agents in manufacturing operations.',
                'topics': ['AI agents', 'manufacturing'],
                'methods': ['controlled pilot comparison'],
                'contributions': ['operational evidence'],
                'limitations': ['single site'],
                'coverage': {'complete': True},
            }
        }
        manuscript.save(update_fields=['parsed_profile', 'updated_at'])

        create = self.client.post(
            f'/api/author/manuscripts/{manuscript.id}/submissions/',
            data=json.dumps({'venue_id': str(venue.id)}),
            content_type='application/json',
            **self._auth(manuscript),
        )
        self.assertEqual(create.status_code, 201, create.content)
        submission_id = create.json()['submission']['id']

        mock_chat.return_value = ('mock-qwen', json.dumps({
            'editor_summary': 'The manuscript is suitable for human editorial consideration with one policy check.',
            'outlet_fit': {
                'summary': 'The operational AI topic aligns with the venue scope.',
                'venue_fields': ['aims_scope', 'current_demand'],
                'manuscript_terms': ['AI agents', 'manufacturing'],
            },
            'policy_compliance': {
                'summary': 'Data availability should be checked against venue policy.',
                'venue_fields': ['policies'],
                'manuscript_terms': ['data availability'],
            },
            'contribution': {
                'summary': 'The manuscript presents operational implementation evidence.',
                'venue_fields': ['quality_threshold'],
                'manuscript_terms': ['operational evidence'],
            },
            'methods': {
                'summary': 'The controlled pilot is visible in the grounded profile.',
                'venue_fields': ['accepted_methods'],
                'manuscript_terms': ['controlled pilot comparison'],
            },
            'citation_integrity': {
                'summary': 'No external reference was verified in this test fixture.',
                'venue_fields': [],
                'manuscript_terms': [],
            },
            'unresolved_risks': [{
                'risk': 'Single-site evidence may limit transferability.',
                'venue_fields': ['quality_threshold'],
                'manuscript_terms': ['single site'],
            }],
            'reviewer_expertise': ['applied AI', 'operations management'],
        }))

        response = self.client.post(
            f'/api/author/venue-submissions/{submission_id}/assessment/run/',
            **self._auth(manuscript),
        )
        self.assertEqual(response.status_code, 201, response.content)
        submission = response.json()['submission']

        self.assertEqual(submission['status'], 'packet_ready')
        self.assertTrue(submission['editorial_brief']['human_decision_required'])
        self.assertEqual(submission['editorial_brief']['venue_config_version'], 1)
        self.assertGreater(submission['packet']['evidence_count'], 0)
        self.assertGreater(len(submission['evidence']), 0)
        self.assertTrue(all(item['claim'] for item in submission['evidence']))

    @patch('review.services.author_agents.ollama_chat_json')
    def test_semantic_readiness_failure_returns_503_without_corrupting_mechanical_result(self, mock_chat):
        manuscript = self._create_manuscript()
        mechanical = self._run_mechanical(manuscript)
        mock_chat.side_effect = RuntimeError('Ollama unavailable')

        response = self.client.post(
            f'/api/author/manuscripts/{manuscript.id}/readiness/semantic/',
            **self._auth(manuscript),
        )
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(response.json()['code'], 'semantic_readiness_failed')

        latest_completed = manuscript.readiness_assessments.filter(status='completed').first()
        self.assertEqual(str(latest_completed.id), mechanical['id'])
        self.assertEqual(latest_completed.engine_version, 'mechanical-v1')
