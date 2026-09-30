import tempfile

import pytest
from django.test import override_settings
from django.utils import timezone

from review.models import AuditEvent, EditorUser, Membership, Organization
from tests.test_audit_logging import admin_client, make_venue_submission


@pytest.fixture(autouse=True)
def viewer_env(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'viewer-test-secret')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')


def editor_for(org, role='viewer', email='viewer@example.com'):
    user = EditorUser.objects.create(email=email, password_hash='hash')
    Membership.objects.create(user=user, organization=org, role=role)
    return user


@pytest.mark.django_db
@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='flexee-viewer-tests-'))
def test_view_returns_file_and_is_audited_as_view():
    org, venue, _, manuscript, submission = make_venue_submission(org_name='View Org', slug='view-venue')
    client = admin_client(editor_for(org))

    response = client.get(f'/api/admin/venue-submissions/{submission.id}/view/')
    assert response.status_code == 200
    body = b''.join(response.streaming_content)
    assert body.startswith(b'# Audit manuscript')
    # Served as an opaque, sandboxed attachment; the portal renders it itself.
    assert response['Content-Type'] == 'application/octet-stream'
    assert 'attachment' in response['Content-Disposition']
    assert response['Content-Security-Policy'] == 'sandbox'
    assert response['Cache-Control'].startswith('private, no-store')

    actions = list(AuditEvent.objects.filter(venue_submission_id=submission.id).values_list('action', flat=True))
    assert actions == ['venue_submission.manuscript_viewed']
    event = AuditEvent.objects.get(action='venue_submission.manuscript_viewed')
    assert event.manuscript_id == manuscript.id
    assert event.detail['filename'] == 'audit.md'


@pytest.mark.django_db
@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='flexee-viewer-tests-'))
def test_download_is_still_audited_as_download():
    org, _, _, _, submission = make_venue_submission(org_name='Download Org', slug='download-venue')
    client = admin_client(editor_for(org, role='editor', email='editor@example.com'))
    response = client.get(f'/api/admin/venue-submissions/{submission.id}/download/')
    assert response.status_code == 200
    assert AuditEvent.objects.filter(action='venue_submission.manuscript_downloaded').count() == 1
    assert not AuditEvent.objects.filter(action='venue_submission.manuscript_viewed').exists()


@pytest.mark.django_db
@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='flexee-viewer-tests-'))
def test_view_requires_access_to_the_venue():
    _, _, _, _, submission = make_venue_submission(org_name='Private Org', slug='private-venue')
    other_org = Organization.objects.create(name='Other Org', organization_type='publisher')
    client = admin_client(editor_for(other_org, email='outsider@example.com'))
    assert client.get(f'/api/admin/venue-submissions/{submission.id}/view/').status_code == 403
    assert not AuditEvent.objects.filter(action='venue_submission.manuscript_viewed').exists()


@pytest.mark.django_db
@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='flexee-viewer-tests-'))
def test_view_requires_admin_session():
    _, _, _, _, submission = make_venue_submission(org_name='Anon Org', slug='anon-venue')
    from django.test import Client
    assert Client().get(f'/api/admin/venue-submissions/{submission.id}/view/').status_code in (401, 403)


@pytest.mark.django_db
@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='flexee-viewer-tests-'))
def test_view_respects_retention():
    org, _, _, manuscript, submission = make_venue_submission(org_name='Retention Org', slug='retention-venue')
    client = admin_client(editor_for(org))
    submission.retention_purged_at = timezone.now()
    submission.save(update_fields=['retention_purged_at'])
    assert client.get(f'/api/admin/venue-submissions/{submission.id}/view/').status_code == 410
    assert not AuditEvent.objects.filter(action='venue_submission.manuscript_viewed').exists()


@pytest.mark.django_db
def test_view_only_allows_get():
    org, _, _, _, submission = make_venue_submission(org_name='Method Org', slug='method-venue')
    client = admin_client(editor_for(org))
    assert client.post(f'/api/admin/venue-submissions/{submission.id}/view/').status_code == 405
