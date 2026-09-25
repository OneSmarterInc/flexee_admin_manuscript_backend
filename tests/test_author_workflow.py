import json
import tempfile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from review.models import Manuscript, Organization, Venue, VenueAgentConfig, VenueSubmission


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='flexee-author-tests-'))
class AuthorWorkflowApiTests(TestCase):
    def _create_manuscript(self):
        from review.models import Author
        from review.auth import issue_author_session
        author = Author.objects.create(email='author@example.com', name='Test Author', email_verified=True)
        token, _ = issue_author_session(author.id)
        
        manuscript = SimpleUploadedFile(
            'paper.md',
            b'# Test manuscript\n\n## Abstract\nA short abstract.\n\n## Methods\nMethods here.\n\n## References\nOne reference.\n\n## Data Availability\nAvailable on request.',
            content_type='text/markdown',
        )
        self.client.cookies['flxee_author_session'] = token
        response = self.client.post('/api/author/manuscripts/', {
            'title': 'A Test Manuscript',
            'author': 'Test Author',
            'email': 'author@example.com',
            'coauthors': 'Second Author',
            'manuscript_type': 'research_article',
            'abstract': 'A short abstract.',
            'keywords': 'AI, operations',
            'disclosure': 'AI was used for copy editing only.',
            'attestation': 'true',
            'manuscript': manuscript,
        })
        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()
        manuscript = payload['manuscript']
        manuscript['_access_token'] = payload['access_token']
        return manuscript

    def _auth(self, manuscript):
        return {'HTTP_X_MANUSCRIPT_TOKEN': manuscript['_access_token']}

    def _create_venues(self):
        org = Organization.objects.create(name='Test Publisher', organization_type='journal')
        venue_a = Venue.objects.create(
            organization=org,
            name='Applied AI Review',
            slug='applied-ai-review',
            venue_type='journal',
        )
        VenueAgentConfig.objects.create(
            venue=venue_a,
            version=1,
            aims_scope='Applied artificial intelligence research.',
            article_types=['Research article'],
            current_demand={'topics': ['agentic AI']},
        )
        venue_b = Venue.objects.create(
            organization=org,
            name='Systems Conference',
            slug='systems-conference',
            venue_type='conference',
        )
        VenueAgentConfig.objects.create(
            venue=venue_b,
            version=1,
            aims_scope='Enterprise systems research.',
            article_types=['Conference paper'],
        )
        return venue_a, venue_b

    def test_create_manuscript_and_run_mechanical_readiness(self):
        manuscript = self._create_manuscript()
        self.assertTrue(Manuscript.objects.filter(id=manuscript['id']).exists())

        response = self.client.post(
            f"/api/author/manuscripts/{manuscript['id']}/readiness/run/",
            **self._auth(manuscript),
        )
        self.assertEqual(response.status_code, 201, response.content)
        readiness = response.json()['readiness']

        self.assertEqual(readiness['status'], 'completed')
        self.assertEqual(readiness['engine_version'], 'mechanical-v1')
        self.assertTrue(readiness['summary']['ready_for_matching'])
        self.assertEqual(readiness['summary']['blocking_issues'], 0)

    def test_policy_gate_generates_persisted_matches_without_ranking(self):
        venue_a, venue_b = self._create_venues()
        manuscript = self._create_manuscript()
        self.client.post(
            f"/api/author/manuscripts/{manuscript['id']}/readiness/run/",
            **self._auth(manuscript),
        )

        response = self.client.post(
            f"/api/author/manuscripts/{manuscript['id']}/matches/run/",
            **self._auth(manuscript),
        )
        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()

        self.assertEqual(payload['matching_stage'], 'deterministic_policy_gate_v1')
        self.assertEqual(len(payload['matches']), 2)

        by_slug = {match['venue']['slug']: match for match in payload['matches']}
        self.assertEqual(by_slug[venue_a.slug]['eligibility'], 'eligible')
        self.assertEqual(by_slug[venue_b.slug]['eligibility'], 'needs_changes')
        self.assertIn('No semantic fit ranking', payload['note'])

        get_response = self.client.get(
            f"/api/author/manuscripts/{manuscript['id']}/matches/",
            **self._auth(manuscript),
        )
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(len(get_response.json()['matches']), 2)

    def test_author_chooses_venue_submits_and_transfers(self):
        venue_a, venue_b = self._create_venues()
        manuscript = self._create_manuscript()

        create_response = self.client.post(
            f"/api/author/manuscripts/{manuscript['id']}/submissions/",
            data=json.dumps({'venue_id': str(venue_a.id)}),
            content_type='application/json',
            **self._auth(manuscript),
        )
        self.assertEqual(create_response.status_code, 201, create_response.content)
        submission = create_response.json()['submission']
        self.assertEqual(submission['status'], 'draft')
        self.assertEqual(submission['venue']['slug'], venue_a.slug)

        draft_submit = self.client.post(
            f"/api/author/venue-submissions/{submission['id']}/submit/",
            data='{}',
            content_type='application/json',
            **self._auth(manuscript),
        )
        self.assertEqual(draft_submit.status_code, 409)

        source = VenueSubmission.objects.get(id=submission['id'])
        source.status = 'packet_ready'
        source.save(update_fields=['status'])

        submit_response = self.client.post(
            f"/api/author/venue-submissions/{submission['id']}/submit/",
            data='{}',
            content_type='application/json',
            **self._auth(manuscript),
        )
        self.assertEqual(submit_response.status_code, 200, submit_response.content)
        self.assertEqual(submit_response.json()['submission']['status'], 'submitted')

        early_transfer = self.client.post(
            f"/api/author/venue-submissions/{submission['id']}/transfer/",
            data=json.dumps({'venue_id': str(venue_b.id), 'reason': 'Author selected another venue'}),
            content_type='application/json',
            **self._auth(manuscript),
        )
        self.assertEqual(early_transfer.status_code, 409)

        source = VenueSubmission.objects.get(id=submission['id'])
        source.status = 'rejected'
        source.save(update_fields=['status'])

        transfer_response = self.client.post(
            f"/api/author/venue-submissions/{submission['id']}/transfer/",
            data=json.dumps({'venue_id': str(venue_b.id), 'reason': 'Author selected another venue'}),
            content_type='application/json',
            **self._auth(manuscript),
        )
        self.assertEqual(transfer_response.status_code, 201, transfer_response.content)
        transferred = transfer_response.json()['submission']
        self.assertEqual(transferred['venue']['slug'], venue_b.slug)
        self.assertEqual(transferred['status'], 'draft')

        source = VenueSubmission.objects.get(id=submission['id'])
        self.assertEqual(source.status, 'transferred')

    def test_manuscript_endpoints_require_the_issued_access_token(self):
        manuscript = self._create_manuscript()
        path = f"/api/author/manuscripts/{manuscript['id']}/"

        from django.test import Client as FreshClient
        fresh = FreshClient()
        missing = fresh.get(path)
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.json()['code'], 'author_token_required')

        invalid = fresh.get(path, HTTP_X_MANUSCRIPT_TOKEN='not-the-token')
        self.assertEqual(invalid.status_code, 403)
        self.assertEqual(invalid.json()['code'], 'author_token_invalid')

        allowed = self.client.get(path, **self._auth(manuscript))
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()['manuscript']['id'], manuscript['id'])

    def test_public_venue_list_exposes_active_config_only(self):
        venue_a, _ = self._create_venues()
        VenueAgentConfig.objects.filter(venue=venue_a, version=1).update(active=False)
        VenueAgentConfig.objects.create(
            venue=venue_a,
            version=2,
            aims_scope='Updated scope',
            article_types=['Research article'],
            active=True,
        )

        response = self.client.get('/api/author/venues/')
        self.assertEqual(response.status_code, 200)
        venues = response.json()['venues']
        item = next(v for v in venues if v['slug'] == venue_a.slug)
        self.assertEqual(item['config']['version'], 2)
        self.assertEqual(item['config']['aims_scope'], 'Updated scope')
