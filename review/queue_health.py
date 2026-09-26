from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from datetime import timedelta

from django.db.models import Count, Q
from django.utils import timezone

from .models import AuditEvent, ReviewJob
from .monitoring import capture_message


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value)


def queue_health_thresholds() -> dict:
    queue_timeout_seconds = _env_int('REVIEW_JOB_QUEUE_TIMEOUT_MINUTES', 10, minimum=1) * 60
    processing_timeout_seconds = _env_int('REVIEW_JOB_PROCESSING_TIMEOUT_MINUTES', 30, minimum=1) * 60

    warning_queued_jobs = _env_int('QUEUE_HEALTH_WARNING_QUEUED_JOBS', 10, minimum=1)
    critical_queued_jobs = max(
        warning_queued_jobs,
        _env_int('QUEUE_HEALTH_CRITICAL_QUEUED_JOBS', 25, minimum=1),
    )
    warning_oldest_queued_seconds = _env_int(
        'QUEUE_HEALTH_WARNING_OLDEST_QUEUED_SECONDS',
        max(60, queue_timeout_seconds // 2),
        minimum=1,
    )
    critical_oldest_queued_seconds = max(
        warning_oldest_queued_seconds,
        _env_int(
            'QUEUE_HEALTH_CRITICAL_OLDEST_QUEUED_SECONDS',
            queue_timeout_seconds,
            minimum=1,
        ),
    )
    warning_oldest_processing_seconds = _env_int(
        'QUEUE_HEALTH_WARNING_OLDEST_PROCESSING_SECONDS',
        max(300, int(processing_timeout_seconds * 0.75)),
        minimum=1,
    )
    critical_oldest_processing_seconds = max(
        warning_oldest_processing_seconds,
        _env_int(
            'QUEUE_HEALTH_CRITICAL_OLDEST_PROCESSING_SECONDS',
            processing_timeout_seconds,
            minimum=1,
        ),
    )
    warning_recent_failures = _env_int(
        'QUEUE_HEALTH_WARNING_RECENT_FAILURES',
        3,
        minimum=1,
    )
    critical_recent_failures = max(
        warning_recent_failures,
        _env_int('QUEUE_HEALTH_CRITICAL_RECENT_FAILURES', 10, minimum=1),
    )

    return {
        'warning_queued_jobs': warning_queued_jobs,
        'critical_queued_jobs': critical_queued_jobs,
        'warning_oldest_queued_seconds': warning_oldest_queued_seconds,
        'critical_oldest_queued_seconds': critical_oldest_queued_seconds,
        'warning_oldest_processing_seconds': warning_oldest_processing_seconds,
        'critical_oldest_processing_seconds': critical_oldest_processing_seconds,
        'failure_window_minutes': _env_int(
            'QUEUE_HEALTH_FAILURE_WINDOW_MINUTES',
            60,
            minimum=1,
        ),
        'warning_recent_failures': warning_recent_failures,
        'critical_recent_failures': critical_recent_failures,
        'alert_cooldown_minutes': _env_int(
            'QUEUE_HEALTH_ALERT_COOLDOWN_MINUTES',
            30,
            minimum=1,
        ),
    }


def _age_seconds(now, dt):
    if not dt:
        return None
    return int(max(0, (now - dt).total_seconds()))


def _issue(code: str, severity: str, message: str, value: int, threshold: int) -> dict:
    return {
        'code': code,
        'severity': severity,
        'message': message,
        'value': value,
        'threshold': threshold,
    }


def queue_health_snapshot(*, now=None) -> dict:
    now = now or timezone.now()
    thresholds = queue_health_thresholds()

    counts = ReviewJob.objects.aggregate(
        queued=Count('id', filter=Q(status='queued')),
        processing=Count('id', filter=Q(status='processing')),
        completed=Count('id', filter=Q(status='completed')),
        failed=Count('id', filter=Q(status='failed')),
    )

    oldest_queued_at = (
        ReviewJob.objects.filter(status='queued')
        .order_by('created_at')
        .values_list('created_at', flat=True)
        .first()
    )
    oldest_processing_at = (
        ReviewJob.objects.filter(status='processing')
        .order_by('updated_at')
        .values_list('updated_at', flat=True)
        .first()
    )

    oldest_queued_seconds = _age_seconds(now, oldest_queued_at)
    oldest_processing_seconds = _age_seconds(now, oldest_processing_at)

    failure_cutoff = now - timedelta(minutes=thresholds['failure_window_minutes'])
    recent = ReviewJob.objects.filter(completed_at__gte=failure_cutoff).aggregate(
        completed=Count('id', filter=Q(status='completed')),
        failed=Count('id', filter=Q(status='failed')),
    )

    by_type = defaultdict(lambda: {'queued': 0, 'processing': 0})
    for row in (
        ReviewJob.objects.filter(status__in=['queued', 'processing'])
        .values('job_type', 'status')
        .annotate(count=Count('id'))
    ):
        by_type[str(row['job_type'])][str(row['status'])] = row['count']

    issues = []

    queued_count = counts['queued'] or 0
    if queued_count >= thresholds['critical_queued_jobs']:
        issues.append(_issue(
            'queue_depth_critical',
            'critical',
            'Queued job count reached the critical threshold.',
            queued_count,
            thresholds['critical_queued_jobs'],
        ))
    elif queued_count >= thresholds['warning_queued_jobs']:
        issues.append(_issue(
            'queue_depth_warning',
            'warning',
            'Queued job count reached the warning threshold.',
            queued_count,
            thresholds['warning_queued_jobs'],
        ))

    if oldest_queued_seconds is not None:
        if oldest_queued_seconds >= thresholds['critical_oldest_queued_seconds']:
            issues.append(_issue(
                'oldest_queued_critical',
                'critical',
                'The oldest queued job has exceeded the critical age threshold.',
                oldest_queued_seconds,
                thresholds['critical_oldest_queued_seconds'],
            ))
        elif oldest_queued_seconds >= thresholds['warning_oldest_queued_seconds']:
            issues.append(_issue(
                'oldest_queued_warning',
                'warning',
                'The oldest queued job has exceeded the warning age threshold.',
                oldest_queued_seconds,
                thresholds['warning_oldest_queued_seconds'],
            ))

    if oldest_processing_seconds is not None:
        if oldest_processing_seconds >= thresholds['critical_oldest_processing_seconds']:
            issues.append(_issue(
                'oldest_processing_critical',
                'critical',
                'The oldest processing job has exceeded the critical age threshold.',
                oldest_processing_seconds,
                thresholds['critical_oldest_processing_seconds'],
            ))
        elif oldest_processing_seconds >= thresholds['warning_oldest_processing_seconds']:
            issues.append(_issue(
                'oldest_processing_warning',
                'warning',
                'The oldest processing job has exceeded the warning age threshold.',
                oldest_processing_seconds,
                thresholds['warning_oldest_processing_seconds'],
            ))

    recent_failed = recent['failed'] or 0
    if recent_failed >= thresholds['critical_recent_failures']:
        issues.append(_issue(
            'recent_failures_critical',
            'critical',
            'Recent background-job failures reached the critical threshold.',
            recent_failed,
            thresholds['critical_recent_failures'],
        ))
    elif recent_failed >= thresholds['warning_recent_failures']:
        issues.append(_issue(
            'recent_failures_warning',
            'warning',
            'Recent background-job failures reached the warning threshold.',
            recent_failed,
            thresholds['warning_recent_failures'],
        ))

    status = 'healthy'
    if any(item['severity'] == 'critical' for item in issues):
        status = 'critical'
    elif issues:
        status = 'degraded'

    return {
        'status': status,
        'generated_at': now.isoformat(),
        # Backward-compatible fields used by the existing admin endpoint/tests.
        'queued_jobs': queued_count,
        'oldest_job_age_seconds': oldest_queued_seconds,
        # Expanded production metrics.
        'processing_jobs': counts['processing'] or 0,
        'completed_jobs_total': counts['completed'] or 0,
        'failed_jobs_total': counts['failed'] or 0,
        'oldest_queued_job_age_seconds': oldest_queued_seconds,
        'oldest_processing_job_age_seconds': oldest_processing_seconds,
        'recent_window_minutes': thresholds['failure_window_minutes'],
        'recent_completed_jobs': recent['completed'] or 0,
        'recent_failed_jobs': recent_failed,
        'active_by_job_type': dict(sorted(by_type.items())),
        'thresholds': thresholds,
        'issues': issues,
    }


def _alert_fingerprint(snapshot: dict) -> str:
    material = [
        {
            'code': item.get('code'),
            'severity': item.get('severity'),
        }
        for item in snapshot.get('issues', [])
    ]
    raw = json.dumps(material, separators=(',', ':'), sort_keys=True).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:32]


def emit_queue_health_alert(snapshot: dict, *, force=False) -> dict:
    """
    Emit a deduplicated Sentry signal and audit record.

    The monitor deliberately stores only operational queue metrics, never
    manuscript, author, or submission payloads.
    """
    now = timezone.now()
    status = snapshot.get('status', 'healthy')
    if status == 'healthy':
        latest_alert = (
            AuditEvent.objects.filter(action='system.queue_health_alert')
            .order_by('-occurred_at')
            .first()
        )
        if latest_alert:
            recovery_already_recorded = AuditEvent.objects.filter(
                action='system.queue_health_recovered',
                occurred_at__gte=latest_alert.occurred_at,
            ).exists()
            if not recovery_already_recorded:
                capture_message(
                    'Queue health recovered',
                    component='django_q',
                    operation='queue_health_recovered',
                    level='info',
                    tags={'queue_health_status': 'healthy'},
                )
                AuditEvent.objects.create(
                    actor_role='system',
                    action='system.queue_health_recovered',
                    resource_type='queue_health',
                    resource_id='healthy',
                    detail={
                        'status': 'healthy',
                        'queued_jobs': snapshot.get('queued_jobs', 0),
                        'processing_jobs': snapshot.get('processing_jobs', 0),
                        'recent_failed_jobs': snapshot.get('recent_failed_jobs', 0),
                    },
                )
                return {'emitted': True, 'reason': 'recovered'}
        return {'emitted': False, 'reason': 'healthy'}

    fingerprint = _alert_fingerprint(snapshot)
    cooldown_minutes = snapshot.get('thresholds', {}).get('alert_cooldown_minutes', 30)
    cutoff = now - timedelta(minutes=max(1, int(cooldown_minutes)))

    duplicate = AuditEvent.objects.filter(
        action='system.queue_health_alert',
        resource_type='queue_health',
        resource_id=fingerprint,
        occurred_at__gte=cutoff,
    ).exists()

    if duplicate and not force:
        return {
            'emitted': False,
            'reason': 'cooldown',
            'fingerprint': fingerprint,
        }

    codes = [item.get('code') for item in snapshot.get('issues', []) if item.get('code')]
    level = 'error' if status == 'critical' else 'warning'
    capture_message(
        'Queue health degraded',
        component='django_q',
        operation='queue_health_alert',
        level=level,
        tags={
            'queue_health_status': status,
            'queue_health_issue_codes': ','.join(codes)[:200],
        },
    )
    AuditEvent.objects.create(
        actor_role='system',
        action='system.queue_health_alert',
        resource_type='queue_health',
        resource_id=fingerprint,
        detail={
            'status': status,
            'issue_codes': codes,
            'queued_jobs': snapshot.get('queued_jobs', 0),
            'processing_jobs': snapshot.get('processing_jobs', 0),
            'oldest_queued_job_age_seconds': snapshot.get('oldest_queued_job_age_seconds'),
            'oldest_processing_job_age_seconds': snapshot.get('oldest_processing_job_age_seconds'),
            'recent_failed_jobs': snapshot.get('recent_failed_jobs', 0),
        },
    )
    return {
        'emitted': True,
        'reason': 'alert',
        'fingerprint': fingerprint,
    }
