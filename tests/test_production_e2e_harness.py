import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from review.management.commands.production_e2e import Command
from review.models import AIUsageEvent, ReviewJob
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

@pytest.mark.django_db
def test_production_e2e_cost_report_uses_persisted_ai_usage():
    AIUsageEvent.objects.create(
        provider='anthropic',
        model='test-model',
        operation='venue_assessment',
        status='completed',
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        estimated_max_cost_usd='0.010000',
        actual_cost_usd='0.002500',
        priced=True,
        usage_estimated=False,
    )

    report = Command()._ai_cost_report(timezone.now() - timezone.timedelta(minutes=1))

    assert report['status'] == 'instrumented'
    assert report['completed_calls'] == 1
    assert report['total_tokens'] == 150
    assert report['cost_usd'] == '0.002500'
    assert report['unpriced_cloud_calls'] == 0
    assert report['by_operation'][0]['operation'] == 'venue_assessment'

