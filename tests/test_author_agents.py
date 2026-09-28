import json
import tempfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from review.models import EvidenceFinding, Manuscript, Organization, ReadinessAssessment, ReviewJob, Venue, VenueAgentConfig, VenueMatch, VenueSubmission


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='flexee-author-agent-tests-'))
class AuthorAgentApiTests(TestCase):
    def _create_manuscript(self):
        from review.models import Author
        from review.auth import issue_author_session
        author = Author.objects.create(email='author@example.com', name='Test Author', email_verified=True)
        token, _ = issue_author_session(author.id)
        
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
        self.client.cookies['flxee_author_session'] = token
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

    @patch('review.services.author_agents.ai_chat_json')
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
        self.assertEqual(response.status_code, 202, response.content)
        body = response.json()
        self.assertIn('job_id', body)

        # Execute the task directly instead of waiting for a worker
        job = ReviewJob.objects.get(id=body['job_id'])
        from review.tasks import run_semantic_readiness_task
        run_semantic_readiness_task(job.id, manuscript.id)

        # Assert persisted database results
        assessment = manuscript.readiness_assessments.filter(
            engine_version__startswith='author-agents-v1:semantic-readiness'
        ).first()
        self.assertIsNotNone(assessment)
        self.assertEqual(assessment.status, 'completed')
        self.assertIn('semantic-readiness:mock-qwen', assessment.engine_version)
        self.assertTrue(assessment.summary['semantic_advisory_only'])
        self.assertIn('semantic_profile', assessment.summary)
        
        # Test short manuscript coverage
        self.assertEqual(assessment.summary['chunks_analyzed'], 1)
        self.assertEqual(assessment.summary['chunks_total'], 1)
        self.assertEqual(assessment.summary['coverage_percent'], 100)
        self.assertEqual(assessment.summary['coverage']['chunks_analyzed'], 1)
        self.assertEqual(assessment.summary['coverage']['coverage_percent'], 100)

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

    def test_semantic_prompt_does_not_seed_literal_schema_placeholders(self):
        from review.services.author_agents import _chunk_prompt, _sanitize_chunk_result

        manuscript = self._create_manuscript()
        chunk = {
            'index': 1,
            'start_line': 1,
            'end_line': 4,
            'text': '[L1] # Agentic Operations\n[L2] ## Abstract\n[L3] This study evaluates AI agents.\n[L4] ## Methods',
        }
        prompt = _chunk_prompt(manuscript, chunk)

        self.assertIn('"topics": []', prompt)
        self.assertIn('Never copy schema labels', prompt)
        self.assertNotIn('"topics": ["topic"]', prompt)

        sanitized = _sanitize_chunk_result({
            'summary': 'A manuscript-specific summary.',
            'topics': ['topic', 'AI agents'],
            'methods': ['method or study design actually visible in this chunk', 'controlled pilot'],
            'contributions': ['claim about contribution visible in this chunk', 'operational evidence'],
            'limitations': ['limitation or unresolved issue visible in this chunk', 'single site'],
            'evidence_points': [],
            'findings': [],
        }, chunk, '# Agentic Operations\n## Abstract\nThis study evaluates AI agents.\n## Methods')

        self.assertEqual(sanitized['topics'], ['AI agents'])
        self.assertEqual(sanitized['methods'], ['controlled pilot'])
        self.assertEqual(sanitized['contributions'], ['operational evidence'])
        self.assertEqual(sanitized['limitations'], ['single site'])

    def test_aggregate_profile_derives_missing_fields_from_grounded_evidence(self):
        from review.services.author_agents import _aggregate_profile

        profile = _aggregate_profile([{
            'summary': '',
            'topics': [],
            'methods': [],
            'contributions': [],
            'limitations': [],
            'evidence_points': [
                {'kind': 'topic', 'text': 'Generative AI adoption in mid-sized enterprises.', 'source': {'type': 'manuscript'}},
                {'kind': 'method', 'text': 'Semi-structured interviews with 24 managers.', 'source': {'type': 'manuscript'}},
                {'kind': 'contribution', 'text': 'Governance and cross-functional teams improve adoption outcomes.', 'source': {'type': 'manuscript'}},
                {'kind': 'limitation', 'text': 'The study covers only 12 organizations.', 'source': {'type': 'manuscript'}},
            ],
            'findings': [],
        }], total_chunks=1, analyzed_chunks=1)

        self.assertEqual(profile['topics'], ['Generative AI adoption in mid-sized enterprises.'])
        self.assertEqual(profile['methods'], ['Semi-structured interviews with 24 managers.'])
        self.assertEqual(profile['contributions'], ['Governance and cross-functional teams improve adoption outcomes.'])
        self.assertEqual(profile['limitations'], ['The study covers only 12 organizations.'])
        self.assertIn('Generative AI adoption', profile['summary'])
        self.assertEqual(profile['coverage']['coverage_percent'], 100)

    def test_aggregate_profile_preserves_model_supplied_fields(self):
        from review.services.author_agents import _aggregate_profile

        profile = _aggregate_profile([{
            'summary': 'Model supplied summary.',
            'topics': ['AI governance'],
            'methods': ['Mixed methods'],
            'contributions': ['Model supplied contribution'],
            'limitations': ['Model supplied limitation'],
            'evidence_points': [
                {'kind': 'topic', 'text': 'Different grounded topic.', 'source': {'type': 'manuscript'}},
            ],
            'findings': [],
        }], total_chunks=1, analyzed_chunks=1)

        self.assertEqual(profile['summary'], 'Model supplied summary.')
        self.assertEqual(profile['topics'], ['AI governance'])
        self.assertEqual(profile['methods'], ['Mixed methods'])
        self.assertEqual(profile['contributions'], ['Model supplied contribution'])
        self.assertEqual(profile['limitations'], ['Model supplied limitation'])

    @patch('review.services.author_agents.ai_chat_json')
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
        self.assertEqual(response.status_code, 202, response.content)
        body = response.json()
        self.assertIn('job_id', body)

        # Execute the task directly
        job = ReviewJob.objects.get(id=body['job_id'])
        from review.tasks import run_semantic_matching_task
        run_semantic_matching_task(job.id, manuscript.id)

        # Assert persisted database results — eligibility must NOT change
        after = {m.venue.slug: m for m in VenueMatch.objects.filter(manuscript=manuscript).select_related('venue')}
        self.assertEqual(after[first.slug].eligibility, before[first.slug])
        self.assertEqual(after[second.slug].eligibility, before[second.slug])
        fit_summaries = [after[first.slug].fit_summary.lower(), after[second.slug].fit_summary.lower()]
        self.assertTrue(any('applied-ai' in text for text in fit_summaries))
        self.assertTrue(any('conference-paper' in text for text in fit_summaries))
        self.assertTrue(after[first.slug].evidence)

    @patch('review.services.author_agents.ai_chat_json')
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
        self.assertEqual(response.status_code, 202, response.content)
        body = response.json()
        self.assertIn('job_id', body)

        # Execute the task directly
        job = ReviewJob.objects.get(id=body['job_id'])
        from review.tasks import run_venue_assessment_task
        run_venue_assessment_task(job.id, submission_id)

        # Assert persisted database results
        submission = VenueSubmission.objects.get(id=submission_id)
        self.assertEqual(submission.status, 'packet_ready')
        self.assertTrue(submission.editorial_brief['human_decision_required'])
        self.assertEqual(submission.editorial_brief['venue_config_version'], 1)
        self.assertGreater(submission.packet['evidence_count'], 0)
        self.assertGreater(
            EvidenceFinding.objects.filter(venue_submission=submission).count(), 0
        )
        for finding in EvidenceFinding.objects.filter(venue_submission=submission):
            self.assertTrue(finding.claim)

    @patch('review.services.author_agents.ai_chat_json')
    def test_venue_assessment_fills_missing_required_sections_from_grounded_inputs(self, mock_chat):
        manuscript = self._create_manuscript()
        venue, _ = self._create_venues()
        manuscript.parsed_profile = {
            'semantic': {
                'summary': 'Applied AI agents in manufacturing operations.',
                'topics': ['AI agents'],
                'methods': ['controlled pilot comparison'],
                'contributions': ['operational implementation evidence'],
                'limitations': ['single site'],
                'evidence_points': [
                    {'id': 'M001', 'kind': 'topic', 'text': 'AI agents', 'source': {'type': 'manuscript'}},
                    {'id': 'M002', 'kind': 'method', 'text': 'controlled pilot comparison', 'source': {'type': 'manuscript'}},
                    {'id': 'M003', 'kind': 'contribution', 'text': 'operational implementation evidence', 'source': {'type': 'manuscript'}},
                ],
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
            'editor_summary': '',
            'outlet_fit': {
                'summary': 'The manuscript aligns with the configured applied AI scope.',
                'venue_fields': ['aims_scope'],
                'manuscript_evidence_ids': ['M001'],
            },
            'policy_compliance': {'summary': '', 'venue_fields': [], 'manuscript_evidence_ids': []},
            'contribution': {'summary': '', 'venue_fields': [], 'manuscript_evidence_ids': []},
            'methods': {'summary': '', 'venue_fields': [], 'manuscript_evidence_ids': []},
            'citation_integrity': {'summary': '', 'venue_fields': [], 'manuscript_evidence_ids': []},
            'unresolved_risks': [],
            'reviewer_expertise': [],
        }))

        response = self.client.post(
            f'/api/author/venue-submissions/{submission_id}/assessment/run/',
            **self._auth(manuscript),
        )
        self.assertEqual(response.status_code, 202, response.content)

        job = ReviewJob.objects.get(id=response.json()['job_id'])
        from review.tasks import run_venue_assessment_task
        run_venue_assessment_task(job.id, submission_id)

        submission = VenueSubmission.objects.get(id=submission_id)
        brief = submission.editorial_brief

        self.assertTrue(brief['outlet_fit']['summary'])
        self.assertTrue(brief['policy_compliance']['summary'])
        self.assertTrue(brief['contribution']['summary'])
        self.assertTrue(brief['methods']['summary'])
        self.assertTrue(brief['citation_integrity']['summary'])
        self.assertIn('operational implementation evidence', brief['contribution']['summary'])
        self.assertIn('controlled pilot comparison', brief['methods']['summary'])
        self.assertTrue(brief['editor_summary'])
        self.assertEqual(
            brief['reviewer_expertise'],
            ['Applied AI', 'Operations management', 'AI agents'],
        )

    @patch('review.services.author_agents.ai_chat_json')
    def test_venue_assessment_filters_placeholder_risks_and_object_reviewer_expertise(self, mock_chat):
        manuscript = self._create_manuscript()
        venue, _ = self._create_venues()
        manuscript.parsed_profile = {
            'semantic': {
                'summary': 'Applied AI agents in manufacturing operations.',
                'topics': ['AI agents'],
                'methods': ['controlled pilot comparison'],
                'contributions': ['operational evidence'],
                'limitations': ['single site'],
                'evidence_points': [{
                    'id': 'M001',
                    'kind': 'topic',
                    'text': 'Applied AI agents in manufacturing operations.',
                    'source': {'type': 'manuscript', 'locator': 'lines 1-3', 'excerpt': 'Applied AI agents.'},
                }],
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
            'editor_summary': 'Human editors should review the venue-specific evidence.',
            'outlet_fit': {'summary': 'The topic aligns with the configured applied AI scope.', 'venue_fields': ['aims_scope'], 'manuscript_evidence_ids': ['M001']},
            'policy_compliance': {'summary': 'The configured policy requires a human check.', 'venue_fields': ['policies'], 'manuscript_evidence_ids': ['M001']},
            'contribution': {'summary': 'The manuscript reports operational evidence.', 'venue_fields': ['quality_threshold'], 'manuscript_evidence_ids': ['M001']},
            'methods': {'summary': 'The controlled pilot is visible in the manuscript.', 'venue_fields': ['accepted_methods'], 'manuscript_evidence_ids': ['M001']},
            'citation_integrity': {'summary': 'Citation checks remain advisory.', 'venue_fields': [], 'manuscript_evidence_ids': []},
            'unresolved_risks': [{
                'risk': '<describe a specific risk or note that it is not configured>',
                'venue_fields': ['reporting_standards'],
                'manuscript_evidence_ids': ['M001'],
            }],
            'reviewer_expertise': [
                {'summary': 'Applied AI'},
                '<specific expertise area>',
                'Enterprise AI governance',
                'Mixed-methods research',
            ],
        }))

        response = self.client.post(
            f'/api/author/venue-submissions/{submission_id}/assessment/run/',
            **self._auth(manuscript),
        )
        self.assertEqual(response.status_code, 202, response.content)

        job = ReviewJob.objects.get(id=response.json()['job_id'])
        from review.tasks import run_venue_assessment_task
        run_venue_assessment_task(job.id, submission_id)

        submission = VenueSubmission.objects.get(id=submission_id)
        self.assertEqual(submission.editorial_brief['unresolved_risks'], [])
        self.assertEqual(
            submission.editorial_brief['reviewer_expertise'],
            ['Enterprise AI governance', 'Mixed-methods research'],
        )
        self.assertNotIn('<', json.dumps(submission.editorial_brief))

    @patch('review.services.author_agents.ai_chat_json')
    @patch('review.services.author_agents.ai_available')
    def test_semantic_readiness_failure_gracefully_degrades_to_deterministic_fallback(self, mock_available, mock_chat):
        manuscript = self._create_manuscript()
        mechanical = self._run_mechanical(manuscript)
        
        # Simulate AI provider failing
        mock_available.return_value = True
        mock_chat.side_effect = RuntimeError('Ollama unavailable')

        response = self.client.post(
            f'/api/author/manuscripts/{manuscript.id}/readiness/semantic/',
            **self._auth(manuscript),
        )
        self.assertEqual(response.status_code, 202, response.content)
        body = response.json()
        self.assertIn('job_id', body)

        # Execute the task directly
        job = ReviewJob.objects.get(id=body['job_id'])
        from review.tasks import run_semantic_readiness_task
        run_semantic_readiness_task(job.id, manuscript.id)

        # Assert persisted database results — should have fallen back to deterministic
        assessment = manuscript.readiness_assessments.filter(
            status='completed',
            engine_version__startswith='author-agents-v1:semantic-readiness'
        ).order_by('-created_at').first()
        self.assertIsNotNone(assessment)
        self.assertEqual(assessment.engine_version, 'author-agents-v1:semantic-readiness:deterministic-fallback')
        self.assertEqual(assessment.summary['model'], 'deterministic-fallback')
        self.assertTrue(assessment.summary['semantic_advisory_only'])
        
        self.assertNotEqual(str(assessment.id), mechanical['id'])

    @patch('review.services.author_agents._chunk_numbered_lines')
    @patch('review.services.author_agents.ai_chat_json')
    def test_semantic_readiness_long_manuscript_samples_coverage(self, mock_chat, mock_chunker):
        manuscript = self._create_manuscript()
        self._run_mechanical(manuscript)
        
        # Create 25 mock chunks
        mock_chunker.return_value = [{'index': i, 'start_line': i, 'end_line': i, 'text': f'Chunk {i}'} for i in range(1, 26)]
        
        mock_chat.return_value = ('mock-qwen', json.dumps({
            'summary': 'A sampled chunk.',
            'topics': ['AI agents'],
            'methods': [],
            'contributions': [],
            'limitations': [],
            'findings': [],
        }))

        response = self.client.post(
            f'/api/author/manuscripts/{manuscript.id}/readiness/semantic/',
            **self._auth(manuscript),
        )
        self.assertEqual(response.status_code, 202, response.content)
        body = response.json()
        self.assertIn('job_id', body)

        # Execute the task directly
        job = ReviewJob.objects.get(id=body['job_id'])
        from review.tasks import run_semantic_readiness_task
        run_semantic_readiness_task(job.id, manuscript.id)

        # Assert persisted database results
        assessment = manuscript.readiness_assessments.filter(
            status='completed',
            engine_version__startswith='author-agents-v1:semantic-readiness'
        ).order_by('-created_at').first()
        self.assertIsNotNone(assessment)

        # AUTHOR_AGENT_MAX_CHUNKS defaults to 8
        self.assertEqual(assessment.summary['chunks_analyzed'], 8)
        self.assertEqual(assessment.summary['chunks_total'], 25)
        self.assertEqual(assessment.summary['coverage_percent'], 32)
        
        # Verify same on the nested coverage dict
        self.assertEqual(assessment.summary['coverage']['chunks_analyzed'], 8)
        self.assertEqual(assessment.summary['coverage']['chunks_total'], 25)
        self.assertEqual(assessment.summary['coverage']['coverage_percent'], 32)
