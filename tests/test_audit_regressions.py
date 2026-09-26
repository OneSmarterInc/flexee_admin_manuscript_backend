import json
import os
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from review.auth import AUTHOR_COOKIE_NAME, COOKIE_NAME, issue_author_session, issue_session
from review.author_api import _queue_unique_job
from review.models import (
    Author,
    AuthorAuthEvent,
    EditorUser,
    Manuscript,
    Membership,
    Organization,
    ReviewJob,
    Submission,
    Venue,
    VenueAgentConfig,
    VenueSubmission,
)
from review.tasks import sweep_stuck_jobs_task


@pytest.fixture(autouse=True)
def audit_env(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'audit-regression-secret')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')


def admin_client(user):
    client = Client()
    token, _ = issue_session(user.email)
    client.cookies[COOKIE_NAME] = token
    return client


def author_client(author):
    client = Client()
    token, _ = issue_author_session(author.id)
    client.cookies[AUTHOR_COOKIE_NAME] = token
    return client


@pytest.mark.django_db
def test_venue_admin_endpoints_are_scoped_to_membership():
    org_a = Organization.objects.create(name='Org A')
    org_b = Organization.objects.create(name='Org B')
    owner_a = EditorUser.objects.create(email='owner-a@example.com', password_hash='x')
    Membership.objects.create(user=owner_a, organization=org_a, role='owner')
    venue_a = Venue.objects.create(
        organization=org_a, name='Venue A', slug='venue-a', venue_type='journal'
    )
    venue_b = Venue.objects.create(
        organization=org_b, name='Venue B', slug='venue-b', venue_type='journal'
    )
    VenueAgentConfig.objects.create(venue=venue_b, version=1, active=True)

    client = admin_client(owner_a)

    listing = client.get('/api/admin/venues/')
    assert listing.status_code == 200
    ids = {item['id'] for item in listing.json()['venues']}
    assert str(venue_a.id) in ids
    assert str(venue_b.id) not in ids

    assert client.get(f'/api/admin/venues/{venue_b.id}/config/').status_code == 403
    assert client.post(
        f'/api/admin/venues/{venue_b.id}/config/',
        data=json.dumps({'aims_scope': 'cross tenant'}),
        content_type='application/json',
    ).status_code == 403
    assert client.post(
        f'/api/admin/venues/{venue_b.id}/feedback/',
        data=json.dumps({'assessment_field': 'methods', 'reason': 'cross tenant'}),
        content_type='application/json',
    ).status_code == 403


@pytest.mark.django_db
def test_non_superuser_cannot_create_orphan_organization_but_owner_can_create_in_own_org():
    org = Organization.objects.create(name='Owned Org')
    owner = EditorUser.objects.create(email='owner@example.com', password_hash='x')
    Membership.objects.create(user=owner, organization=org, role='owner')
    client = admin_client(owner)

    before = Organization.objects.count()
    denied = client.post(
        '/api/admin/venues/',
        data=json.dumps({
            'name': 'Bad Venue',
            'venue_type': 'journal',
            'organization_name': 'Unauthorized New Org',
        }),
        content_type='application/json',
    )
    assert denied.status_code == 403
    assert Organization.objects.count() == before
    assert not Organization.objects.filter(name='Unauthorized New Org').exists()

    allowed = client.post(
        '/api/admin/venues/',
        data=json.dumps({
            'name': 'Owned Venue',
            'venue_type': 'journal',
            'organization_id': str(org.id),
        }),
        content_type='application/json',
    )
    assert allowed.status_code == 201
    assert allowed.json()['venue']['organization']['id'] == str(org.id)


@pytest.mark.django_db
@pytest.mark.parametrize('role', ['owner', 'editor', 'viewer'])
def test_org_users_cannot_use_platform_wide_admin_endpoints(role):
    org = Organization.objects.create(name=f'{role} org')
    user = EditorUser.objects.create(email=f'{role}@example.com', password_hash='x')
    Membership.objects.create(user=user, organization=org, role=role)
    client = admin_client(user)

    assert client.get('/api/admin/submissions/').status_code == 403
    assert client.get('/api/admin/smtp/').status_code == 403


@pytest.mark.django_db
def test_platform_superuser_keeps_legacy_admin_access_and_session_capabilities():
    user = EditorUser.objects.create(
        email='platform@example.com',
        password_hash='x',
        platform_superuser=True,
    )
    client = admin_client(user)

    assert client.get('/api/admin/submissions/').status_code == 200
    assert client.get('/api/admin/smtp/').status_code == 200
    session = client.get('/api/admin/session/')
    assert session.status_code == 200
    assert session.json()['platform_superuser'] is True
    assert session.json()['memberships'] == []


@pytest.mark.django_db
def test_cors_allows_frontend_patch_and_manuscript_token_preflight(monkeypatch):
    monkeypatch.setenv('FRONTEND_ORIGINS', 'https://frontend.example')
    client = Client()

    response = client.options(
        '/api/author/manuscripts/example/',
        HTTP_ORIGIN='https://frontend.example',
        HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS='x-manuscript-token,content-type',
    )
    assert response.status_code == 204
    assert 'PATCH' in response.headers['Access-Control-Allow-Methods']
    assert 'X-Manuscript-Token' in response.headers['Access-Control-Allow-Headers']


@pytest.mark.django_db
def test_admin_patch_uses_same_documented_origin_configuration(monkeypatch):
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '0')
    monkeypatch.setenv('FRONTEND_ORIGINS', 'https://frontend.example')
    monkeypatch.delenv('ADMIN_ALLOWED_ORIGINS', raising=False)

    org = Organization.objects.create(name='Org')
    owner = EditorUser.objects.create(email='owner-origin@example.com', password_hash='x')
    Membership.objects.create(user=owner, organization=org, role='owner')
    venue = Venue.objects.create(
        organization=org, name='Venue', slug='origin-venue', venue_type='journal'
    )
    client = admin_client(owner)

    bad = client.patch(
        f'/api/admin/venues/{venue.id}/',
        data=json.dumps({'description': 'bad'}),
        content_type='application/json',
        HTTP_ORIGIN='https://evil.example',
    )
    assert bad.status_code == 403

    good = client.patch(
        f'/api/admin/venues/{venue.id}/',
        data=json.dumps({'description': 'updated'}),
        content_type='application/json',
        HTTP_ORIGIN='https://frontend.example',
    )
    assert good.status_code == 200


@pytest.mark.django_db
def test_sweeper_does_not_write_invalid_fields_to_venue_submission():
    author = Author.objects.create(email='author-sweep@example.com', name='Author', password_hash='x')
    manuscript = Manuscript.objects.create(
        author_account=author,
        author_name='Author',
        author_email=author.email,
        title='Sweep manuscript',
        disclosure='none',
        manuscript_filename='sweep.md',
        manuscript_file=SimpleUploadedFile('sweep.md', b'test'),
    )
    org = Organization.objects.create(name='Sweep Org')
    venue = Venue.objects.create(
        organization=org, name='Sweep Venue', slug='sweep-venue', venue_type='journal'
    )
    submission = VenueSubmission.objects.create(
        manuscript=manuscript,
        venue=venue,
        status='draft',
    )
    job = ReviewJob.objects.create(
        job_type='venue_assessment',
        reference_id=str(submission.id),
        status='processing',
    )
    ReviewJob.objects.filter(id=job.id).update(
        updated_at=timezone.now() - timedelta(minutes=45)
    )

    result = sweep_stuck_jobs_task()
    assert 'Swept 1 stuck jobs' in result

    job.refresh_from_db()
    submission.refresh_from_db()
    assert job.status == 'failed'
    assert submission.status == 'draft'


@pytest.mark.django_db
def test_sweeper_fails_stale_queued_jobs_and_related_public_submission(monkeypatch):
    monkeypatch.setenv('REVIEW_JOB_QUEUE_TIMEOUT_MINUTES', '10')
    submission = Submission.objects.create(
        status='processing',
        kind='article',
        author_name='Queued Author',
        title='Queued',
        disclosure='none',
        manuscript_filename='queued.md',
        manuscript_bytes=10,
        manuscript_sha256='a' * 64,
    )
    job = ReviewJob.objects.create(
        job_type='public_review',
        reference_id=str(submission.id),
        status='queued',
    )
    ReviewJob.objects.filter(id=job.id).update(
        created_at=timezone.now() - timedelta(minutes=20)
    )

    sweep_stuck_jobs_task()

    job.refresh_from_db()
    submission.refresh_from_db()
    assert job.status == 'failed'
    assert 'queue timeout' in job.error_message.lower()
    assert submission.status == 'failed'
    assert submission.error['type'] == 'TimeoutError'


@pytest.mark.django_db(transaction=True)
def test_duplicate_active_author_jobs_reuse_existing_job():
    reference_id = '11111111-1111-1111-1111-111111111111'
    with patch('review.author_api.async_task') as mocked_async:
        first, first_created = _queue_unique_job(
            'semantic_readiness',
            reference_id,
            'review.tasks.run_semantic_readiness_task',
            reference_id,
        )
        second, second_created = _queue_unique_job(
            'semantic_readiness',
            reference_id,
            'review.tasks.run_semantic_readiness_task',
            reference_id,
        )

    assert first_created is True
    assert second_created is False
    assert first.id == second.id
    assert mocked_async.call_count == 1


@pytest.mark.django_db
def test_author_job_status_does_not_expose_internal_exception_text():
    author = Author.objects.create(email='private-error@example.com', name='Author', password_hash='x')
    manuscript = Manuscript.objects.create(
        author_account=author,
        author_name='Author',
        author_email=author.email,
        title='Private error',
        disclosure='none',
        manuscript_filename='private.md',
        manuscript_file=SimpleUploadedFile('private.md', b'test'),
    )
    job = ReviewJob.objects.create(
        job_type='semantic_readiness',
        reference_id=str(manuscript.id),
        status='failed',
        error_message='secret database path /var/run/private and provider key details',
    )
    client = author_client(author)

    response = client.get(f'/api/author/jobs/{job.id}/')
    assert response.status_code == 200
    assert response.json()['error'] == 'The background job could not be completed. Please try again.'
    assert '/var/run/private' not in str(response.content)


@pytest.mark.django_db
def test_author_can_resend_verification_email():
    author = Author.objects.create(
        email='verify-me@example.com',
        name='Verify Me',
        password_hash='x',
        email_verified=False,
    )
    client = author_client(author)

    with patch('review.author_api._send_email') as mocked_send:
        response = client.post('/api/author/resend-verification/', data='{}', content_type='application/json')

    assert response.status_code == 200
    assert response.json()['ok'] is True
    mocked_send.assert_called_once()
    assert AuthorAuthEvent.objects.filter(
        success=True,
        detail__action='resend_verification',
    ).exists()


@pytest.mark.django_db
def test_author_job_endpoint_does_not_expose_public_review_jobs():
    submission = Submission.objects.create(
        status='processing',
        kind='article',
        author_name='Public Author',
        title='Public job',
        disclosure='none',
        manuscript_filename='public.md',
        manuscript_bytes=10,
        manuscript_sha256='b' * 64,
    )
    job = ReviewJob.objects.create(
        job_type='public_review',
        reference_id=str(submission.id),
        status='queued',
    )

    response = Client().get(f'/api/author/jobs/{job.id}/')
    assert response.status_code == 404
