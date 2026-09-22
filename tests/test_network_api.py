import json
import os
import shutil
import tempfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings

from review.auth import COOKIE_NAME, issue_session
from review.models import (
    Manuscript,
    ReadinessAssessment,
    Venue,
    VenueAgentConfig,
    VenueAssessment,
    VenueMatch,
    VenueSubmission,
)


class NetworkApiTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.media_root = tempfile.mkdtemp(prefix='flexee-network-tests-')
        self.settings_override = override_settings(MEDIA_ROOT=self.media_root)
        self.settings_override.enable()

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def _create_manuscript(self):
        response = self.client.post(
            '/api/author/manuscripts/',
            data={
                'title': 'AI Agents in Manufacturing Operations',
                'manuscript_type': 'Research article',
                'author': 'Alex Morgan',
                'email': 'alex@example.com',
                'coauthors': 'Priya Shah; Jordan Lee',
                'abstract': 'A manufacturing AI study.',
                'keywords': 'AI agents, manufacturing, operations',
                'disclosure': 'AI was used for editing and code assistance.',
                'attestation': 'true',
                'manuscript': SimpleUploadedFile(
                    'paper.md',
                    b'# AI Agents in Manufacturing\n\nMethods and results.',
                    content_type='text/markdown',
                ),
            },
        )
        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()
        manuscript = Manuscript.objects.get(id=payload['manuscript']['id'])
        return manuscript, payload['access_key']

    def _venue(self, slug='applied-ai-review', name='Applied AI Review'):
        venue = Venue.objects.create(
            name=name,
            slug=slug,
            venue_type='journal',
            subscriber_name='Example Publisher',
            active=True,
        )
        VenueAgentConfig.objects.create(
            venue=venue,
            aims_scope='Applied artificial intelligence with operational evidence.',
            accepted_article_types=['Research article'],
            accepted_methods=['Case study'],
            quality_threshold='Internal threshold text',
            policies={'internal_policy': 'not public'},
            disclosures=['AI-use disclosure'],
            reporting_standards=['Data availability statement'],
            desk_rejection_rules=['Out of scope'],
            current_demand={'topics': ['AI agents']},
            reviewer_criteria={'expertise': ['AI agents']},
        )
        return venue

    def _admin_client(self):
        client = Client()
        token, _ = issue_session('admin')
        client.cookies[COOKIE_NAME] = token
        return client

    def test_create_manuscript_returns_one_time_access_key_and_protects_detail(self):
        manuscript, access_key = self._create_manuscript()

        self.assertNotEqual(manuscript.access_key_hash, access_key)
        self.assertTrue(manuscript.authorship_attested)
        self.assertEqual(manuscript.coauthors, ['Priya Shah', 'Jordan Lee'])
        self.assertEqual(manuscript.keywords, ['AI agents', 'manufacturing', 'operations'])

        denied = self.client.get(f'/api/author/manuscripts/{manuscript.id}/')
        self.assertEqual(denied.status_code, 404)

        allowed = self.client.get(
            f'/api/author/manuscripts/{manuscript.id}/',
            HTTP_X_MANUSCRIPT_KEY=access_key,
        )
        self.assertEqual(allowed.status_code, 200)
        payload = allowed.json()
        self.assertEqual(payload['manuscript']['title'], manuscript.title)
        self.assertEqual(payload['readiness']['status'], 'not_started')
        self.assertEqual(payload['venue_matches'], [])

    def test_public_venue_api_hides_internal_editor_configuration(self):
        self._venue()

        response = self.client.get('/api/venues/')
        self.assertEqual(response.status_code, 200)
        config = response.json()['items'][0]['agent_config']

        self.assertEqual(config['aims_scope'], 'Applied artificial intelligence with operational evidence.')
        self.assertIn('accepted_article_types', config)
        self.assertIn('current_demand', config)
        self.assertNotIn('quality_threshold', config)
        self.assertNotIn('policies', config)
        self.assertNotIn('desk_rejection_rules', config)
        self.assertNotIn('reviewer_criteria', config)

    @patch.dict(os.environ, {'ADMIN_SESSION_SECRET': 'network-test-secret', 'ADMIN_USERNAME': 'admin'}, clear=False)
    def test_admin_can_create_and_update_venue_agent_config(self):
        client = self._admin_client()
        response = client.post(
            '/api/admin/venues/',
            data=json.dumps({
                'name': 'Operations AI Journal',
                'venue_type': 'journal',
                'aims_scope': 'Operational AI research.',
                'accepted_article_types': ['Research article'],
                'quality_threshold': 'Internal quality threshold.',
                'policies': {'internal': 'rule'},
                'desk_rejection_rules': ['Outside scope'],
                'submission_capacity': 25,
                'current_demand': {'topics': ['AI agents']},
                'reviewer_criteria': {'expertise': ['operations']},
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 201, response.content)
        venue_payload = response.json()['venue']
        self.assertEqual(venue_payload['slug'], 'operations-ai-journal')
        self.assertEqual(venue_payload['agent_config']['quality_threshold'], 'Internal quality threshold.')
        self.assertEqual(venue_payload['agent_config']['submission_capacity'], 25)

        venue = Venue.objects.get(slug='operations-ai-journal')
        old_version = venue.agent_config.version
        update = client.post(
            f'/api/admin/venues/{venue.id}/',
            data=json.dumps({
                'current_demand': {'topics': ['agentic systems', 'manufacturing AI']},
            }),
            content_type='application/json',
        )
        self.assertEqual(update.status_code, 200, update.content)
        venue.agent_config.refresh_from_db()
        self.assertEqual(venue.agent_config.version, old_version + 1)

    @patch.dict(os.environ, {'ADMIN_SESSION_SECRET': 'network-test-secret', 'ADMIN_USERNAME': 'admin'}, clear=False)
    def test_author_can_choose_submit_and_transfer_without_reuploading(self):
        manuscript, access_key = self._create_manuscript()
        first = self._venue()
        second = self._venue(slug='enterprise-systems-journal', name='Enterprise Systems Journal')

        ReadinessAssessment.objects.create(
            manuscript=manuscript,
            status='completed',
            overall_state='ready',
            checks=[{'id': 'structure', 'passed': True}],
            engine_version='test-readiness',
        )
        VenueMatch.objects.create(
            manuscript=manuscript,
            venue=first,
            fit_level='strong',
            reasons=['Scope fit'],
            is_current=True,
        )
        VenueAssessment.objects.create(
            manuscript=manuscript,
            venue=first,
            status='completed',
            editorial_brief={'outlet_fit': 'Strong'},
            agent_config_version=first.agent_config.version,
            engine_version='test-venue-agent',
            is_current=True,
        )

        choose = self.client.post(
            f'/api/author/manuscripts/{manuscript.id}/choose-venue/',
            data=json.dumps({'venue_slug': first.slug}),
            content_type='application/json',
            HTTP_X_MANUSCRIPT_KEY=access_key,
        )
        self.assertEqual(choose.status_code, 201, choose.content)
        self.assertTrue(choose.json()['packet_ready'])
        first_submission_id = choose.json()['submission']['id']

        submit = self.client.post(
            f'/api/author/manuscripts/{manuscript.id}/submit/',
            data='{}',
            content_type='application/json',
            HTTP_X_MANUSCRIPT_KEY=access_key,
        )
        self.assertEqual(submit.status_code, 200, submit.content)
        self.assertEqual(submit.json()['submission']['status'], 'submitted')

        admin_client = self._admin_client()
        reject = admin_client.post(
            f'/api/admin/network-submissions/{first_submission_id}/decision/',
            data=json.dumps({'decision': 'rejected', 'note': 'Not for this issue.'}),
            content_type='application/json',
        )
        self.assertEqual(reject.status_code, 200, reject.content)
        self.assertEqual(reject.json()['submission']['status'], 'rejected')

        VenueMatch.objects.create(
            manuscript=manuscript,
            venue=second,
            fit_level='possible',
            reasons=['Enterprise systems fit'],
            gaps=['Add reproducibility statement'],
            is_current=True,
        )
        VenueAssessment.objects.create(
            manuscript=manuscript,
            venue=second,
            status='completed',
            editorial_brief={'outlet_fit': 'Possible'},
            agent_config_version=second.agent_config.version,
            engine_version='test-venue-agent',
            is_current=True,
        )

        transfer = self.client.post(
            f'/api/author/manuscripts/{manuscript.id}/transfer/',
            data=json.dumps({
                'source_submission_id': first_submission_id,
                'venue_slug': second.slug,
            }),
            content_type='application/json',
            HTTP_X_MANUSCRIPT_KEY=access_key,
        )
        self.assertEqual(transfer.status_code, 201, transfer.content)
        payload = transfer.json()['submission']
        self.assertEqual(payload['parent_submission_id'], first_submission_id)
        self.assertEqual(payload['venue']['slug'], second.slug)
        self.assertEqual(payload['status'], 'packet_ready')

        manuscript.refresh_from_db()
        self.assertEqual(manuscript.venue_submissions.count(), 2)
        self.assertEqual(manuscript.manuscript_filename, 'paper.md')
        self.assertEqual(VenueSubmission.objects.filter(manuscript=manuscript, is_current=True).count(), 1)

    def test_current_match_constraint_allows_history_but_one_current_record(self):
        manuscript, _ = self._create_manuscript()
        venue = self._venue()

        VenueMatch.objects.create(
            manuscript=manuscript,
            venue=venue,
            fit_level='possible',
            is_current=False,
        )
        current = VenueMatch.objects.create(
            manuscript=manuscript,
            venue=venue,
            fit_level='strong',
            is_current=True,
        )
        self.assertTrue(current.is_current)
        self.assertEqual(VenueMatch.objects.filter(manuscript=manuscript, venue=venue).count(), 2)
