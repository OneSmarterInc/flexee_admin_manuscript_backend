from datetime import timedelta
from unittest.mock import patch
import pytest
from django.utils import timezone
from django_q.models import Schedule
from review.models import ReviewJob
from review.tasks import run_semantic_readiness_task, _schedule_readiness_retry, sweep_stuck_jobs_task
from tests.test_readiness_resume import prepared
from review.services import author_agents as a

pytestmark = pytest.mark.django_db

def job_for(m):
    return ReviewJob.objects.create(job_type='semantic_readiness', reference_id=str(m.pk))

def deliver(job, m):
    ReviewJob.objects.filter(pk=job.pk).update(retry_at=timezone.now()-timedelta(seconds=1))
    run_semantic_readiness_task(job.pk, str(m.pk))
    job.refresh_from_db()

def test_partial_automatically_resumes_only_unread_chunks(prepared, monkeypatch):
    monkeypatch.setenv('AUTHOR_AGENT_MAX_CHUNKS','1')
    calls=[]
    monkeypatch.setattr(a, '_agent_json', lambda prompt, **kw: (calls.append(prompt) or ('test', {'summary':'Checked'})))
    job=job_for(prepared)
    deliver(job, prepared)
    assert job.status=='queued' and job.auto_retry_count==1 and job.progress==50
    assert Schedule.objects.filter(func='review.tasks.run_semantic_readiness_task').count()==1
    run_semantic_readiness_task(job.pk,str(prepared.pk))
    assert len(calls)==1  # early/duplicate delivery cannot bypass backoff
    assert not _schedule_readiness_retry(job.pk)
    deliver(job, prepared)
    assert job.status=='completed' and len(calls)==2 and 'CHUNK 2' in calls[1]

def test_unavailable_model_stops_after_three_retries(prepared, monkeypatch):
    monkeypatch.setattr(a,'ai_available',lambda:False)
    job=job_for(prepared)
    for _ in range(4): deliver(job,prepared)
    assert job.status=='failed' and job.auto_retry_count==3
    assert Schedule.objects.filter(func='review.tasks.run_semantic_readiness_task').count()==3
    assert 'Automatic retries finished' in job.error_message

def test_changed_version_or_purged_manuscript_not_retried(prepared):
    job=job_for(prepared)
    job.status='failed';job.readiness_version='different';job.save()
    assert not _schedule_readiness_retry(job.pk)
    job.readiness_version=str(prepared.current_version_id);job.save()
    prepared.content_purged_at=timezone.now();prepared.save()
    assert not _schedule_readiness_retry(job.pk)

def test_worker_timeout_schedules_recovery(prepared):
    job=job_for(prepared)
    job.status='processing';job.readiness_version=str(prepared.current_version_id);job.save()
    ReviewJob.objects.filter(pk=job.pk).update(updated_at=timezone.now()-timedelta(minutes=45))
    sweep_stuck_jobs_task()
    job.refresh_from_db()
    assert job.status=='queued' and job.auto_retry_count==1

def test_invalid_input_not_retried(prepared):
    job=job_for(prepared)
    with patch('review.tasks.run_semantic_readiness',side_effect=a.AgentInputError('Missing input')):
        deliver(job,prepared)
    assert job.status=='failed' and job.auto_retry_count==0
