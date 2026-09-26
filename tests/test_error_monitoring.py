import io
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from review import monitoring
from review.models import ReviewJob
from review.tasks import run_semantic_readiness_task


def test_scrub_sentry_event_removes_sensitive_http_and_stack_payloads():
    original = {
        'request': {
            'url': 'https://example.test/api/author/verify/?token=secret-token',
            'query_string': 'token=secret-token',
            'data': {'manuscript': 'full manuscript text'},
            'cookies': {'session': 'secret'},
            'headers': {'Authorization': 'Bearer secret'},
            'env': {'REMOTE_ADDR': '10.0.0.1'},
            'method': 'POST',
        },
        'user': {'email': 'author@example.com', 'ip_address': '10.0.0.1'},
        'extra': {'prompt': 'full prompt text'},
        'breadcrumbs': {
            'values': [
                {
                    'timestamp': 123,
                    'category': 'http',
                    'level': 'info',
                    'message': 'author@example.com submitted a manuscript',
                    'data': {'body': 'manuscript text'},
                }
            ]
        },
        'exception': {
            'values': [
                {
                    'type': 'RuntimeError',
                    'value': 'Could not parse Secret Manuscript.docx',
                    'stacktrace': {
                        'frames': [
                            {
                                'filename': 'review/tasks.py',
                                'lineno': 100,
                                'vars': {'content': 'manuscript text'},
                            }
                        ]
                    },
                }
            ]
        },
        'contexts': {
            'runtime': {'name': 'CPython'},
            'auth': {'token': 'secret-token', 'password': 'secret-password'},
        },
        'tags': {'component': 'django_q', 'api_token': 'secret-token'},
    }

    cleaned = monitoring.scrub_sentry_event(original)

    assert cleaned['request'] == {
        'url': 'https://example.test/api/author/verify/',
        'method': 'POST',
    }
    assert 'user' not in cleaned
    assert 'extra' not in cleaned
    assert cleaned['breadcrumbs']['values'][0] == {
        'timestamp': 123,
        'category': 'http',
        'level': 'info',
    }
    value = cleaned['exception']['values'][0]
    assert value['type'] == 'RuntimeError'
    assert value['value'] == '[redacted]'
    assert 'vars' not in value['stacktrace']['frames'][0]
    assert cleaned['contexts']['auth']['token'] == '[redacted]'
    assert cleaned['contexts']['auth']['password'] == '[redacted]'
    assert cleaned['tags']['api_token'] == '[redacted]'

    # The scrubber works on a copy and never mutates the event handed to it.
    assert original['request']['data']['manuscript'] == 'full manuscript text'
    assert original['exception']['values'][0]['value'] == 'Could not parse Secret Manuscript.docx'


def test_initialize_sentry_is_disabled_without_dsn(monkeypatch):
    called = []
    monkeypatch.setattr('sentry_sdk.init', lambda **kwargs: called.append(kwargs))
    monitoring._ENABLED = True

    assert monitoring.initialize_sentry(
        dsn='',
        environment='production',
        release='abc123',
    ) is False
    assert monitoring.is_sentry_enabled() is False
    assert called == []


def test_initialize_sentry_enforces_privacy_defaults(monkeypatch):
    called = []
    monkeypatch.setattr('sentry_sdk.init', lambda **kwargs: called.append(kwargs))
    monitoring._ENABLED = False

    assert monitoring.initialize_sentry(
        dsn='https://public@example.invalid/1',
        environment='production',
        release='abc123',
        traces_sample_rate=2.0,
    ) is True

    options = called[0]
    assert options['send_default_pii'] is False
    assert options['max_request_body_size'] == 'never'
    assert options['include_local_variables'] is False
    assert options['profiles_sample_rate'] == 0.0
    assert options['traces_sample_rate'] == 1.0
    assert options['environment'] == 'production'
    assert options['release'] == 'abc123'
    assert options['before_send'] is monitoring.scrub_sentry_event
    assert options['before_send_transaction'] is monitoring.scrub_sentry_event

    monitoring._ENABLED = False


def test_background_task_decorator_captures_then_reraises(monkeypatch):
    captured = []

    monkeypatch.setattr(
        monitoring,
        'capture_exception',
        lambda exc, **kwargs: captured.append((exc, kwargs)),
    )

    @monitoring.monitor_background_task('test_worker')
    def explode():
        raise RuntimeError('boom')

    with pytest.raises(RuntimeError, match='boom'):
        explode()

    assert len(captured) == 1
    exc, metadata = captured[0]
    assert isinstance(exc, RuntimeError)
    assert metadata['component'] == 'django_q'
    assert metadata['operation'] == 'test_worker'
    assert metadata['tags']['task'] == 'explode'


@pytest.mark.django_db
def test_handled_semantic_job_failure_is_reported_and_job_state_preserved(monkeypatch):
    job = ReviewJob.objects.create(
        job_type='semantic_readiness',
        reference_id='00000000-0000-0000-0000-000000000001',
    )
    captured = []
    monkeypatch.setattr(
        'review.tasks.capture_exception',
        lambda exc, **kwargs: captured.append((exc, kwargs)),
    )

    with patch('review.tasks.Manuscript.objects.get', side_effect=RuntimeError('provider failed')):
        run_semantic_readiness_task(job.id, job.reference_id)

    job.refresh_from_db()
    assert job.status == 'failed'
    assert job.error_message == 'provider failed'
    assert len(captured) == 1
    assert captured[0][1]['operation'] == 'semantic_readiness'
    assert captured[0][1]['tags']['job_type'] == 'semantic_readiness'


@override_settings(
    SENTRY_ENABLED=False,
    SENTRY_ENVIRONMENT='test',
    SENTRY_RELEASE='test-release',
    SENTRY_TRACES_SAMPLE_RATE=0.0,
)
def test_verify_monitoring_command_does_not_expose_dsn():
    output = io.StringIO()
    call_command('verify_error_monitoring', stdout=output)
    value = output.getvalue()

    assert 'Sentry enabled: False' in value
    assert 'Environment: test' in value
    assert 'Release: test-release' in value
    assert 'Privacy mode:' in value
    assert 'DSN' not in value


@override_settings(SENTRY_ENABLED=False)
def test_verify_monitoring_refuses_test_event_when_disabled():
    with pytest.raises(CommandError, match='Sentry is disabled'):
        call_command('verify_error_monitoring', send_event=True)
