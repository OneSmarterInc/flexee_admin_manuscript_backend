import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from review.auth import AUTHOR_COOKIE_NAME, COOKIE_NAME, issue_author_session, issue_session
from review.models import (
    Author,
    EditorUser,
    Manuscript,
    Membership,
    Organization,
    ReadinessAssessment,
    SubmissionRequirementFile,
    Venue,
    VenueAgentConfig,
    VenueSubmission,
)


@pytest.fixture(autouse=True)
def venue_feature_env(monkeypatch, settings, tmp_path):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'venue-feature-test-secret')
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


def manuscript_for(author, *, title='Test manuscript', manuscript_type='research_article'):
    payload = b'# Test manuscript\n\nA short manuscript used for venue workflow tests.\n'
    return Manuscript.objects.create(
        author_account=author,
        author_name='Test Author',
        author_email=author.email,
        title=title,
        manuscript_type=manuscript_type,
        disclosure='AI was used for copy editing.',
        manuscript_filename='test.md',
        manuscript_file=SimpleUploadedFile('test.md', payload, content_type='text/markdown'),
        manuscript_bytes=len(payload),
        manuscript_sha256='f' * 64,
    )


@pytest.mark.django_db
def test_existing_venue_without_new_requirements_submits_unchanged():
    author = Author.objects.create(email='legacy@example.com', name='Legacy Author', password_hash='x')
    manuscript = manuscript_for(author)
    org = Organization.objects.create(name='Legacy Org')
    venue = Venue.objects.create(
        organization=org,
        name='Legacy Venue',
        slug='legacy-venue',
        venue_type='journal',
    )
    config = VenueAgentConfig.objects.create(
        venue=venue,
        version=1,
        active=True,
        aims_scope='Legacy venue scope.',
    )
    submission = VenueSubmission.objects.create(
        manuscript=manuscript,
        venue=venue,
        venue_config=config,
        status='packet_ready',
        packet={'editorial_brief_ready': True},
    )

    response = author_client(author).post(
        f'/api/author/venue-submissions/{submission.id}/submit/',
        data='{}',
        content_type='application/json',
    )

    assert response.status_code == 200
    submission.refresh_from_db()
    assert submission.status == 'submitted'
    assert response.json()['submission']['requirements']['configured'] is False
    assert response.json()['submission']['requirements']['complete'] is True


@pytest.mark.django_db
def test_required_text_checkbox_and_file_items_block_until_complete():
    author = Author.objects.create(email='requirements@example.com', name='Requirements Author', password_hash='x')
    manuscript = manuscript_for(author)
    org = Organization.objects.create(name='Requirements Org')
    venue = Venue.objects.create(
        organization=org,
        name='Requirements Venue',
        slug='requirements-venue',
        venue_type='journal',
    )
    config = VenueAgentConfig.objects.create(
        venue=venue,
        version=1,
        active=True,
        aims_scope='Requirements venue scope.',
        required_submission_items=[
            {'key': 'orcid', 'label': 'ORCID', 'type': 'text', 'required': True, 'help_text': '', 'max_length': 100},
            {'key': 'author_confirm', 'label': 'Author confirmation', 'type': 'checkbox', 'required': True, 'help_text': '', 'max_length': 1},
            {'key': 'cover_letter', 'label': 'Cover letter', 'type': 'file', 'required': True, 'help_text': '', 'max_length': 4000},
        ],
    )
    submission = VenueSubmission.objects.create(
        manuscript=manuscript,
        venue=venue,
        venue_config=config,
        status='packet_ready',
        packet={'editorial_brief_ready': True},
    )
    client = author_client(author)

    detail = client.get(f'/api/author/venue-submissions/{submission.id}/')
    assert detail.status_code == 200
    assert detail.json()['submission']['requirements']['complete'] is False

    blocked = client.post(
        f'/api/author/venue-submissions/{submission.id}/submit/',
        data='{}',
        content_type='application/json',
    )
    assert blocked.status_code == 409
    assert blocked.json()['requirements']['complete'] is False

    saved = client.post(
        f'/api/author/venue-submissions/{submission.id}/requirements/',
        data=json.dumps({
            'responses': {
                'orcid': '0000-0002-1825-0097',
                'author_confirm': True,
            }
        }),
        content_type='application/json',
    )
    assert saved.status_code == 200
    assert saved.json()['requirements']['complete'] is False

    uploaded = client.post(
        f'/api/author/venue-submissions/{submission.id}/requirements/cover_letter/upload/',
        data={'file': SimpleUploadedFile('cover-letter.pdf', b'%PDF-1.4 test cover letter', content_type='application/pdf')},
    )
    assert uploaded.status_code == 200
    assert uploaded.json()['requirements']['complete'] is True
    assert SubmissionRequirementFile.objects.filter(
        venue_submission=submission,
        requirement_key='cover_letter',
    ).exists()

    submitted = client.post(
        f'/api/author/venue-submissions/{submission.id}/submit/',
        data='{}',
        content_type='application/json',
    )
    assert submitted.status_code == 200
    assert submitted.json()['submission']['status'] == 'submitted'


@pytest.mark.django_db
def test_editor_can_download_completed_requirement_file():
    author = Author.objects.create(email='download-author@example.com', name='Download Author', password_hash='x')
    manuscript = manuscript_for(author)
    org = Organization.objects.create(name='Download Org')
    venue = Venue.objects.create(
        organization=org,
        name='Download Venue',
        slug='download-venue',
        venue_type='journal',
    )
    config = VenueAgentConfig.objects.create(
        venue=venue,
        version=1,
        active=True,
        required_submission_items=[
            {'key': 'cover_letter', 'label': 'Cover letter', 'type': 'file', 'required': True, 'help_text': '', 'max_length': 4000},
        ],
    )
    submission = VenueSubmission.objects.create(
        manuscript=manuscript,
        venue=venue,
        venue_config=config,
        status='submitted',
    )
    row = SubmissionRequirementFile.objects.create(
        venue_submission=submission,
        requirement_key='cover_letter',
        original_filename='cover-letter.pdf',
        file=SimpleUploadedFile('cover-letter.pdf', b'%PDF-1.4 editor copy', content_type='application/pdf'),
        file_bytes=20,
        file_sha256='a' * 64,
    )
    editor = EditorUser.objects.create(email='editor-download@example.com', password_hash='x')
    Membership.objects.create(user=editor, organization=org, role='editor')

    response = admin_client(editor).get(
        f'/api/admin/venue-submissions/{submission.id}/requirements/cover_letter/download/'
    )

    assert response.status_code == 200
    assert 'attachment' in response.headers.get('Content-Disposition', '').lower()
    assert row.original_filename in response.headers.get('Content-Disposition', '')


@pytest.mark.django_db
def test_structured_word_count_rule_marks_match_ineligible_and_blocks_routing():
    author = Author.objects.create(email='desk-rule@example.com', name='Desk Rule Author', password_hash='x')
    manuscript = manuscript_for(author)
    ReadinessAssessment.objects.create(
        manuscript=manuscript,
        status='completed',
        engine_version='mechanical-v1',
        summary={
            'word_count': 9001,
            'blocking_issues': 0,
            'warnings': 0,
            'ready_for_matching': True,
        },
        findings=[],
    )
    org = Organization.objects.create(name='Desk Rule Org')
    venue = Venue.objects.create(
        organization=org,
        name='Desk Rule Venue',
        slug='desk-rule-venue',
        venue_type='journal',
    )
    VenueAgentConfig.objects.create(
        venue=venue,
        version=1,
        active=True,
        aims_scope='Research in operations.',
        article_types=['research_article'],
        desk_rejection_rules=['Keep manuscripts concise.'],
        structured_desk_rejection_rules=[
            {
                'field': 'word_count',
                'operator': '>',
                'value': 8000,
                'message': 'Maximum manuscript length is 8,000 words.',
            }
        ],
    )
    client = author_client(author)

    matches = client.post(
        f'/api/author/manuscripts/{manuscript.id}/matches/run/',
        data='{}',
        content_type='application/json',
    )

    assert matches.status_code == 201
    result = next(item for item in matches.json()['matches'] if item['venue']['id'] == str(venue.id))
    assert result['eligibility'] == 'ineligible'
    assert 'Maximum manuscript length is 8,000 words.' in result['gaps']

    create = client.post(
        f'/api/author/manuscripts/{manuscript.id}/submissions/',
        data=json.dumps({'venue_id': str(venue.id)}),
        content_type='application/json',
    )
    assert create.status_code == 409


@pytest.mark.django_db
def test_invalid_structured_venue_configuration_is_rejected_without_affecting_old_fields():
    org = Organization.objects.create(name='Config Org')
    venue = Venue.objects.create(
        organization=org,
        name='Config Venue',
        slug='config-venue',
        venue_type='journal',
    )
    owner = EditorUser.objects.create(email='config-owner@example.com', password_hash='x')
    Membership.objects.create(user=owner, organization=org, role='owner')
    client = admin_client(owner)

    invalid = client.post(
        f'/api/admin/venues/{venue.id}/config/',
        data=json.dumps({
            'aims_scope': 'Configured scope',
            'desk_rejection_rules': ['Existing free-text guidance remains supported.'],
            'structured_desk_rejection_rules': [
                {'field': 'unknown_field', 'operator': '>', 'value': 1, 'message': 'Bad rule'}
            ],
            'required_submission_items': [],
        }),
        content_type='application/json',
    )

    assert invalid.status_code == 400
    assert VenueAgentConfig.objects.filter(venue=venue).count() == 0

    valid = client.post(
        f'/api/admin/venues/{venue.id}/config/',
        data=json.dumps({
            'aims_scope': 'Configured scope',
            'desk_rejection_rules': ['Existing free-text guidance remains supported.'],
            'structured_desk_rejection_rules': [
                {'field': 'word_count', 'operator': '>', 'value': 8000, 'message': 'Maximum 8,000 words.'}
            ],
            'required_submission_items': [
                {'key': 'orcid', 'label': 'ORCID', 'type': 'text', 'required': True}
            ],
        }),
        content_type='application/json',
    )

    assert valid.status_code == 201
    cfg = VenueAgentConfig.objects.get(venue=venue)
    assert cfg.desk_rejection_rules == ['Existing free-text guidance remains supported.']
    assert cfg.structured_desk_rejection_rules[0]['field'] == 'word_count'
    assert cfg.required_submission_items[0]['key'] == 'orcid'


@pytest.mark.django_db
def test_transfer_rechecks_target_structured_desk_rules():
    author = Author.objects.create(email='transfer-rule@example.com', name='Transfer Rule Author', password_hash='x')
    manuscript = manuscript_for(author)
    ReadinessAssessment.objects.create(
        manuscript=manuscript,
        status='completed',
        engine_version='mechanical-v1',
        summary={
            'word_count': 9001,
            'blocking_issues': 0,
            'warnings': 0,
            'ready_for_matching': True,
        },
        findings=[],
    )
    org = Organization.objects.create(name='Transfer Rule Org')
    source_venue = Venue.objects.create(
        organization=org,
        name='Source Venue',
        slug='source-venue-rule-test',
        venue_type='journal',
    )
    target_venue = Venue.objects.create(
        organization=org,
        name='Target Venue',
        slug='target-venue-rule-test',
        venue_type='journal',
    )
    source_config = VenueAgentConfig.objects.create(
        venue=source_venue,
        version=1,
        active=True,
        aims_scope='Source scope.',
    )
    VenueAgentConfig.objects.create(
        venue=target_venue,
        version=1,
        active=True,
        aims_scope='Target scope.',
        structured_desk_rejection_rules=[
            {
                'field': 'word_count',
                'operator': '>',
                'value': 8000,
                'message': 'Target venue maximum is 8,000 words.',
            }
        ],
    )
    source = VenueSubmission.objects.create(
        manuscript=manuscript,
        venue=source_venue,
        venue_config=source_config,
        status='rejected',
    )

    response = author_client(author).post(
        f'/api/author/venue-submissions/{source.id}/transfer/',
        data=json.dumps({'venue_id': str(target_venue.id), 'reason': 'Try another venue'}),
        content_type='application/json',
    )

    assert response.status_code == 409
    assert response.json()['violations'][0]['message'] == 'Target venue maximum is 8,000 words.'
    source.refresh_from_db()
    assert source.status == 'rejected'
    assert VenueSubmission.objects.filter(manuscript=manuscript).count() == 1


@pytest.mark.django_db
def test_reference_count_and_required_sections_rules_are_deterministic():
    author = Author.objects.create(email='structure-rule@example.com', name='Structure Rule Author', password_hash='x')
    payload = (
        b'# Test manuscript\n\n'
        b'## Introduction\nBackground text.\n\n'
        b'## Methods\nMethods text.\n\n'
        b'## References\nSmith, J. (2024). One reference title. Journal Name.\n'
    )
    manuscript = Manuscript.objects.create(
        author_account=author,
        author_name='Structure Rule Author',
        author_email=author.email,
        title='Structured rule manuscript',
        manuscript_type='research_article',
        disclosure='No competing interests.',
        manuscript_filename='structured.md',
        manuscript_file=SimpleUploadedFile('structured.md', payload, content_type='text/markdown'),
        manuscript_bytes=len(payload),
        manuscript_sha256='e' * 64,
    )
    ReadinessAssessment.objects.create(
        manuscript=manuscript,
        status='completed',
        engine_version='mechanical-v1',
        summary={
            'word_count': 1000,
            'blocking_issues': 0,
            'warnings': 0,
            'ready_for_matching': True,
        },
        findings=[],
    )
    org = Organization.objects.create(name='Structured Rule Org')
    venue = Venue.objects.create(
        organization=org,
        name='Structured Rule Venue',
        slug='structured-rule-venue',
        venue_type='journal',
    )
    VenueAgentConfig.objects.create(
        venue=venue,
        version=1,
        active=True,
        aims_scope='Research articles.',
        article_types=['research_article'],
        structured_desk_rejection_rules=[
            {
                'field': 'reference_count',
                'operator': '<',
                'value': 2,
                'message': 'At least two references are required.',
            },
            {
                'field': 'required_sections',
                'operator': 'missing_any',
                'value': ['Methods', 'Results'],
                'message': 'Methods and Results sections are required.',
            },
        ],
    )

    response = author_client(author).post(
        f'/api/author/manuscripts/{manuscript.id}/matches/run/',
        data='{}',
        content_type='application/json',
    )

    assert response.status_code == 201
    result = next(item for item in response.json()['matches'] if item['venue']['id'] == str(venue.id))
    assert result['eligibility'] == 'ineligible'
    assert 'At least two references are required.' in result['gaps']
    assert 'Methods and Results sections are required.' in result['gaps']

    rules = [
        evidence['rule']
        for evidence in result['evidence']
        if evidence.get('rule')
    ]
    reference_rule = next(rule for rule in rules if rule['field'] == 'reference_count')
    section_rule = next(rule for rule in rules if rule['field'] == 'required_sections')
    assert reference_rule['actual'] == 1
    assert section_rule['actual']['missing'] == ['Results']
