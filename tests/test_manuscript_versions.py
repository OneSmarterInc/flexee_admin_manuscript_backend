"""8 October instructions, 2.4: manuscript versions.

A revision is a new version; nothing an earlier brief, match or submission was built on changes."""
import hashlib
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from review.models import (
    EditorUser, Manuscript, ManuscriptVersion, Membership, ReadinessAssessment, VenueMatch, VenueSubmission,
)
from review.storage_quota import storage_usage
from tests.test_author_dashboard_actions import author_client, env, make_manuscript, make_venue  # noqa: F401


def upload(client, manuscript, data=b'# Revised\n\nNew text.\n', note='Cut 500 words', name='paper-v2.md', **extra):
    return client.post(f'/api/author/manuscripts/{manuscript.id}/versions/',
                       {'manuscript': SimpleUploadedFile(name, data, content_type='text/markdown'),
                        'change_note': note, **extra})


def submit(manuscript, venue, status='submitted'):
    return VenueSubmission.objects.create(manuscript=manuscript, venue=venue, status=status,
                                          submitted_at=timezone.now() if status != 'draft' else None)


@pytest.mark.django_db
def test_new_manuscript_starts_as_version_1_and_rows_attach_to_it():
    _client, author = author_client()
    m = make_manuscript(author)
    v1 = m.current_version
    assert v1.number == 1 and v1.file.name == m.manuscript_file.name and v1.sha256 == m.manuscript_sha256
    assert ReadinessAssessment.objects.get(manuscript=m).version_id == v1.id
    venue = make_venue('Field Notes Journal', 'fnj')
    assert VenueMatch.objects.create(manuscript=m, venue=venue).version_id == v1.id
    assert submit(m, venue, 'draft').version_id == v1.id


@pytest.mark.django_db
def test_uploading_a_version_never_changes_the_submitted_one():
    client, author = author_client()
    m = make_manuscript(author)
    v1 = m.current_version
    v1_file, v1_hash = v1.file.name, v1.sha256
    venue = make_venue('Field Notes Journal', 'fnj')
    match = VenueMatch.objects.create(manuscript=m, venue=venue, eligibility='eligible')
    sent = submit(m, venue)

    response = upload(client, m, title='AI inspection, revised')
    assert response.status_code == 201, response.json()
    body = response.json()
    assert body['version']['number'] == 2 and body['version']['is_current']
    assert body['manuscript']['current_version'] == 2 and body['manuscript']['title'] == 'AI inspection, revised'

    v1.refresh_from_db()
    m.refresh_from_db()
    assert (v1.file.name, v1.sha256) == (v1_file, v1_hash)  # the submitted text is untouched
    assert v1.file.read() == b'# AI quality inspection\n\nSome text.\n'
    assert m.manuscript_file.name != v1_file and m.current_version.number == 2
    assert m.manuscript_sha256 == hashlib.sha256(b'# Revised\n\nNew text.\n').hexdigest()
    sent.refresh_from_db()
    match.refresh_from_db()
    assert sent.version_id == v1.id and match.version_id == v1.id  # old work stays with the old text
    # The new version starts with no readiness or matches of its own.
    assert not m.current_readiness().exists() and not m.current_matches().exists()
    assert m.readiness_assessments.count() == 1 and m.venue_matches.count() == 1


@pytest.mark.django_db
def test_editing_is_refused_after_submission_but_a_new_version_is_allowed():
    client, author = author_client()
    m = make_manuscript(author)
    submit(m, make_venue('Field Notes Journal', 'fnj'))
    edit = client.post(f'/api/author/manuscripts/{m.id}/update/', {
        'title': 'x', 'author': 'Priya', 'manuscript_type': 'practitioner_article', 'disclosure': 'None',
        'attestation': 'true'})
    assert edit.status_code == 409 and 'Upload a revised version' in edit.json()['detail']
    assert upload(client, m).status_code == 201
    # The new version has not been submitted, so it can be edited again.
    assert client.get(f'/api/author/manuscripts/{m.id}/').json()['manuscript']['editable'] is True


@pytest.mark.django_db
def test_draft_packets_move_to_the_new_version_and_reset():
    client, author = author_client()
    m = make_manuscript(author)
    draft = submit(m, make_venue('Field Notes Journal', 'fnj'), 'packet_ready')
    draft.editorial_brief = {'summary': 'old'}
    draft.save()
    body = upload(client, m).json()
    draft.refresh_from_db()
    assert body['reset_submissions'] == 1
    assert draft.version.number == 2 and draft.status == 'draft' and draft.editorial_brief == {}
    assert draft.packet['manuscript_filename'] == 'paper-v2.md'


@pytest.mark.django_db
def test_matching_runs_per_version_and_keeps_the_old_matches():
    client, author = author_client()
    venue = make_venue('Field Notes Journal', 'fnj')
    m = make_manuscript(author)
    assert client.post(f'/api/author/manuscripts/{m.id}/matches/run/').status_code == 201
    upload(client, m)
    ReadinessAssessment.objects.create(manuscript=m, status='completed',
                                       summary={'ready_for_matching': True, 'word_count': 500})
    assert client.post(f'/api/author/manuscripts/{m.id}/matches/run/').status_code == 201
    v1, v2 = m.versions.order_by('number')
    assert VenueMatch.objects.filter(version=v1, venue=venue).count() == 1
    assert VenueMatch.objects.filter(version=v2, venue=venue).count() == 1
    listed = client.get(f'/api/author/manuscripts/{m.id}/matches/').json()['matches']
    assert len(listed) == 1  # the author sees the current version's matches


@pytest.mark.django_db
def test_version_list_and_files():
    client, author = author_client()
    m = make_manuscript(author)
    venue = make_venue('Field Notes Journal', 'fnj')
    sent = submit(m, venue, 'rejected')
    upload(client, m, revised_after=str(sent.id))
    listing = client.get(f'/api/author/manuscripts/{m.id}/versions/').json()
    assert [v['number'] for v in listing['versions']] == [2, 1]
    assert listing['versions'][0]['revised_after_submission_id'] == str(sent.id)
    assert listing['versions'][0]['change_note'] == 'Cut 500 words'
    assert listing['versions'][1]['submissions'] == [{'id': str(sent.id), 'venue': 'Field Notes Journal',
                                                      'status': 'rejected'}]
    old = client.get(f'/api/author/manuscripts/{m.id}/versions/1/file/')
    assert old.status_code == 200 and b''.join(old.streaming_content) == b'# AI quality inspection\n\nSome text.\n'
    assert client.get(f'/api/author/manuscripts/{m.id}/versions/9/file/').status_code == 404


@pytest.mark.django_db
@pytest.mark.parametrize('kwargs, message', [
    ({'note': ''}, 'change_note is required'),
    ({'data': b'# AI quality inspection\n\nSome text.\n'}, 'identical to the current version'),
    ({'name': 'paper.exe'}, ''),
    ({'revised_after': '00000000-0000-0000-0000-000000000000'}, 'revised_after'),
])
def test_bad_uploads_are_refused(kwargs, message):
    client, author = author_client()
    m = make_manuscript(author)
    response = upload(client, m, **kwargs)
    assert response.status_code in (400, 409)
    assert message in response.json()['detail']
    assert m.versions.count() == 1


@pytest.mark.django_db
def test_version_cap(monkeypatch):
    monkeypatch.setenv('MANUSCRIPT_MAX_VERSIONS', '2')
    client, author = author_client()
    m = make_manuscript(author)
    assert upload(client, m).status_code == 201
    third = upload(client, m, data=b'third')
    assert third.status_code == 409 and third.json()['code'] == 'too_many_versions'


@pytest.mark.django_db
def test_another_author_cannot_see_or_add_versions():
    _client, author = author_client()
    m = make_manuscript(author)
    other, _ = author_client('other@example.com')
    assert other.get(f'/api/author/manuscripts/{m.id}/versions/').status_code in (401, 403, 404)
    assert upload(other, m).status_code in (401, 403, 404)
    assert other.get(f'/api/author/manuscripts/{m.id}/versions/1/file/').status_code in (401, 403, 404)


@pytest.mark.django_db
def test_earlier_versions_count_toward_storage():
    client, author = author_client()
    m = make_manuscript(author)
    before = storage_usage(author=author)
    upload(client, m, data=b'x' * 1000)
    assert storage_usage(author=author) == before + 1000  # old file still kept, new file now current


@pytest.mark.django_db
def test_editor_gets_the_version_they_received(monkeypatch):
    from review.auth import COOKIE_NAME, issue_session
    from django.test import Client
    client, author = author_client()
    m = make_manuscript(author)
    venue = make_venue('Field Notes Journal', 'fnj')
    sent = submit(m, venue)
    upload(client, m, title='Newer title')
    editor = EditorUser.objects.create(email='ed@example.com', password_hash='x')
    Membership.objects.create(user=editor, organization=venue.organization, role='editor')
    ec = Client()
    ec.cookies[COOKIE_NAME] = issue_session(editor.email)[0]
    detail = ec.get(f'/api/admin/venue-submissions/{sent.id}/')
    assert detail.status_code == 200, detail.content
    manuscript = detail.json()['submission']['manuscript']
    assert manuscript['title'] == 'AI inspection' and manuscript['manuscript_version'] == 1
    for path in ('download/', 'view/'):
        response = ec.get(f'/api/admin/venue-submissions/{sent.id}/{path}')
        assert response.status_code == 200, (path, response.status_code)
        assert b''.join(response.streaming_content) == b'# AI quality inspection\n\nSome text.\n'


@pytest.mark.django_db
def test_retention_purges_an_old_version_once_its_submissions_expire():
    from review.tasks import sweep_retention_task
    client, author = author_client()
    m = make_manuscript(author)
    venue = make_venue('Field Notes Journal', 'fnj')
    old = submit(m, venue, 'rejected')
    upload(client, m)
    v1 = m.versions.get(number=1)
    old_name = v1.file.name
    storage = v1.file.storage
    VenueSubmission.objects.filter(id=old.id).update(retention_expires_at=timezone.now() - timedelta(days=1))
    sweep_retention_task()
    v1.refresh_from_db()
    m.refresh_from_db()
    assert v1.content_purged_at and not v1.file and not storage.exists(old_name)
    assert m.content_purged_at is None and m.manuscript_file  # the current version is untouched


@pytest.mark.django_db
def test_full_purge_still_happens_when_the_current_version_expires():
    from review.tasks import sweep_retention_task
    client, author = author_client()
    m = make_manuscript(author)
    venue = make_venue('Field Notes Journal', 'fnj')
    first = submit(m, venue, 'rejected')
    upload(client, m)
    second = submit(m, make_venue('Second Journal', 'sj'), 'rejected')
    VenueSubmission.objects.filter(id__in=[first.id, second.id]).update(
        retention_expires_at=timezone.now() - timedelta(days=1))
    sweep_retention_task()
    m.refresh_from_db()
    assert m.content_purged_at and not m.manuscript_file
    assert not m.versions.filter(content_purged_at__isnull=True).exists()
    assert all(not v.file for v in m.versions.all())


@pytest.mark.django_db
def test_backfill_migration_creates_version_1(django_db_blocker):
    """Rows that existed before versions get a version 1 sharing their file."""
    import importlib
    migration = importlib.import_module('review.migrations.0038_backfill_manuscript_versions')
    from django.apps import apps
    _client, author = author_client()
    m = make_manuscript(author)
    ManuscriptVersion.objects.filter(manuscript=m).delete()
    Manuscript.objects.filter(id=m.id).update(current_version=None)
    ReadinessAssessment.objects.filter(manuscript=m).update(version=None)
    migration.forwards(apps, None)
    m.refresh_from_db()
    assert m.current_version.number == 1 and m.current_version.source == 'migration'
    assert m.current_version.file.name == m.manuscript_file.name
    assert ReadinessAssessment.objects.get(manuscript=m).version_id == m.current_version_id
