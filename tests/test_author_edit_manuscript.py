import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from review.models import (
    Author, EvidenceFinding, Manuscript, Organization, ReadinessAssessment,
    ReviewJob, Venue, VenueAgentConfig, VenueMatch, VenueSubmission,
)

MD = b'# Title\n\n## Abstract\nShort.\n\n## References\nOne.\n'


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='flexee-edit-tests-'))
class AuthorEditManuscriptTests(TestCase):
    def setUp(self):
        from review.auth import issue_author_session
        self.author = Author.objects.create(email='author@example.com', name='Author', email_verified=True)
        token, _ = issue_author_session(self.author.id)
        self.client.cookies['flxee_author_session'] = token
        response = self.client.post('/api/author/manuscripts/', {
            'title': 'Original title', 'author': 'Author', 'email': 'author@example.com',
            'manuscript_type': 'research_article', 'abstract': 'Old abstract', 'keywords': 'a, b',
            'disclosure': 'None.', 'attestation': 'true',
            'manuscript': SimpleUploadedFile('paper.md', MD, content_type='text/markdown'),
        })
        self.assertEqual(response.status_code, 201, response.content)
        self.manuscript_id = response.json()['manuscript']['id']
        self.token = response.json()['access_token']

    def edit(self, **overrides):
        data = {
            'title': 'Edited title', 'author': 'Author Two', 'email': 'two@example.com',
            'coauthors': 'C. One', 'manuscript_type': 'case_study', 'abstract': 'New abstract',
            'keywords': 'x, y', 'disclosure': 'Copy editing only.', 'notes': 'n', 'attestation': 'true',
        }
        data.update(overrides)
        return self.client.post(f'/api/author/manuscripts/{self.manuscript_id}/update/', data)

    def venue_submission(self, status):
        org = Organization.objects.create(name=f'Org {status}')
        venue = Venue.objects.create(organization=org, name=f'Venue {status}', slug=f'venue-{status}', venue_type='journal')
        config = VenueAgentConfig.objects.create(venue=venue, version=1, aims_scope='Scope')
        return VenueSubmission.objects.create(
            manuscript_id=self.manuscript_id, venue=venue, venue_config=config, status=status,
            editorial_brief={'editor_summary': 'old'}, packet={'editorial_brief_ready': True},
        ), venue, config

    def test_edit_updates_fields_and_keeps_file(self):
        before = Manuscript.objects.get(id=self.manuscript_id)
        response = self.edit()
        self.assertEqual(response.status_code, 200, response.content)
        item = Manuscript.objects.get(id=self.manuscript_id)
        self.assertEqual(item.title, 'Edited title')
        self.assertEqual(item.author_name, 'Author Two')
        self.assertEqual(item.manuscript_type, 'case_study')
        self.assertEqual(item.keywords, ['x', 'y'])
        self.assertEqual(item.manuscript_sha256, before.manuscript_sha256)
        self.assertFalse(response.json()['file_replaced'])

    def test_edit_can_replace_file(self):
        old_name = Manuscript.objects.get(id=self.manuscript_id).manuscript_file.name
        new = SimpleUploadedFile('revised.md', b'# Revised\n\nMore text here.\n', content_type='text/markdown')
        with self.captureOnCommitCallbacks(execute=True):
            response = self.edit(manuscript=new)
        self.assertEqual(response.status_code, 200, response.content)
        item = Manuscript.objects.get(id=self.manuscript_id)
        self.assertEqual(item.manuscript_filename, 'revised.md')
        self.assertTrue(response.json()['file_replaced'])
        self.assertFalse(item.manuscript_file.storage.exists(old_name))

    def test_edit_validates_required_fields(self):
        self.assertEqual(self.edit(title='').status_code, 400)
        self.assertEqual(self.edit(disclosure='').status_code, 400)
        self.assertEqual(self.edit(manuscript_type='poem').status_code, 400)
        self.assertEqual(Manuscript.objects.get(id=self.manuscript_id).title, 'Original title')

    def test_edit_rejects_unsafe_file(self):
        bad = SimpleUploadedFile('payload.exe', b'MZ', content_type='application/octet-stream')
        self.assertEqual(self.edit(manuscript=bad).status_code, 400)

    def test_edit_resets_stale_analysis(self):
        submission, venue, config = self.venue_submission('packet_ready')
        VenueMatch.objects.create(manuscript_id=self.manuscript_id, venue=venue, venue_config=config, eligibility='eligible')
        EvidenceFinding.objects.create(manuscript_id=self.manuscript_id, venue_submission=submission,
                                       finding_type='x', claim='y', source_type='manuscript')
        Manuscript.objects.filter(id=self.manuscript_id).update(parsed_profile={'semantic': {'topics': ['t']}})

        response = self.edit()
        self.assertEqual(response.status_code, 200, response.content)
        submission.refresh_from_db()
        self.assertEqual(submission.status, 'draft')
        self.assertEqual(submission.editorial_brief, {})
        self.assertEqual(submission.packet, {})
        self.assertFalse(VenueMatch.objects.filter(manuscript_id=self.manuscript_id).exists())
        self.assertFalse(EvidenceFinding.objects.filter(venue_submission=submission).exists())
        self.assertEqual(Manuscript.objects.get(id=self.manuscript_id).parsed_profile, {})

    def test_edit_blocked_after_submission(self):
        for status in ['submitted', 'under_review', 'revision_requested', 'accepted', 'rejected', 'withdrawn', 'transferred']:
            with self.subTest(status=status):
                submission, _, _ = self.venue_submission(status)
                response = self.edit()
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()['code'], 'manuscript_locked')
                submission.delete()
        self.assertEqual(Manuscript.objects.get(id=self.manuscript_id).title, 'Original title')

    def test_edit_blocked_while_analysis_running(self):
        ReviewJob.objects.create(job_type='semantic_readiness', reference_id=self.manuscript_id, status='processing')
        response = self.edit()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 'manuscript_busy')

    def test_edit_requires_owner(self):
        self.client.cookies.clear()
        self.assertEqual(self.edit().status_code, 401)
        response = self.client.post(f'/api/author/manuscripts/{self.manuscript_id}/update/',
                                    {'title': 'x'}, HTTP_X_MANUSCRIPT_TOKEN='wrong')
        self.assertEqual(response.status_code, 403)

    def test_semantic_readiness_older_than_mechanical_is_hidden(self):
        from datetime import timedelta
        from django.utils import timezone
        semantic = ReadinessAssessment.objects.create(
            manuscript_id=self.manuscript_id, status='completed',
            engine_version='author-agents-v1:semantic-readiness:test')
        mechanical = ReadinessAssessment.objects.create(
            manuscript_id=self.manuscript_id, status='completed', engine_version='mechanical-v1')
        ReadinessAssessment.objects.filter(id=semantic.id).update(created_at=timezone.now() - timedelta(minutes=5))
        payload = self.client.get(f'/api/author/manuscripts/{self.manuscript_id}/readiness/').json()
        self.assertIsNone(payload['semantic_readiness'])
        self.assertEqual(payload['mechanical_readiness']['id'], str(mechanical.id))

        ReadinessAssessment.objects.filter(id=semantic.id).update(created_at=timezone.now() + timedelta(minutes=5))
        payload = self.client.get(f'/api/author/manuscripts/{self.manuscript_id}/readiness/').json()
        self.assertEqual(payload['semantic_readiness']['id'], str(semantic.id))

    def test_payload_reports_editable(self):
        url = f'/api/author/manuscripts/{self.manuscript_id}/'
        self.assertTrue(self.client.get(url).json()['manuscript']['editable'])
        self.venue_submission('submitted')
        self.assertFalse(self.client.get(url).json()['manuscript']['editable'])
