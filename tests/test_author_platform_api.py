import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings

from review.models import (
    Manuscript,
    Organization,
    Transfer,
    Venue,
    VenueAgentConfig,
    VenueSubmission,
)


TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix='flexee-author-api-test-')


@override_settings(MEDIA_ROOT=TEST_MEDIA_ROOT)
class AuthorPlatformApiTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEST_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.client = Client()
        self.organization = Organization.objects.create(
            name='Test Scholarly Group',
            slug='test-scholarly-group',
            organization_type='journal',
        )
        self.venue = Venue.objects.create(
            organization=self.organization,
            name='Applied AI Review',
            slug='applied-ai-review',
            venue_type='journal',
        )
        self.config = VenueAgentConfig.objects.create(
            venue=self.venue,
            version=1,
            is_active=True,
            aims_scope='Applied artificial intelligence research.',
            article_types=['Research article'],
            accepted_methods=['Empirical'],
            quality_threshold='Clear evidence and contribution.',
            reviewer_criteria=['Applied AI', 'Operations'],
            operating_rules={'requires_data_availability': True},
            current_demand={'topics': ['agentic systems']},
        )

    def _create_manuscript(self):
        upload = SimpleUploadedFile(
            'paper.md',
            b'# Test manuscript\n\nThis is a test manuscript.',
            content_type='text/markdown',
        )
        response = self.client.post('/api/author/manuscripts/', {
            'author': 'Alex Morgan',
            'email': 'alex@example.com',
            'coauthors': 'Priya Shah',
            'title': 'AI Agents in Manufacturing',
            'manuscript_type': 'Research article',
            'abstract': 'A study of AI agents in manufacturing.',
            'keywords': 'AI agents, manufacturing',
            'disclosure': 'AI was used for copy editing only.',
            'manuscript': upload,
        })
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        return body['manuscript'], body['access_token']

    def test_create_manuscript_returns_one_time_access_token(self):
        manuscript, token = self._create_manuscript()

        self.assertTrue(token)
        self.assertEqual(manuscript['title'], 'AI Agents in Manufacturing')
        self.assertEqual(manuscript['keywords'], ['AI agents', 'manufacturing'])

        stored = Manuscript.objects.get(id=manuscript['id'])
        self.assertNotEqual(stored.access_token_hash, token)
        self.assertEqual(len(stored.access_token_hash), 64)

    def test_manuscript_detail_requires_token(self):
        manuscript, token = self._create_manuscript()
        url = f"/api/author/manuscripts/{manuscript['id']}/"

        unauthorized = self.client.get(url)
        self.assertEqual(unauthorized.status_code, 401)

        authorized = self.client.get(url, HTTP_X_MANUSCRIPT_TOKEN=token)
        self.assertEqual(authorized.status_code, 200)
        self.assertEqual(authorized.json()['title'], manuscript['title'])

    def test_readiness_endpoint_returns_empty_until_agent_runs(self):
        manuscript, token = self._create_manuscript()
        response = self.client.get(
            f"/api/author/manuscripts/{manuscript['id']}/readiness/",
            HTTP_X_MANUSCRIPT_TOKEN=token,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['available'])
        self.assertIsNone(response.json()['assessment'])

    def test_public_venue_endpoint_exposes_active_config(self):
        response = self.client.get('/api/author/venues/')

        self.assertEqual(response.status_code, 200)
        item = response.json()['items'][0]
        self.assertEqual(item['name'], 'Applied AI Review')
        self.assertEqual(item['active_config']['version'], 1)
        self.assertEqual(item['active_config']['article_types'], ['Research article'])

    def test_author_can_choose_only_configured_active_venue(self):
        manuscript, token = self._create_manuscript()

        response = self.client.post(
            f"/api/author/manuscripts/{manuscript['id']}/submissions/",
            data=f'{{"venue_id":"{self.venue.id}"}}',
            content_type='application/json',
            HTTP_X_MANUSCRIPT_TOKEN=token,
        )

        self.assertEqual(response.status_code, 201, response.content)
        submission = VenueSubmission.objects.get(id=response.json()['submission']['id'])
        self.assertEqual(submission.venue_id, self.venue.id)
        self.assertEqual(submission.venue_config_id, self.config.id)
        self.assertEqual(submission.status, 'draft')

    def test_submit_packet_is_guarded_until_packet_ready(self):
        manuscript, token = self._create_manuscript()
        submission = VenueSubmission.objects.create(
            manuscript_id=manuscript['id'],
            venue=self.venue,
            venue_config=self.config,
            status='draft',
        )

        response = self.client.post(
            f'/api/author/venue-submissions/{submission.id}/submit/',
            HTTP_X_MANUSCRIPT_TOKEN=token,
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['status'], 'draft')

    def test_transfer_reuses_manuscript_after_rejection(self):
        manuscript, token = self._create_manuscript()
        source = VenueSubmission.objects.create(
            manuscript_id=manuscript['id'],
            venue=self.venue,
            venue_config=self.config,
            status='rejected',
        )

        second = Venue.objects.create(
            organization=self.organization,
            name='Enterprise Systems Journal',
            slug='enterprise-systems-journal',
            venue_type='journal',
        )
        second_config = VenueAgentConfig.objects.create(
            venue=second,
            version=1,
            is_active=True,
            aims_scope='Enterprise systems research.',
        )

        response = self.client.post(
            f'/api/author/venue-submissions/{source.id}/transfer/',
            data=f'{{"venue_id":"{second.id}"}}',
            content_type='application/json',
            HTTP_X_MANUSCRIPT_TOKEN=token,
        )

        self.assertEqual(response.status_code, 201, response.content)
        source.refresh_from_db()
        self.assertEqual(source.status, 'transferred')

        target = VenueSubmission.objects.get(id=response.json()['submission']['id'])
        self.assertEqual(target.manuscript_id, source.manuscript_id)
        self.assertEqual(target.venue_id, second.id)
        self.assertEqual(target.venue_config_id, second_config.id)
        self.assertTrue(Transfer.objects.filter(from_submission=source, to_submission=target).exists())
