import pytest
from django.utils import timezone
from datetime import timedelta
from review.models import ReviewJob, Submission
from review.tasks import sweep_stuck_jobs_task

@pytest.mark.django_db
class TestJobSweeper:
    def test_sweep_stuck_jobs(self):
        # Create a stuck public submission
        sub1 = Submission.objects.create(
            status='processing',
            title='Stuck',
            author_name='Alice',
            manuscript_filename='x.docx',
            manuscript_bytes=100
        )
        job1 = ReviewJob.objects.create(
            job_type='public_review',
            reference_id=str(sub1.id),
            status='processing'
        )
        # Manually backdate updated_at
        ReviewJob.objects.filter(id=job1.id).update(updated_at=timezone.now() - timedelta(minutes=45))

        # Create a recent public submission
        sub2 = Submission.objects.create(
            status='processing',
            title='Recent',
            author_name='Bob',
            manuscript_filename='y.docx',
            manuscript_bytes=100
        )
        job2 = ReviewJob.objects.create(
            job_type='public_review',
            reference_id=str(sub2.id),
            status='processing'
        )

        # Run sweeper
        result = sweep_stuck_jobs_task()
        assert "Swept 1 stuck jobs" in result

        # Check job1 is failed
        job1.refresh_from_db()
        assert job1.status == 'failed'
        assert job1.error_message == 'Job timed out after 30 minutes.'
        
        sub1.refresh_from_db()
        assert sub1.status == 'failed'
        assert sub1.error['type'] == 'TimeoutError'
        assert sub1.error['message'] == 'Job timed out after 30 minutes.'

        # Check job2 is processing
        job2.refresh_from_db()
        assert job2.status == 'processing'
        
        sub2.refresh_from_db()
        assert sub2.status == 'processing'
