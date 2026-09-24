from django.utils import timezone
from datetime import timedelta
from .models import ReviewJob, Manuscript, VenueSubmission
from .services.author_agents import (
    run_semantic_readiness,
    run_semantic_matching,
    run_venue_assessment,
    AgentInputError,
    AgentExecutionError
)

def run_semantic_readiness_task(job_id, manuscript_id):
    job = ReviewJob.objects.get(id=job_id)
    job.status = 'processing'
    job.save(update_fields=['status'])
    
    try:
        manuscript = Manuscript.objects.get(id=manuscript_id)
        run_semantic_readiness(manuscript)
        
        job.status = 'completed'
        job.completed_at = timezone.now()
        job.progress = 100
        job.save(update_fields=['status', 'completed_at', 'progress'])
    except Exception as e:
        job.status = 'failed'
        job.error_message = str(e)
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'error_message', 'completed_at'])

def run_semantic_matching_task(job_id, manuscript_id, venue_ids=None):
    job = ReviewJob.objects.get(id=job_id)
    job.status = 'processing'
    job.save(update_fields=['status'])
    
    try:
        manuscript = Manuscript.objects.get(id=manuscript_id)
        run_semantic_matching(manuscript, venue_ids=venue_ids)
        
        job.status = 'completed'
        job.completed_at = timezone.now()
        job.progress = 100
        job.save(update_fields=['status', 'completed_at', 'progress'])
    except Exception as e:
        job.status = 'failed'
        job.error_message = str(e)
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'error_message', 'completed_at'])

def run_venue_assessment_task(job_id, submission_id):
    job = ReviewJob.objects.get(id=job_id)
    job.status = 'processing'
    job.save(update_fields=['status'])
    
    try:
        submission = VenueSubmission.objects.select_related(
            'manuscript', 'venue', 'venue__organization', 'venue_config'
        ).get(id=submission_id)
        run_venue_assessment(submission)
        
        job.status = 'completed'
        job.completed_at = timezone.now()
        job.progress = 100
        job.save(update_fields=['status', 'completed_at', 'progress'])
    except Exception as e:
        job.status = 'failed'
        job.error_message = str(e)
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'error_message', 'completed_at'])

def sweep_stuck_jobs_task():
    cutoff = timezone.now() - timedelta(minutes=30)
    stuck_jobs = ReviewJob.objects.filter(status='processing', updated_at__lt=cutoff)
    count = stuck_jobs.update(
        status='failed', 
        error_message='Job timed out after 30 minutes.',
        completed_at=timezone.now()
    )
    return f"Swept {count} stuck jobs."
