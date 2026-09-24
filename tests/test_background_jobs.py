import pytest
from django.test import Client
from review.models import Manuscript, Organization, Venue, VenueSubmission, ReviewJob
from review.tasks import run_semantic_readiness_task, run_semantic_matching_task, run_venue_assessment_task, sweep_stuck_jobs_task
from django.utils import timezone
from datetime import timedelta

@pytest.mark.django_db
class TestBackgroundJobs:
    def setup_method(self):
        self.client = Client()
        self.org = Organization.objects.create(name='Test Org')
        self.manuscript = Manuscript.objects.create(
            title='Test Background Job',
            manuscript_filename='test.pdf',
            word_count=1000
        )
        self.venue = Venue.objects.create(name='Test Venue', slug='test-venue', organization=self.org)
        self.submission = VenueSubmission.objects.create(
            manuscript=self.manuscript,
            venue=self.venue,
            status='pending'
        )
    
    def test_job_status_endpoint(self):
        job = ReviewJob.objects.create(
            job_type='semantic_readiness',
            reference_id=str(self.manuscript.id),
            status='queued'
        )
        response = self.client.get(f'/api/author/jobs/{job.id}/')
        assert response.status_code == 200
        data = response.json()
        assert data['status'] == 'queued'
        assert data['id'] == str(job.id)

    def test_sweep_stuck_jobs(self):
        stuck_job = ReviewJob.objects.create(
            job_type='semantic_readiness',
            reference_id=str(self.manuscript.id),
            status='processing'
        )
        ReviewJob.objects.filter(id=stuck_job.id).update(
            updated_at=timezone.now() - timedelta(minutes=35)
        )
        
        sweep_stuck_jobs_task()
        
        stuck_job.refresh_from_db()
        assert stuck_job.status == 'failed'
        assert 'timed out' in stuck_job.error_message
