import io
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.utils import timezone

from review.auth import issue_session
from review.models import AuditEvent, EditorUser, ReviewJob
from review.queue_health import emit_queue_health_alert, queue_health_snapshot


@pytest.mark.django_db
class TestQueueHealth:
    def setup_method(self):
        self.client = Client()
        self.admin = EditorUser.objects.create(
            email='admin@example.com',
            password_hash='dummy',
            platform_superuser=True,
        )
        token, _ = issue_session('admin@example.com')
        self.client.cookies['flxee_admin_session'] = token

    def test_queue_health_empty_is_healthy_and_backward_compatible(self):
        response = self.client.get('/api/admin/queue-health/')
        assert response.status_code == 200
        assert response['Cache-Control'] == 'no-store'

        data = response.json()
        assert data['status'] == 'healthy'
        assert data['queued_jobs'] == 0
        assert data['processing_jobs'] == 0
        assert data['oldest_job_age_seconds'] is None
        assert data['oldest_queued_job_age_seconds'] is None
        assert data['oldest_processing_job_age_seconds'] is None
        assert data['recent_failed_jobs'] == 0
        assert data['issues'] == []
        assert data['active_by_job_type'] == {}
        assert data['thresholds']['critical_oldest_queued_seconds'] == 600

    def test_queue_health_reports_oldest_queued_job_and_warning(self):
        job = ReviewJob.objects.create(
            job_type='public_review',
            reference_id='queued-old',
            status='queued',
        )
        past = timezone.now() - timedelta(minutes=6)
        ReviewJob.objects.filter(id=job.id).update(created_at=past)

        response = self.client.get('/api/admin/queue-health/')
        assert response.status_code == 200
        data = response.json()

        assert data['queued_jobs'] == 1
        assert data['oldest_job_age_seconds'] >= 360
        assert data['status'] == 'degraded'
        codes = {item['code'] for item in data['issues']}
        assert 'oldest_queued_warning' in codes
        assert data['active_by_job_type']['public_review']['queued'] == 1

    def test_queue_health_processing_timeout_is_critical(self):
        job = ReviewJob.objects.create(
            job_type='semantic_readiness',
            reference_id='processing-old',
            status='processing',
        )
        past = timezone.now() - timedelta(minutes=31)
        ReviewJob.objects.filter(id=job.id).update(updated_at=past)

        snapshot = queue_health_snapshot()

        assert snapshot['processing_jobs'] == 1
        assert snapshot['oldest_processing_job_age_seconds'] >= 1860
        assert snapshot['status'] == 'critical'
        codes = {item['code'] for item in snapshot['issues']}
        assert 'oldest_processing_critical' in codes

    def test_queue_health_recent_failures_respect_completed_time(self, monkeypatch):
        monkeypatch.setenv('QUEUE_HEALTH_WARNING_RECENT_FAILURES', '2')
        monkeypatch.setenv('QUEUE_HEALTH_CRITICAL_RECENT_FAILURES', '5')

        old_created = timezone.now() - timedelta(days=2)
        for index in range(2):
            job = ReviewJob.objects.create(
                job_type='semantic_matching',
                reference_id=f'failed-{index}',
                status='failed',
                completed_at=timezone.now(),
            )
            ReviewJob.objects.filter(id=job.id).update(created_at=old_created)

        snapshot = queue_health_snapshot()

        assert snapshot['recent_failed_jobs'] == 2
        assert snapshot['status'] == 'degraded'
        assert any(
            item['code'] == 'recent_failures_warning'
            for item in snapshot['issues']
        )

    def test_queue_health_alert_is_deduplicated_during_cooldown(self, monkeypatch):
        monkeypatch.setenv('QUEUE_HEALTH_WARNING_QUEUED_JOBS', '1')
        monkeypatch.setenv('QUEUE_HEALTH_CRITICAL_QUEUED_JOBS', '10')
        monkeypatch.setenv('QUEUE_HEALTH_WARNING_OLDEST_QUEUED_SECONDS', '99999')
        monkeypatch.setenv('QUEUE_HEALTH_CRITICAL_OLDEST_QUEUED_SECONDS', '99999')

        ReviewJob.objects.create(
            job_type='public_review',
            reference_id='alert-job',
            status='queued',
        )
        captured = []
        monkeypatch.setattr(
            'review.queue_health.capture_message',
            lambda message, **kwargs: captured.append((message, kwargs)),
        )

        snapshot = queue_health_snapshot()
        first = emit_queue_health_alert(snapshot)
        second = emit_queue_health_alert(snapshot)

        assert snapshot['status'] == 'degraded'
        assert first['emitted'] is True
        assert first['reason'] == 'alert'
        assert second['emitted'] is False
        assert second['reason'] == 'cooldown'
        assert len(captured) == 1
        assert captured[0][0] == 'Queue health degraded'
        assert AuditEvent.objects.filter(action='system.queue_health_alert').count() == 1

    def test_queue_health_recovery_emits_once_after_alert(self, monkeypatch):
        monkeypatch.setenv('QUEUE_HEALTH_WARNING_QUEUED_JOBS', '1')
        monkeypatch.setenv('QUEUE_HEALTH_CRITICAL_QUEUED_JOBS', '10')
        monkeypatch.setenv('QUEUE_HEALTH_WARNING_OLDEST_QUEUED_SECONDS', '99999')
        monkeypatch.setenv('QUEUE_HEALTH_CRITICAL_OLDEST_QUEUED_SECONDS', '99999')

        job = ReviewJob.objects.create(
            job_type='venue_assessment',
            reference_id='recover-job',
            status='queued',
        )
        captured = []
        monkeypatch.setattr(
            'review.queue_health.capture_message',
            lambda message, **kwargs: captured.append((message, kwargs)),
        )

        unhealthy = queue_health_snapshot()
        emit_queue_health_alert(unhealthy)

        job.status = 'completed'
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'completed_at', 'updated_at'])

        healthy = queue_health_snapshot()
        recovery = emit_queue_health_alert(healthy)
        duplicate_recovery = emit_queue_health_alert(healthy)

        assert healthy['status'] == 'healthy'
        assert recovery == {'emitted': True, 'reason': 'recovered'}
        assert duplicate_recovery == {'emitted': False, 'reason': 'healthy'}
        assert [item[0] for item in captured] == [
            'Queue health degraded',
            'Queue health recovered',
        ]
        assert AuditEvent.objects.filter(action='system.queue_health_recovered').count() == 1

    def test_queue_health_command_can_fail_for_external_monitoring(self, monkeypatch):
        monkeypatch.setenv('QUEUE_HEALTH_WARNING_QUEUED_JOBS', '1')
        monkeypatch.setenv('QUEUE_HEALTH_CRITICAL_QUEUED_JOBS', '10')
        ReviewJob.objects.create(
            job_type='public_review',
            reference_id='command-job',
            status='queued',
        )

        output = io.StringIO()
        with pytest.raises(CommandError, match='Queue health is degraded'):
            call_command(
                'check_queue_health',
                fail_on_unhealthy=True,
                stdout=output,
            )

        assert 'Queue status: degraded' in output.getvalue()

    def test_queue_health_unauthorized(self):
        self.client.cookies.clear()
        response = self.client.get('/api/admin/queue-health/')
        assert response.status_code == 401
