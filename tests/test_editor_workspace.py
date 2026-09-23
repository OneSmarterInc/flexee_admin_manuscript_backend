import json
import os
import tempfile
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings

from review.auth import COOKIE_NAME, issue_session
from review.models import (
    EditorFeedback,
    EvidenceFinding,
    Manuscript,
    Organization,
    Venue,
    VenueAgentConfig,
    VenueSubmission,
)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='flexee-editor-tests-'))
class EditorWorkspaceApiTests(TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                'ADMIN_SESSION_SECRET': 'editor-workspace-test-secret',
                'ADMIN_USERNAME': 'admin',
                'ADMIN_SESSION_HOURS': '8',
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

        token, _ = issue_session('admin')
        self.client.cookies[COOKIE_NAME] = token

        self.organization = Organization.objects.create(
            name='Editor Test Publisher',
            organization_type='publisher',
        )
        self.venue = Venue.objects.create(
            organization=self.organization,
            name='Editor Test Journal',
            slug='editor-test-journal',
            venue_type='journal',
            description='A test venue.',
        )
        self.config_v1 = VenueAgentConfig.objects.create(
            venue=self.venue,
            version=1,
            active=True,
            aims_scope='Applied AI research.',
            article_types=['Research article'],
            policies={'word_count': {'min': 1000, 'max': 8000}},
            current_demand={'topics': ['agentic systems']},
        )
        self.manuscript = Manuscript.objects.create(
            author_name='Editor Test Author',
            author_email='author@example.com',
            title='A manuscript for editor review',
            manuscript_type='research_article',
            abstract='A short abstract.',
            keywords=['AI', 'operations'],
            disclosure='AI was used for copy editing.',
            attestation=True,
            manuscript_filename='editor-paper.md',
            manuscript_file=SimpleUploadedFile(
                'editor-paper.md',
                b'# Paper\n\n## Abstract\nTest.\n\n## Methods\nA method.\n',
                content_type='text/markdown',
            ),
            manuscript_bytes=58,
            manuscript_sha256='a' * 64,
            parsed_profile={'word_count': 1200},
        )
        self.submission = VenueSubmission.objects.create(
            manuscript=self.manuscript,
            venue=self.venue,
            venue_config=self.config_v1,
            status='submitted',
            packet={'editorial_brief_ready': True, 'evidence_count': 1},
            editorial_brief={
                'editor_summary': 'Ready for human editorial review.',
                'outlet_fit': {'summary': 'The topic aligns with the configured scope.'},
                'policy_compliance': {'summary': 'No blocking policy issue recorded.'},
                'contribution': {'summary': 'Contribution is visible.'},
                'methods': {'summary': 'Methods are visible.'},
                'citation_integrity': {'summary': 'Citation verification is advisory.'},
                'reviewer_expertise': ['applied AI', 'operations'],
                'unresolved_risks': ['Single-site evidence.'],
                'human_decision_required': True,
            },
        )
        EvidenceFinding.objects.create(
            manuscript=self.manuscript,
            venue_submission=self.submission,
            finding_type='scope',
            claim='The topic aligns with the venue scope.',
            source_type='venue_policy',
            source_locator='venue config v1 · aims_scope',
            excerpt='Applied AI research.',
        )

    def test_venue_configuration_history_and_activation(self):
        response = self.client.get(f'/api/admin/venues/{self.venue.id}/')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['venue']['config']['version'], 1)

        create = self.client.post(
            f'/api/admin/venues/{self.venue.id}/config/',
            data=json.dumps({
                'aims_scope': 'Updated applied AI and operations scope.',
                'article_types': ['Research article', 'Case study'],
                'accepted_methods': ['Case study'],
                'quality_threshold': 'Clear contribution and methods.',
                'reviewer_criteria': ['Applied AI'],
                'policies': {'word_count': {'min': 1500, 'max': 7000}},
                'disclosures': ['AI-use disclosure required'],
                'reporting_standards': ['State limitations'],
                'desk_rejection_rules': ['Out of scope'],
                'deadlines': {'submission': 'rolling'},
                'submission_capacity': {},
                'current_demand': {'topics': ['AI agents']},
                'config_notes': 'Editor test update.',
            }),
            content_type='application/json',
        )
        self.assertEqual(create.status_code, 201, create.content)
        config_v2 = create.json()['config']
        self.assertEqual(config_v2['version'], 2)

        self.config_v1.refresh_from_db()
        self.assertFalse(self.config_v1.active)

        history = self.client.get(f'/api/admin/venues/{self.venue.id}/configs/')
        self.assertEqual(history.status_code, 200, history.content)
        self.assertEqual([item['version'] for item in history.json()['configs']], [2, 1])

        activate = self.client.post(
            f"/api/admin/venues/{self.venue.id}/configs/{self.config_v1.id}/activate/",
            data='{}',
            content_type='application/json',
        )
        self.assertEqual(activate.status_code, 200, activate.content)
        self.assertTrue(activate.json()['config']['active'])
        active = self.venue.agent_configs.filter(active=True).get()
        self.assertEqual(active.version, 1)

    def test_venue_metadata_can_be_updated_without_rewriting_config(self):
        response = self.client.patch(
            f'/api/admin/venues/{self.venue.id}/',
            data=json.dumps({
                'name': 'Renamed Journal',
                'description': 'Updated description.',
                'active': False,
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.venue.refresh_from_db()
        self.assertEqual(self.venue.name, 'Renamed Journal')
        self.assertFalse(self.venue.active)
        self.assertEqual(self.venue.agent_configs.count(), 1)

    def test_editor_queue_detail_feedback_and_human_decision(self):
        listing = self.client.get('/api/admin/venue-submissions/?scope=editor')
        self.assertEqual(listing.status_code, 200, listing.content)
        self.assertEqual(listing.json()['counts']['submitted'], 1)
        self.assertEqual(listing.json()['items'][0]['manuscript']['title'], self.manuscript.title)

        detail = self.client.get(f'/api/admin/venue-submissions/{self.submission.id}/')
        self.assertEqual(detail.status_code, 200, detail.content)
        self.assertEqual(detail.json()['submission']['venue_config']['version'], 1)
        self.assertEqual(len(detail.json()['submission']['evidence']), 1)

        feedback = self.client.post(
            f'/api/admin/venues/{self.venue.id}/feedback/',
            data=json.dumps({
                'venue_submission_id': str(self.submission.id),
                'assessment_field': 'methods',
                'agent_value': {'summary': 'Methods are visible.'},
                'editor_value': {'summary': 'Sampling detail needs clarification.'},
                'reason': 'Editor correction grounded in the manuscript review.',
            }),
            content_type='application/json',
        )
        self.assertEqual(feedback.status_code, 201, feedback.content)
        self.assertEqual(EditorFeedback.objects.filter(venue_submission=self.submission).count(), 1)

        start = self.client.post(
            f'/api/admin/venue-submissions/{self.submission.id}/start-review/',
            data='{}',
            content_type='application/json',
        )
        self.assertEqual(start.status_code, 200, start.content)
        self.assertEqual(start.json()['submission']['status'], 'under_review')

        decision = self.client.post(
            f'/api/admin/venue-submissions/{self.submission.id}/decision/',
            data=json.dumps({'decision': 'accepted', 'note': 'Accepted after human editorial review.'}),
            content_type='application/json',
        )
        self.assertEqual(decision.status_code, 200, decision.content)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, 'accepted')
        self.assertTrue(self.submission.decision['human_decision'])
        self.assertEqual(self.submission.decision['decided_by'], 'admin')

        refreshed = self.client.get(f'/api/admin/venue-submissions/{self.submission.id}/')
        self.assertEqual(len(refreshed.json()['submission']['feedback']), 1)

    def test_rejection_and_revision_require_editor_note(self):
        self.client.post(
            f'/api/admin/venue-submissions/{self.submission.id}/start-review/',
            data='{}',
            content_type='application/json',
        )
        missing = self.client.post(
            f'/api/admin/venue-submissions/{self.submission.id}/decision/',
            data=json.dumps({'decision': 'rejected', 'note': ''}),
            content_type='application/json',
        )
        self.assertEqual(missing.status_code, 400)

        revision = self.client.post(
            f'/api/admin/venue-submissions/{self.submission.id}/decision/',
            data=json.dumps({'decision': 'revision_requested', 'note': 'Please clarify the sampling method.'}),
            content_type='application/json',
        )
        self.assertEqual(revision.status_code, 200, revision.content)
        self.assertEqual(revision.json()['submission']['status'], 'revision_requested')

    def test_editor_can_download_original_manuscript(self):
        response = self.client.get(f'/api/admin/venue-submissions/{self.submission.id}/download/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('editor-paper.md', response.headers.get('Content-Disposition', ''))


class FlexeeVenueSeedCommandTests(TestCase):
    def test_seed_command_is_idempotent_and_refresh_versions_config(self):
        call_command('seed_flexee_venues', verbosity=0)

        field_notes = Venue.objects.get(slug='field-notes-journal')
        five_zero = Venue.objects.get(slug='five-zero-books')
        self.assertEqual(field_notes.name, 'Field Notes Journal')
        self.assertEqual(five_zero.name, 'Five Zero Books')
        self.assertEqual(field_notes.agent_configs.get().policies['word_count'], {'min': 1500, 'max': 3000})
        self.assertEqual(five_zero.agent_configs.get().policies['chapters_required'], 12)
        self.assertEqual(five_zero.agent_configs.get().policies['figures_required'], 40)

        call_command('seed_flexee_venues', verbosity=0)
        self.assertEqual(field_notes.agent_configs.count(), 1)
        self.assertEqual(five_zero.agent_configs.count(), 1)

        call_command('seed_flexee_venues', '--refresh', verbosity=0)
        self.assertEqual(field_notes.agent_configs.count(), 2)
        self.assertEqual(five_zero.agent_configs.count(), 2)
        self.assertEqual(field_notes.agent_configs.filter(active=True).get().version, 2)
        self.assertEqual(five_zero.agent_configs.filter(active=True).get().version, 2)
