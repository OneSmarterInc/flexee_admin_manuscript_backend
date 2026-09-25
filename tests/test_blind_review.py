import json
import os
import pytest
from unittest.mock import patch
from django.core.files.uploadedfile import SimpleUploadedFile
from review.models import Organization, Venue, VenueAgentConfig, Manuscript, VenueSubmission
from review.services.author_agents import run_venue_assessment


# A minimal valid assessment response for the second ai_chat_json call
# (the editorial-brief prompt) so run_venue_assessment can complete.
_ASSESSMENT_RESPONSE = json.dumps({
    'editor_summary': 'Test assessment.',
    'outlet_fit': {'summary': 'Ok', 'venue_fields': [], 'manuscript_evidence_ids': []},
    'policy_compliance': {'summary': 'Ok', 'venue_fields': [], 'manuscript_evidence_ids': []},
    'contribution': {'summary': 'Ok', 'venue_fields': [], 'manuscript_evidence_ids': []},
    'methods': {'summary': 'Ok', 'venue_fields': [], 'manuscript_evidence_ids': []},
    'citation_integrity': {'summary': 'Ok', 'venue_fields': [], 'manuscript_evidence_ids': []},
    'unresolved_risks': [],
    'reviewer_expertise': ['test'],
})


@pytest.mark.django_db
@patch.dict(os.environ, {"AI_PROVIDER": "mock"})
class TestBlindReview:
    def setup_method(self):
        self.org = Organization.objects.create(name='Test Org')
        self.venue = Venue.objects.create(name='Test Venue', slug='test-venue', organization=self.org)
        
        self.config = VenueAgentConfig.objects.create(
            venue=self.venue,
            policies={'blind_review': True}
        )
        
    @patch('review.services.author_agents.ai_chat_json')
    def test_blind_review_blocked(self, mock_chat):
        # The anonymization prompt should be answered with "blocked".
        # The assessment prompt never runs because anonymization blocks first.
        mock_chat.return_value = ('mock', json.dumps({
            'status': 'blocked',
            'issues': [{'type': 'name', 'location': 'title page', 'evidence': 'John Doe'}],
        }))

        manuscript = Manuscript.objects.create(
            title='Test Paper',
            manuscript_filename='test.md',
            parsed_profile={'semantic': {'summary': 'Ok'}}
        )
        manuscript.manuscript_file.save('test.md', SimpleUploadedFile('test.md', b'John Doe was here.'))
        
        submission = VenueSubmission.objects.create(
            manuscript=manuscript,
            venue=self.venue,
            venue_config=self.config,
            status='draft'
        )
        
        submission = run_venue_assessment(submission)
        
        # Blocked
        assert submission.status == 'draft'
        assert submission.packet['editorial_brief_ready'] is False
        assert submission.packet['anonymization']['status'] == 'blocked'

    @patch('review.services.author_agents.ai_chat_json')
    def test_blind_review_clean(self, mock_chat):
        # First call: anonymization check → passed.
        # Second call: assessment prompt → valid editorial brief.
        mock_chat.side_effect = [
            ('mock', json.dumps({'status': 'passed', 'issues': []})),
            ('mock', _ASSESSMENT_RESPONSE),
        ]

        manuscript = Manuscript.objects.create(
            title='Test Paper',
            manuscript_filename='test.md',
            parsed_profile={'semantic': {'summary': 'Ok'}}
        )
        manuscript.manuscript_file.save('test.md', SimpleUploadedFile('test.md', b'A completely clean manuscript.'))
        
        submission = VenueSubmission.objects.create(
            manuscript=manuscript,
            venue=self.venue,
            venue_config=self.config,
            status='draft'
        )
        
        submission = run_venue_assessment(submission)
        
        # Succeeds
        assert submission.status == 'packet_ready'
        assert submission.packet['editorial_brief_ready'] is True
        assert submission.packet['anonymization']['status'] == 'passed'

    @patch('review.services.author_agents.ai_chat_json')
    def test_blind_review_disabled(self, mock_chat):
        # Blind review disabled → no anonymization call, only assessment.
        self.config.policies = {'blind_review': False}
        self.config.save()

        mock_chat.return_value = ('mock', _ASSESSMENT_RESPONSE)
        
        manuscript = Manuscript.objects.create(
            title='Test Paper',
            manuscript_filename='test.md',
            parsed_profile={'semantic': {'summary': 'Ok'}}
        )
        # Even if John Doe is here, it should not be blocked since disabled
        manuscript.manuscript_file.save('test.md', SimpleUploadedFile('test.md', b'John Doe was here.'))
        
        submission = VenueSubmission.objects.create(
            manuscript=manuscript,
            venue=self.venue,
            venue_config=self.config,
            status='draft'
        )
        
        submission = run_venue_assessment(submission)
        
        # Succeeds
        assert submission.status == 'packet_ready'
        assert submission.packet['editorial_brief_ready'] is True
        assert 'anonymization' not in submission.packet

    @patch('review.services.author_agents.ai_chat_json')
    def test_author_override(self, mock_chat):
        # Author override → anonymization skipped, only assessment runs.
        mock_chat.return_value = ('mock', _ASSESSMENT_RESPONSE)

        manuscript = Manuscript.objects.create(
            title='Test Paper',
            manuscript_filename='test.md',
            parsed_profile={'semantic': {'summary': 'Ok'}}
        )
        manuscript.manuscript_file.save('test.md', SimpleUploadedFile('test.md', b'John Doe was here.'))
        
        submission = VenueSubmission.objects.create(
            manuscript=manuscript,
            venue=self.venue,
            venue_config=self.config,
            status='draft',
            packet={'anonymization_override': True}
        )
        
        submission = run_venue_assessment(submission)
        
        # Succeeds
        assert submission.status == 'packet_ready'
        assert submission.packet['editorial_brief_ready'] is True
