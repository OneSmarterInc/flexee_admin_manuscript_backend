import json
import tempfile
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from django.utils import timezone

from review.auth import COOKIE_NAME, issue_session
from review.models import (
    AuditEvent,
    EditorUser,
    Manuscript,
    Membership,
    Organization,
    Submission,
    Venue,
    VenueAgentConfig,
    VenueSubmission,
)
from review.tasks import sweep_retention_task


@pytest.fixture(autouse=True)
def audit_env(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'audit-test-secret')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')


def admin_client(user):
    client = Client()
    token, _ = issue_session(user.email)
    client.cookies[COOKIE_NAME] = token
    return client


def make_venue_submission(org_name='Audit Org', slug='audit-venue'):
    org = Organization.objects.create(name=org_name, organization_type='publisher')
    venue = Venue.objects.create(
        organization=org,
        name=f'{org_name} Journal',
        slug=slug,
        venue_type='journal',
    )
    config = VenueAgentConfig.objects.create(
        venue=venue,
        version=1,
        active=True,
        aims_scope='Audit test scope.',
    )
    payload = b'# Audit manuscript\n\nContent used for audit logging tests.\n'
    manuscript = Manuscript.objects.create(
        author_name='Audit Author',
        author_email='author@example.com',
        title='Audit manuscript',
        manuscript_type='research_article',
        abstract='Audit abstract',
        disclosure='AI used for copy editing.',
        manuscript_filename='audit.md',
        manuscript_file=SimpleUploadedFile('audit.md', payload, content_type='text/markdown'),
        manuscript_bytes=len(payload),
        manuscript_sha256='a' * 64,
    )
    submission = VenueSubmission.objects.create(
        manuscript=manuscript,
        venue=venue,
        venue_config=config,
        status='submitted',
        packet={'editorial_brief_ready': True},
        editorial_brief={'editor_summary': 'Audit brief'},
    )
    return org, venue, config, manuscript, submission


@pytest.mark.django_db
@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='flexee-audit-tests-'))
def test_editor_view_download_review_and_decision_are_audited():
    org, venue, _, manuscript, submission = make_venue_submission()
    editor = EditorUser.objects.create(email='audit-editor@example.com', password_hash='x')
    Membership.objects.create(user=editor, organization=org, role='editor')
    client = admin_client(editor)

    detail = client.get(f'/api/admin/venue-submissions/{submission.id}/')
    assert detail.status_code == 200

    download = client.get(f'/api/admin/venue-submissions/{submission.id}/download/')
    assert download.status_code == 200
    download.close()

    start = client.post(
        f'/api/admin/venue-submissions/{submission.id}/start-review/',
        data='{}',
        content_type='application/json',
    )
    assert start.status_code == 200

    with patch('review.editor_api.send_acceptance_email'):
        decision = client.post(
            f'/api/admin/venue-submissions/{submission.id}/decision/',
            data=json.dumps({'decision': 'accepted', 'note': 'Human audit test decision.'}),
            content_type='application/json',
        )
    assert decision.status_code == 200

    events = AuditEvent.objects.filter(venue_submission_id=submission.id)
    actions = set(events.values_list('action', flat=True))
    assert {
        'venue_submission.viewed',
        'venue_submission.manuscript_downloaded',
        'venue_submission.review_started',
        'venue_submission.decision_recorded',
    }.issubset(actions)

    event = events.get(action='venue_submission.decision_recorded')
    assert event.actor_email == editor.email
    assert event.actor_role == 'editor'
    assert event.organization_id == org.id
    assert event.venue_id == venue.id
    assert event.manuscript_id == manuscript.id
    assert event.detail['decision'] == 'accepted'
    assert len(event.remote_hash) == 64


@pytest.mark.django_db
def test_venue_configuration_changes_and_feedback_are_audited():
    org, venue, config, _, submission = make_venue_submission(
        org_name='Audit Config Org',
        slug='audit-config-venue',
    )
    owner = EditorUser.objects.create(email='audit-owner@example.com', password_hash='x')
    Membership.objects.create(user=owner, organization=org, role='owner')
    client = admin_client(owner)

    update = client.patch(
        f'/api/admin/venues/{venue.id}/',
        data=json.dumps({'description': 'Updated through audit test.'}),
        content_type='application/json',
    )
    assert update.status_code == 200

    create = client.post(
        f'/api/admin/venues/{venue.id}/config/',
        data=json.dumps({
            'aims_scope': 'Updated audit scope.',
            'retention_days': 365,
            'required_submission_items': [],
            'structured_desk_rejection_rules': [],
        }),
        content_type='application/json',
    )
    assert create.status_code == 201

    activate = client.post(
        f'/api/admin/venues/{venue.id}/configs/{config.id}/activate/',
        data='{}',
        content_type='application/json',
    )
    assert activate.status_code == 200

    feedback = client.post(
        f'/api/admin/venues/{venue.id}/feedback/',
        data=json.dumps({
            'venue_submission_id': str(submission.id),
            'assessment_field': 'methods',
            'agent_value': {'summary': 'Agent summary'},
            'editor_value': {'summary': 'Editor correction'},
            'reason': 'Audit test correction.',
        }),
        content_type='application/json',
    )
    assert feedback.status_code == 201

    actions = set(AuditEvent.objects.filter(organization_id=org.id).values_list('action', flat=True))
    assert 'venue.updated' in actions
    assert 'venue_config.created' in actions
    assert 'venue_config.activated' in actions
    assert 'venue_submission.feedback_recorded' in actions

    config_event = AuditEvent.objects.filter(
        organization_id=org.id,
        action='venue_config.created',
    ).latest('occurred_at')
    assert config_event.actor_role == 'owner'
    assert config_event.detail['retention_days'] == 365


@pytest.mark.django_db
def test_audit_api_is_tenant_scoped_and_platform_superuser_sees_all():
    org_a = Organization.objects.create(name='Audit Tenant A')
    org_b = Organization.objects.create(name='Audit Tenant B')
    venue_a = Venue.objects.create(
        organization=org_a,
        name='Audit A',
        slug='audit-tenant-a',
        venue_type='journal',
    )
    venue_b = Venue.objects.create(
        organization=org_b,
        name='Audit B',
        slug='audit-tenant-b',
        venue_type='journal',
    )
    AuditEvent.objects.create(
        action='venue.updated',
        resource_type='venue',
        resource_id=str(venue_a.id),
        organization_id=org_a.id,
        venue_id=venue_a.id,
        actor_email='a@example.com',
        actor_role='owner',
    )
    AuditEvent.objects.create(
        action='venue.updated',
        resource_type='venue',
        resource_id=str(venue_b.id),
        organization_id=org_b.id,
        venue_id=venue_b.id,
        actor_email='b@example.com',
        actor_role='owner',
    )
    AuditEvent.objects.create(
        action='smtp.updated',
        resource_type='smtp_settings',
        resource_id='1',
        actor_email='platform@example.com',
        actor_role='platform_superuser',
    )

    editor_a = EditorUser.objects.create(email='tenant-a-editor@example.com', password_hash='x')
    Membership.objects.create(user=editor_a, organization=org_a, role='viewer')
    tenant_response = admin_client(editor_a).get('/api/admin/audit-events/')
    assert tenant_response.status_code == 200
    tenant_events = tenant_response.json()['events']
    assert len(tenant_events) == 1
    assert tenant_events[0]['organization_id'] == str(org_a.id)

    platform = EditorUser.objects.create(
        email='audit-platform@example.com',
        password_hash='x',
        platform_superuser=True,
    )
    platform_response = admin_client(platform).get('/api/admin/audit-events/')
    assert platform_response.status_code == 200
    assert platform_response.json()['total'] == 3

    filtered = admin_client(platform).get('/api/admin/audit-events/?q=smtp')
    assert filtered.status_code == 200
    assert filtered.json()['total'] == 1
    assert filtered.json()['events'][0]['action'] == 'smtp.updated'


@pytest.mark.django_db
@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='flexee-legacy-audit-tests-'))
def test_platform_legacy_submission_view_download_and_decision_are_audited():
    platform = EditorUser.objects.create(
        email='legacy-audit-admin@example.com',
        password_hash='x',
        platform_superuser=True,
    )
    payload = b'# Legacy audit manuscript\n'
    submission = Submission.objects.create(
        kind='article',
        author_name='Legacy Author',
        author_email='legacy@example.com',
        title='Legacy audit manuscript',
        disclosure='No AI use.',
        manuscript_filename='legacy.md',
        manuscript_file=SimpleUploadedFile('legacy.md', payload, content_type='text/markdown'),
        manuscript_bytes=len(payload),
        manuscript_sha256='b' * 64,
        status='completed',
    )
    client = admin_client(platform)

    detail = client.get(f'/api/admin/submissions/{submission.id}/')
    assert detail.status_code == 200

    download = client.get(f'/api/admin/submissions/{submission.id}/download/')
    assert download.status_code == 200
    download.close()

    with patch('review.views.send_acceptance_email') as email:
        email.return_value = {
            'status': 'sent',
            'detail': {'sent': True},
            'notified_at': None,
        }
        decision = client.post(
            f'/api/admin/submissions/{submission.id}/accept/',
            data=json.dumps({'message': 'Accepted in audit test.'}),
            content_type='application/json',
        )
    assert decision.status_code == 200

    actions = set(
        AuditEvent.objects.filter(
            resource_type='legacy_submission',
            resource_id=str(submission.id),
        ).values_list('action', flat=True)
    )
    assert {
        'legacy_submission.viewed',
        'legacy_submission.manuscript_downloaded',
        'legacy_submission.decision_recorded',
    }.issubset(actions)

@pytest.mark.django_db
@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='flexee-audit-retention-tests-'))
def test_retention_purge_is_recorded_as_system_audit_event():
    org, venue, config, manuscript, submission = make_venue_submission(
        org_name='Audit Retention Org',
        slug='audit-retention-venue',
    )
    config.retention_days = 1
    config.save(update_fields=['retention_days'])
    submission.retention_expires_at = timezone.now() - timedelta(minutes=1)
    submission.save(update_fields=['retention_expires_at'])

    sweep_retention_task()

    event = AuditEvent.objects.get(
        action='venue_submission.retention_purged',
        venue_submission_id=submission.id,
    )
    assert event.actor_role == 'system'
    assert event.organization_id == org.id
    assert event.venue_id == venue.id
    assert event.manuscript_id == manuscript.id

