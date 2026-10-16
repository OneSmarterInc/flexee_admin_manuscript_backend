"""Review 5, R5-7 and R5-9: the upload check that reached only the first upload, and the author mail
that still went to the free-text address."""
import io
import json
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from review import editor_api
from review.models import VenueSubmission
from tests.test_author_dashboard_actions import author_client, env, make_manuscript, make_venue  # noqa: F401
from tests.test_bugs_high import _claim_all, _editor  # noqa: F401


def _docx_bomb(uncompressed_bytes):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<Types/>')
        archive.writestr('word/document.xml', '<w:document>' + ('x' * uncompressed_bytes) + '</w:document>')
    return buffer.getvalue()


def _bomb_upload(name='paper.docx'):
    return SimpleUploadedFile(name, _docx_bomb(8 * 1024 * 1024), content_type='application/octet-stream')


def _create(client, **fields):
    data = {'title': 'A study', 'author': 'Priya', 'email': 'priya@example.com',
            'manuscript_type': 'practitioner_article', 'disclosure': 'None', 'attestation': 'true',
            'manuscript': SimpleUploadedFile('paper.md', b'# A study\n\nText.\n', content_type='text/markdown')}
    data.update(fields)
    return client.post('/api/author/manuscripts/', data)


# R5-7 ---------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _small_cap(monkeypatch):
    monkeypatch.setenv('OOXML_MAX_UNCOMPRESSED_BYTES', str(4 * 1024 * 1024))


@pytest.mark.django_db
def test_r5_7_a_new_version_gets_the_same_archive_limits(env):  # noqa: F811
    """Review 4's A3 ran only on create, so the .docx refused on the first upload was accepted as a
    revised version — and the readiness check inflates it in the worker just the same."""
    client, _author = author_client()
    created = _create(client)
    assert created.status_code == 201
    manuscript_id = created.json()['manuscript']['id']
    response = client.post(f'/api/author/manuscripts/{manuscript_id}/versions/',
                           {'manuscript': _bomb_upload(), 'change_note': 'revised'})
    assert response.status_code == 400, response.content


@pytest.mark.django_db
def test_r5_7_editing_a_manuscript_gets_the_same_archive_limits(env):  # noqa: F811
    client, _author = author_client()
    created = _create(client)
    manuscript_id = created.json()['manuscript']['id']
    response = client.post(f'/api/author/manuscripts/{manuscript_id}/update/', {
        'title': 'A study', 'author': 'Priya', 'email': 'priya@example.com',
        'manuscript_type': 'practitioner_article', 'disclosure': 'None', 'attestation': 'true',
        'manuscript': _bomb_upload()})
    assert response.status_code == 400, response.content


@pytest.mark.django_db
def test_r5_7_a_docx_inside_a_bundle_zip_is_scanned(env):  # noqa: F811
    """The bundle's own member count says nothing about what a .docx inside it declares."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr('manuscript.docx', _docx_bomb(8 * 1024 * 1024))
    client, _author = author_client()
    response = _create(client, manuscript=SimpleUploadedFile('bundle.zip', buffer.getvalue(),
                                                             content_type='application/zip'))
    assert response.status_code == 400, response.content


# R5-9 ---------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_r5_9_a_title_with_a_bare_web_address_is_refused(env):  # noqa: F811
    """Only '://' was checked, so 'www.evil.example' passed and went into the mail Flexee sends."""
    client, _author = author_client()
    assert _create(client, title='Read more at www.evil.example').status_code == 400


@pytest.mark.django_db
def test_r5_9_a_new_version_cannot_rename_the_manuscript_to_a_web_address(env):  # noqa: F811
    client, _author = author_client()
    manuscript_id = _create(client).json()['manuscript']['id']
    response = client.post(f'/api/author/manuscripts/{manuscript_id}/versions/', {
        'manuscript': SimpleUploadedFile('v2.md', b'# v2\n\nText.\n', content_type='text/markdown'),
        'change_note': 'revised', 'title': 'Claim your prize at www.evil.example'})
    assert response.status_code == 400, response.content


@pytest.mark.django_db
def test_r5_9_an_ordinary_title_still_passes(env):  # noqa: F811
    """The check must not refuse real titles: a colon, an ampersand, an @ in a product name."""
    client, _author = author_client()
    assert _create(client, title='Design@Scale: Co-ordination & Practice in Large Teams').status_code == 201


@pytest.mark.django_db
def test_r5_9_the_editors_decision_goes_to_the_account_address(env, monkeypatch):  # noqa: F811
    """Review 4 sent the packet confirmation to the signed-in address; the editor's decision mail, which
    carries the same author-chosen name and title, still used the free-text contact field."""
    sent = []
    monkeypatch.setattr(editor_api, 'send_acceptance_email',
                        lambda adapter, note, venue_name=None: sent.append(adapter.author_email))
    client, author = author_client()
    venue = make_venue('Field Notes Journal', 'fnj')
    manuscript = make_manuscript(author)
    manuscript.author_email = 'victim@example.org'
    manuscript.save(update_fields=['author_email'])
    submission = VenueSubmission.objects.create(manuscript=manuscript, venue=venue,
                                                venue_config=venue.agent_configs.first(),
                                                status='submitted', version=manuscript.current_version,
                                                packet={})
    editor = _editor(venue, email='editor@fnj.example')
    decided = editor.post(f'/api/admin/venue-submissions/{submission.id}/decision/',
                          data=json.dumps({'decision': 'accepted', 'note': 'Welcome'}),
                          content_type='application/json')
    assert decided.status_code == 200, decided.content
    assert sent == [author.email], sent
