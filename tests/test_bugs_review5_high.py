"""Review 5: the three Review 4 fixes that blocked legitimate use.

Each test asserts the behaviour a real editor or author needs, and fails on main at 9c09793.
"""
import io
import json
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from review.auth import COOKIE_NAME, hash_password, issue_session
from review.models import EditorUser, Membership, Organization, Venue, VenueClaim
from tests.test_author_dashboard_actions import author_client, env  # noqa: F401
from tests.test_venue_claims import CLAIM, admin, outbox, post, token_from  # noqa: F401


# R5-1 ---------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_r5_1_an_owner_can_claim_the_venue_they_already_run(outbox):  # noqa: F811
    """Review 4's A1 refused approval whenever the venue's organization had any member — including the
    claimant. The owner of a 'listed' venue could then never reach 'Editor-confirmed'."""
    org = Organization.objects.create(name='Journal of Applied AI editorial office')
    owner = EditorUser.objects.create(email='owner@jaai.example', password_hash=hash_password('x' * 14))
    Membership.objects.create(user=owner, organization=org, role='owner')
    client = Client(raise_request_exception=False)
    client.cookies[COOKIE_NAME] = issue_session(owner.email)[0]
    assert post(client, '/api/admin/venues/', {'name': 'Journal of Applied AI', 'venue_type': 'journal',
                                               'organization_id': str(org.id), 'slug': 'jaai'}).status_code == 201
    venue = Venue.objects.get(slug='jaai')

    created = post(Client(), '/api/journals/claim/', {'kind': 'venue', 'key': 'jaai',
                                                      'email': owner.email, **CLAIM})
    assert created.status_code == 201, created.content
    post(Client(), '/api/journals/claim/verify/', {'token': token_from(outbox[-1])})
    claim = VenueClaim.objects.get(email=owner.email)

    approved = post(admin(), f'/api/admin/claims/{claim.id}/approve/', {})
    assert approved.status_code == 200, approved.content
    claim.refresh_from_db()
    assert claim.status == 'approved'
    venue.refresh_from_db()
    # The venue stays in the organization the owner already had, rather than moving to a second one.
    assert venue.organization_id == org.id
    assert Membership.objects.filter(user=owner, organization=org, role='owner').exists()
    assert Organization.objects.count() == 1, 'the owner was left holding a second organization'
    # The approved claim is what lets the venue reach 'Editor-confirmed' when the owner saves its rules.
    assert venue.claims.filter(status='approved').exists()


@pytest.mark.django_db
def test_r5_1_a_stranger_still_cannot_take_a_venue_other_editors_hold(outbox):  # noqa: F811
    """The A1 protection itself must survive the fix."""
    org = Organization.objects.create(name='Held editorial office')
    holder = EditorUser.objects.create(email='holder@held.example', password_hash=hash_password('x' * 14))
    Membership.objects.create(user=holder, organization=org, role='owner')
    client = Client(raise_request_exception=False)
    client.cookies[COOKIE_NAME] = issue_session(holder.email)[0]
    assert post(client, '/api/admin/venues/', {'name': 'Held Journal', 'venue_type': 'journal',
                                               'organization_id': str(org.id), 'slug': 'held'}).status_code == 201
    venue = Venue.objects.get(slug='held')

    post(Client(), '/api/journals/claim/', {'kind': 'venue', 'key': 'held',
                                            'email': 'intruder@example.com', **CLAIM})
    post(Client(), '/api/journals/claim/verify/', {'token': token_from(outbox[-1])})
    claim = VenueClaim.objects.get(email='intruder@example.com')

    assert post(admin(), f'/api/admin/claims/{claim.id}/approve/', {}).status_code == 409
    venue.refresh_from_db()
    assert venue.organization_id == org.id


# R5-2 ---------------------------------------------------------------------------------------------

def _docx(parts):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<Types/>')
        archive.writestr('word/document.xml', '<w:document><w:body/></w:document>')
        archive.writestr('word/_rels/document.xml.rels', '<Relationships/>')
        for name, blob in parts:
            archive.writestr(name, blob)
    return buffer.getvalue()


def _upload(client, upload):
    return client.post('/api/author/manuscripts/', {
        'title': 'A study', 'author': 'Priya', 'email': 'priya@example.com',
        'manuscript_type': 'practitioner_article', 'disclosure': 'None', 'attestation': 'true',
        'manuscript': upload})


@pytest.mark.django_db
def test_r5_2_a_word_file_with_many_figures_uploads(env):  # noqa: F811
    """Review 4's A3 applied the 40-entry bundle limit to .docx. Word writes a part per image plus its
    relationships, so a manuscript with about twenty figures was refused as 'too many files'."""
    client, _author = author_client()
    # Twenty-five figures: image parts, their relationships, and the usual styles and theme parts.
    parts = [(f'word/media/image{i}.png', b'\x89PNG\r\n\x1a\n' + bytes(2000)) for i in range(25)]
    parts += [(f'word/_rels/image{i}.png.rels', b'<Relationships/>') for i in range(25)]
    parts += [('word/styles.xml', b'<styles/>'), ('word/theme/theme1.xml', b'<theme/>'),
              ('docProps/core.xml', b'<props/>'), ('docProps/app.xml', b'<props/>')]
    package = _docx(parts)
    with zipfile.ZipFile(io.BytesIO(package)) as check:
        assert len(check.infolist()) > 40, 'the probe must exceed the old bundle limit'

    response = _upload(client, SimpleUploadedFile('paper.docx', package,
                                                  content_type='application/vnd.openxmlformats-officedocument'
                                                               '.wordprocessingml.document'))
    assert response.status_code == 201, response.content


@pytest.mark.django_db
def test_r5_2_a_declared_gigabyte_document_xml_is_still_refused(env, monkeypatch):  # noqa: F811
    """The A3 protection itself must survive the fix: the part that gets parsed is still ratio-checked."""
    monkeypatch.setenv('OOXML_MAX_UNCOMPRESSED_BYTES', str(4 * 1024 * 1024))
    client, _author = author_client()
    package = _docx([('word/bomb.xml', b'x' * (8 * 1024 * 1024))])
    assert len(package) < 64 * 1024, 'tiny on the wire'
    response = _upload(client, SimpleUploadedFile('paper.docx', package, content_type='application/octet-stream'))
    assert response.status_code == 400, response.content


# R5-8 ---------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_r5_8_a_mixed_case_editor_stays_signed_in(env):  # noqa: F811
    """Review 4's A5 made the sign-in match the address case-insensitively, but the session was issued for
    the address as typed while require_admin matched exactly: the editor signed in and was then refused."""
    EditorUser.objects.create(email='Asha@JISP.example.org', password_hash=hash_password('a-long-secret-1'))
    client = Client(raise_request_exception=False)
    signed_in = client.post('/api/admin/login/',
                            data=json.dumps({'username': 'asha@jisp.example.org', 'password': 'a-long-secret-1'}),
                            content_type='application/json')
    assert signed_in.status_code == 200, signed_in.content
    # The very next admin request must be accepted with the cookie that login just set.
    assert client.get('/api/admin/venues/').status_code != 401
