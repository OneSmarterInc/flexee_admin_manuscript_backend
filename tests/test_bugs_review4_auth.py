"""Review 4c (auth, authorization, email, public endpoints). Each test asserts the correct behaviour and
fails on main because of the bug it names."""
import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from review import author_api
from review.auth import COOKIE_NAME, hash_password, issue_session
from review.models import EditorUser, Membership, Organization, Venue, VenueClaim, VenueSubmission
from tests.test_author_dashboard_actions import author_client, env, make_manuscript, make_venue  # noqa: F401
from tests.test_venue_claims import CLAIM, admin, outbox, post, token_from  # noqa: F401


def _editor(email, org, role='owner'):
    user = EditorUser.objects.create(email=email, password_hash=hash_password('x' * 14))
    Membership.objects.create(user=user, organization=org, role=role)
    client = Client(raise_request_exception=False)
    client.cookies[COOKIE_NAME] = issue_session(user.email)[0]
    return client, user


# A1 ------------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_a1_approving_a_claim_does_not_take_a_venue_from_the_editors_who_already_own_it(outbox):  # noqa: F811
    """A venue an org owner created through the admin API is 'listed' (not 'claimed'), so anyone can claim it
    from the public page. Approving that claim moves the venue into a new organization and the existing
    owners lose access to it."""
    org = Organization.objects.create(name='Journal of Applied AI editorial office')
    owner, _user = _editor('owner@jaai.example', org)
    created = post(owner, '/api/admin/venues/', {'name': 'Journal of Applied AI', 'venue_type': 'journal',
                                                  'organization_id': str(org.id), 'slug': 'jaai'})
    assert created.status_code == 201
    venue = Venue.objects.get(slug='jaai')
    assert owner.get(f'/api/admin/venues/{venue.id}/').status_code == 200

    response = post(Client(), '/api/journals/claim/', {'kind': 'venue', 'key': 'jaai', 'email': 'intruder@example.com',
                                                        **CLAIM})
    if response.status_code == 201:
        post(Client(), '/api/journals/claim/verify/', {'token': token_from(outbox[-1])})
        claim_ = VenueClaim.objects.get(email='intruder@example.com')
        post(admin(), f'/api/admin/claims/{claim_.id}/approve/', {})

    venue.refresh_from_db()
    assert venue.organization_id == org.id
    assert owner.get(f'/api/admin/venues/{venue.id}/').status_code == 200
    assert not Membership.objects.filter(user__email='intruder@example.com', organization=org).exists()


# A2 ------------------------------------------------------------------------------------------------

def _upload(client, **fields):
    data = {'title': 'A study', 'author': 'Priya', 'email': 'priya@example.com', 'manuscript_type': 'practitioner_article',
            'disclosure': 'None', 'attestation': 'true',
            'manuscript': SimpleUploadedFile('paper.md', b'# A study\n\nText.\n', content_type='text/markdown')}
    data.update(fields)
    return client.post('/api/author/manuscripts/', data)


@pytest.mark.django_db
def test_a2_an_author_upload_refuses_a_web_address_in_the_author_name_like_the_public_form_does():
    """Review 3, C2 made the public form, sign-up and claims refuse a name with a web address, because the name
    goes into an email Flexee sends to whatever address was given. The signed-in author upload has no such
    check, and its author_email is any address the author types (not the account's). The packet confirmation
    and the editor's decision emails then go to that address with the name and title in them."""
    client, _author = author_client()
    response = _upload(client, author='Visit https://evil.example now', email='victim@example.org')
    assert response.status_code == 400, response.content


@pytest.mark.django_db
def test_a2_the_packet_confirmation_goes_only_to_the_author_or_carries_no_link(monkeypatch):
    sent = []
    monkeypatch.setattr(author_api, '_send_email', lambda to, subject, body: sent.append((to, subject, body)) or {'sent': True})
    client, author = author_client()
    venue = make_venue('Field Notes Journal', 'fnj')
    m = make_manuscript(author)
    m.author_email = 'victim@example.org'
    m.author_name = 'Claim your prize at https://evil.example/login'
    m.save(update_fields=['author_email', 'author_name'])
    sub = VenueSubmission.objects.create(manuscript=m, venue=venue, venue_config=venue.agent_configs.first(),
                                         status='packet_ready', version=m.current_version, packet={})
    assert client.post(f'/api/author/venue-submissions/{sub.id}/submit/').status_code == 200
    for to, _subject, body in sent:
        assert to == author.email or 'https://evil.example' not in body


# A3 ------------------------------------------------------------------------------------------------

def _docx_bomb(uncompressed_bytes):
    """An OOXML package (a zip) whose word/document.xml inflates to `uncompressed_bytes`."""
    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<Types/>')
        archive.writestr('word/document.xml', '<w:document>' + ('x' * uncompressed_bytes) + '</w:document>')
    return buffer.getvalue()


@pytest.mark.django_db
def test_a3_a_docx_is_a_zip_and_gets_the_same_archive_limits(monkeypatch):
    """validate_manuscript_zip (member count, uncompressed size, compression ratio) runs only for a file named
    .zip. A .docx is a zip too, and python-docx inflates every part of it into memory when the readiness check
    runs (in the request, for the mechanical check) or when the public review job runs in the worker. A docx
    whose document.xml is declared at gigabytes passes the upload unchecked."""
    monkeypatch.setenv('MANUSCRIPT_ZIP_MAX_UNCOMPRESSED_BYTES', str(2 * 1024 * 1024))
    client, _author = author_client()
    bomb = _docx_bomb(6 * 1024 * 1024)
    assert len(bomb) < 64 * 1024  # tiny on the wire
    response = _upload(client, manuscript=SimpleUploadedFile('paper.docx', bomb, content_type='application/octet-stream'))
    assert response.status_code == 400, response.content


# A4 ------------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_a4_a_huge_page_number_on_the_public_journal_index_is_not_a_server_error():
    """journal_search slices the listed-journal queryset at (page - 1) * 20 with no upper bound on page, so an
    offset past 2**63 reaches the database and raises (SQLite: datatype mismatch; PostgreSQL: bigint out of
    range). Public, unauthenticated, one GET."""
    client = Client(raise_request_exception=False)
    response = client.get('/api/journals/?page=99999999999999999999')
    assert response.status_code < 500, response.status_code


# A5 ------------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_a5_an_editor_can_sign_in_with_the_capitalisation_they_used_on_the_claim_form():
    """The claim flow lowercases the editor's email (normalize_email) and even looks accounts up with iexact on
    approval, but admin_verify_password / admin_login look the typed username up with an exact match. An
    editor invited as Asha@JISP.example.org gets the account asha@jisp.example.org and 'Invalid username or
    password' when signing in the way they typed it on the claim form."""
    EditorUser.objects.create(email='asha@jisp.example.org', password_hash=hash_password('a-long-secret-1'))
    response = Client().post('/api/admin/verify-password/',
                             data=json.dumps({'username': 'Asha@JISP.example.org', 'password': 'a-long-secret-1'}),
                             content_type='application/json')
    assert response.status_code == 200, response.content
