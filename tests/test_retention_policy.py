import json
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client
from django.utils import timezone
from django_q.models import Schedule

from review.auth import AUTHOR_COOKIE_NAME, COOKIE_NAME, issue_author_session, issue_session
from review.models import (
    Author,
    EditorUser,
    EvidenceFinding,
    Manuscript,
    Membership,
    Organization,
    ReadinessAssessment,
    SubmissionRequirementFile,
    Venue,
    VenueAgentConfig,
    VenueMatch,
    VenueSubmission,
)
from review.tasks import sweep_retention_task


@pytest.fixture(autouse=True)
def retention_env(monkeypatch, settings, tmp_path):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'retention-test-secret')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')
    settings.MEDIA_ROOT = tmp_path


def author_client(author):
    client = Client()
    token, _ = issue_author_session(author.id)
    client.cookies[AUTHOR_COOKIE_NAME] = token
    return client


def admin_client(user):
    client = Client()
    token, _ = issue_session(user.email)
    client.cookies[COOKIE_NAME] = token
    return client


def make_manuscript(author, *, suffix=''):
    payload = b'# Retention test\n\nAbstract\n\nBody text used for retention testing.\n'
    return Manuscript.objects.create(
        author_account=author,
        author_name='Retention Author',
        author_email=author.email,
        title=f'Retention manuscript {suffix}'.strip(),
        manuscript_type='research_article',
        abstract='Sensitive abstract text.',
        disclosure='AI was used for copy editing.',
        notes='Sensitive author notes.',
        manuscript_filename=f'retention{suffix}.md',
        manuscript_file=SimpleUploadedFile(
            f'retention{suffix}.md',
            payload,
            content_type='text/markdown',
        ),
        manuscript_bytes=len(payload),
        manuscript_sha256=('a' if not suffix else 'b') * 64,
        parsed_profile={'semantic': {'summary': 'Derived semantic text'}},
    )


@pytest.mark.django_db
def test_owner_can_configure_retention_days_and_invalid_values_are_rejected():
    org = Organization.objects.create(name='Retention Config Org')
    venue = Venue.objects.create(
        organization=org,
        name='Retention Journal',
        slug='retention-journal',
        venue_type='journal',
    )
    owner = EditorUser.objects.create(email='retention-owner@example.com', password_hash='x')
    Membership.objects.create(user=owner, organization=org, role='owner')
    client = admin_client(owner)

    invalid = client.post(
        f'/api/admin/venues/{venue.id}/config/',
        data=json.dumps({'aims_scope': 'Scope', 'retention_days': 0}),
        content_type='application/json',
    )
    assert invalid.status_code == 400
    assert VenueAgentConfig.objects.filter(venue=venue).count() == 0

    valid = client.post(
        f'/api/admin/venues/{venue.id}/config/',
        data=json.dumps({'aims_scope': 'Scope', 'retention_days': 90}),
        content_type='application/json',
    )
    assert valid.status_code == 201
    assert valid.json()['config']['retention_days'] == 90
    assert VenueAgentConfig.objects.get(venue=venue).retention_days == 90


@pytest.mark.django_db
def test_formal_submission_snapshots_configured_retention_expiry():
    author = Author.objects.create(email='retention-submit@example.com', name='Retention Author', password_hash='x')
    manuscript = make_manuscript(author)
    org = Organization.objects.create(name='Retention Submit Org')
    venue = Venue.objects.create(
        organization=org,
        name='Retention Submit Venue',
        slug='retention-submit-venue',
        venue_type='journal',
    )
    config = VenueAgentConfig.objects.create(
        venue=venue,
        version=1,
        active=True,
        aims_scope='Scope',
        retention_days=30,
    )
    submission = VenueSubmission.objects.create(
        manuscript=manuscript,
        venue=venue,
        venue_config=config,
        status='packet_ready',
    )

    response = author_client(author).post(
        f'/api/author/venue-submissions/{submission.id}/submit/',
        data='{}',
        content_type='application/json',
    )

    assert response.status_code == 200
    submission.refresh_from_db()
    assert submission.submitted_at is not None
    assert submission.retention_expires_at is not None
    expected = submission.submitted_at + timedelta(days=30)
    assert abs((submission.retention_expires_at - expected).total_seconds()) < 1
    assert response.json()['submission']['retention_expires_at'] is not None


@pytest.mark.django_db
def test_expired_venue_copy_is_purged_without_deleting_another_retained_copy():
    author = Author.objects.create(email='shared-retention@example.com', name='Shared Author', password_hash='x')
    manuscript = make_manuscript(author, suffix='-shared')
    org = Organization.objects.create(name='Shared Retention Org')
    expired_venue = Venue.objects.create(
        organization=org,
        name='Expired Venue',
        slug='expired-retention-venue',
        venue_type='journal',
    )
    retained_venue = Venue.objects.create(
        organization=org,
        name='Retained Venue',
        slug='retained-retention-venue',
        venue_type='journal',
    )
    expired_config = VenueAgentConfig.objects.create(
        venue=expired_venue,
        version=1,
        active=True,
        retention_days=1,
    )
    retained_config = VenueAgentConfig.objects.create(
        venue=retained_venue,
        version=1,
        active=True,
        retention_days=None,
    )
    expired = VenueSubmission.objects.create(
        manuscript=manuscript,
        venue=expired_venue,
        venue_config=expired_config,
        status='accepted',
        submitted_at=timezone.now() - timedelta(days=5),
        retention_expires_at=timezone.now() - timedelta(days=4),
        packet={'author_name': 'Retention Author', 'requirement_responses': {'orcid': '0000'}},
        editorial_brief={'editor_summary': 'Sensitive generated brief.'},
        decision={'decision': 'accepted', 'human_decision': True},
    )
    VenueSubmission.objects.create(
        manuscript=manuscript,
        venue=retained_venue,
        venue_config=retained_config,
        status='submitted',
        submitted_at=timezone.now() - timedelta(days=5),
        retention_expires_at=None,
    )
    EvidenceFinding.objects.create(
        manuscript=manuscript,
        venue_submission=expired,
        finding_type='scope',
        claim='Sensitive finding',
        source_type='manuscript',
        source_locator='line 1',
        excerpt='Sensitive manuscript excerpt',
    )
    requirement = SubmissionRequirementFile.objects.create(
        venue_submission=expired,
        requirement_key='cover_letter',
        original_filename='cover.pdf',
        file=SimpleUploadedFile('cover.pdf', b'%PDF retention', content_type='application/pdf'),
        file_bytes=14,
        file_sha256='c' * 64,
    )
    requirement_name = requirement.file.name

    result = sweep_retention_task()

    assert 'Purged 1 expired venue submission' in result
    expired.refresh_from_db()
    manuscript.refresh_from_db()
    assert expired.retention_purged_at is not None
    assert expired.editorial_brief == {}
    assert expired.packet['retention_purged'] is True
    assert expired.decision['decision'] == 'accepted'
    assert expired.evidence_findings.count() == 0
    assert expired.requirement_files.count() == 0
    assert not requirement.file.storage.exists(requirement_name)
    assert manuscript.content_purged_at is None
    assert manuscript.manuscript_file
    assert manuscript.parsed_profile

    editor = EditorUser.objects.create(email='retention-editor@example.com', password_hash='x')
    Membership.objects.create(user=editor, organization=org, role='editor')
    detail = admin_client(editor).get(f'/api/admin/venue-submissions/{expired.id}/')
    assert detail.status_code == 200
    payload = detail.json()['submission']
    assert payload['retention_purged_at'] is not None
    assert payload['manuscript']['content_retained'] is False
    assert payload['manuscript']['abstract'] == ''

    download = admin_client(editor).get(f'/api/admin/venue-submissions/{expired.id}/download/')
    assert download.status_code == 410


@pytest.mark.django_db
def test_last_expired_venue_copy_purges_shared_manuscript_and_derived_artifacts():
    author = Author.objects.create(email='final-retention@example.com', name='Final Author', password_hash='x')
    manuscript = make_manuscript(author, suffix='-final')
    org = Organization.objects.create(name='Final Retention Org')
    venue = Venue.objects.create(
        organization=org,
        name='Final Retention Venue',
        slug='final-retention-venue',
        venue_type='journal',
    )
    config = VenueAgentConfig.objects.create(
        venue=venue,
        version=1,
        active=True,
        retention_days=1,
    )
    submission = VenueSubmission.objects.create(
        manuscript=manuscript,
        venue=venue,
        venue_config=config,
        status='rejected',
        submitted_at=timezone.now() - timedelta(days=5),
        retention_expires_at=timezone.now() - timedelta(days=4),
        editorial_brief={'editor_summary': 'Generated content'},
        decision={'decision': 'rejected', 'note': 'Human decision is retained.'},
    )
    ReadinessAssessment.objects.create(
        manuscript=manuscript,
        status='completed',
        engine_version='mechanical-v1',
        summary={'word_count': 1234, 'ready_for_matching': True},
        findings=[{'detail': 'Derived readiness text'}],
    )
    VenueMatch.objects.create(
        manuscript=manuscript,
        venue=venue,
        venue_config=config,
        eligibility='eligible',
        fit_summary='Derived fit summary',
        reasons=['Derived reason'],
        gaps=[],
        evidence=[],
    )
    stored_name = manuscript.manuscript_file.name
    storage = manuscript.manuscript_file.storage

    sweep_retention_task()

    submission.refresh_from_db()
    manuscript.refresh_from_db()
    assert submission.retention_purged_at is not None
    assert submission.decision['decision'] == 'rejected'
    assert manuscript.content_purged_at is not None
    assert not manuscript.manuscript_file
    assert manuscript.manuscript_bytes == 0
    assert manuscript.parsed_profile == {}
    assert manuscript.abstract == ''
    assert manuscript.notes == ''
    assert not storage.exists(stored_name)
    assert manuscript.readiness_assessments.count() == 0
    assert manuscript.venue_matches.count() == 0


@pytest.mark.django_db
def test_retention_schedule_installer_is_idempotent():
    call_command('install_retention_schedule', verbosity=0)
    call_command('install_retention_schedule', verbosity=0)

    schedules = Schedule.objects.filter(name='flexee-retention-sweep')
    assert schedules.count() == 1
    schedule = schedules.get()
    assert schedule.func == 'review.tasks.sweep_retention_task'
    assert schedule.schedule_type == Schedule.HOURLY
    assert schedule.repeats == -1
