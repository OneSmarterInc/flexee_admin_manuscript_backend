import pytest
import os
from django.test import Client, RequestFactory
from django.utils import timezone
from datetime import timedelta
import json
from review.models import Submission, ReviewJob, ReadinessAssessment, Manuscript, VenueMatch, Author, Venue, Organization, AuthorAuthEvent
from review.auth import remote_hash
from review.tasks import sweep_stuck_jobs_task

@pytest.mark.django_db
class TestStatusAndSweeper:
    def setup_method(self):
        self.client = Client()

    def test_status_polling_after_submission_limit(self, monkeypatch):
        monkeypatch.setenv('PUBLIC_SUBMIT_MAX_SUBMISSIONS', '1')
        monkeypatch.setenv('PUBLIC_STATUS_MAX_POLLS', '5')

        # Build the same HMAC hash that production code uses for 127.0.0.1
        request = RequestFactory().get('/', REMOTE_ADDR='127.0.0.1')
        seeded_hash = remote_hash(request)

        # Seed multiple submission events under the real hash
        for index in range(4):
            AuthorAuthEvent.objects.create(
                remote_hash=seeded_hash,
                success=True,
                detail={
                    'action': 'public_submit',
                    'email': f'test{index}@example.com'
                }
            )

        sub = Submission.objects.create(
            status='processing',
            title='Test Sub',
            author_name='Alice',
            manuscript_filename='x.docx',
            manuscript_bytes=100
        )

        # Status polling must still work even though submission limit is exceeded
        res = self.client.get(f'/api/submissions/{sub.id}/status/', REMOTE_ADDR='127.0.0.1')
        assert res.status_code == 200

    def test_status_polling_rate_limit(self, monkeypatch):
        monkeypatch.setenv('PUBLIC_STATUS_MAX_POLLS', '2')
        
        sub = Submission.objects.create(
            status='processing',
            title='Test Sub',
            author_name='Alice',
            manuscript_filename='x.docx',
            manuscript_bytes=100
        )
        
        res1 = self.client.get(f'/api/submissions/{sub.id}/status/', REMOTE_ADDR='127.0.0.1')
        assert res1.status_code == 200
        
        res2 = self.client.get(f'/api/submissions/{sub.id}/status/', REMOTE_ADDR='127.0.0.1')
        assert res2.status_code == 200
        
        res3 = self.client.get(f'/api/submissions/{sub.id}/status/', REMOTE_ADDR='127.0.0.1')
        assert res3.status_code == 429

    def test_hidden_errors_in_status(self):
        sub = Submission.objects.create(
            status='failed',
            title='Test Sub',
            author_name='Alice',
            manuscript_filename='x.docx',
            manuscript_bytes=100,
            error={'message': 'Secret DB error /var/run/secret'}
        )
        
        res = self.client.get(f'/api/submissions/{sub.id}/status/', REMOTE_ADDR='127.0.0.1')
        assert res.status_code == 200
        data = res.json()
        assert data['status'] == 'failed'
        assert data['error'] == 'Review processing failed. Please contact support.'
        
    def test_sweeper_clears_semantic_readiness(self):
        author = Author.objects.create(email='test@example.com', name='Author')
        ms = Manuscript.objects.create(title='Test MS', author_account=author)
        assessment = ReadinessAssessment.objects.create(
            manuscript=ms, status='pending', engine_version='test'
        )
        job = ReviewJob.objects.create(
            job_type='semantic_readiness', reference_id=str(ms.id), status='processing'
        )
        
        ReviewJob.objects.filter(id=job.id).update(updated_at=timezone.now() - timedelta(minutes=45))
        
        sweep_stuck_jobs_task()
        
        assessment.refresh_from_db()
        assert assessment.status == 'failed'
        assert assessment.error['type'] == 'TimeoutError'
        
        job.refresh_from_db()
        assert job.status == 'failed'

    def test_sweeper_preserves_semantic_match_data(self):
        """A stuck semantic_matches job must not destroy existing VenueMatch data."""
        author = Author.objects.create(email='test@example.com', name='Author')
        ms = Manuscript.objects.create(title='Test MS', author_account=author)
        org = Organization.objects.create(name='Test Org')
        venue = Venue.objects.create(name='Test Venue', slug='test-venue', organization=org)
        match = VenueMatch.objects.create(
            manuscript=ms,
            venue=venue,
            eligibility='eligible',
            fit_summary='Passes the currently configured deterministic routing checks.',
            reasons=['Word count is within range.'],
            gaps=[],
            evidence=[],
        )

        job = ReviewJob.objects.create(
            job_type='semantic_matches', reference_id=str(ms.id), status='processing'
        )

        ReviewJob.objects.filter(id=job.id).update(updated_at=timezone.now() - timedelta(minutes=45))

        sweep_stuck_jobs_task()

        # The job must be marked as failed
        job.refresh_from_db()
        assert job.status == 'failed'
        assert 'timed out' in job.error_message

        # All VenueMatch data must be preserved unchanged
        match.refresh_from_db()
        assert match.fit_summary == 'Passes the currently configured deterministic routing checks.'
        assert match.eligibility == 'eligible'
        assert match.reasons == ['Word count is within range.']
        assert match.gaps == []
        assert match.evidence == []

    def test_sweeper_ignores_unknown_job_types(self):
        job = ReviewJob.objects.create(
            job_type='unknown_type', reference_id='123', status='processing'
        )
        ReviewJob.objects.filter(id=job.id).update(updated_at=timezone.now() - timedelta(minutes=45))
        
        sweep_stuck_jobs_task()
        
        job.refresh_from_db()
        assert job.status == 'failed'
