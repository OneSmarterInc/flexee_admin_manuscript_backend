import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from review.management.commands.production_e2e import Command
from review.models import ReviewJob
from review.tasks import run_e2e_worker_probe_task


@pytest.mark.django_db
def test_e2e_worker_probe_task_completes_review_job():
    job = ReviewJob.objects.create(
        job_type='e2e_probe',
        reference_id='probe-reference',
        status='queued',
    )

    run_e2e_worker_probe_task(job.id)

    job.refresh_from_db()
    assert job.status == 'completed'
    assert job.progress == 100
    assert job.completed_at is not None


def test_production_e2e_rejects_mock_provider_before_live_run(monkeypatch, tmp_path):
    manuscript = tmp_path / 'sample.md'
    manuscript.write_text('# Sample\n\nTest manuscript.', encoding='utf-8')
    monkeypatch.setenv('AI_PROVIDER', 'mock')
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'test-secret')

    with pytest.raises(CommandError, match='ollama or anthropic'):
        call_command(
            'production_e2e',
            manuscript=str(manuscript),
            author_email='e2e@example.com',
            report=str(tmp_path / 'report.json'),
        )


def test_find_match_returns_requested_venue():
    command = Command()
    match = command._find_match(
        [
            {'venue': {'id': 'one'}, 'eligibility': 'eligible'},
            {'venue': {'id': 'two'}, 'eligibility': 'needs_changes'},
        ],
        'two',
    )

    assert match['venue']['id'] == 'two'


def test_find_match_fails_when_target_missing():
    command = Command()
    with pytest.raises(CommandError, match='was not present'):
        command._find_match([{'venue': {'id': 'one'}}], 'missing')
