"""12 October audit: editors never see drafts; rate limits cannot be dodged with a forged header."""
import os

import pytest
from django.core.files.base import ContentFile
from django.test import Client, RequestFactory

from review.auth import client_address, hash_password, issue_session
from review.models import Author, EditorUser, Manuscript, Membership, Organization, Venue, VenueSubmission


@pytest.fixture
def editor_setup(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'x')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')
    org = Organization.objects.create(name='O', organization_type='journal')
    editor = EditorUser.objects.create(email='ed@x.org', password_hash=hash_password('pw'))
    Membership.objects.create(user=editor, organization=org, role='owner')
    venue = Venue.objects.create(name='V', slug='v', venue_type='journal', organization=org)
    author = Author.objects.create(email='a@x.org', password_hash=hash_password('right-password'))
    ms = Manuscript.objects.create(author_account=author, title='Secret', manuscript_filename='m.md',
                                   manuscript_file=ContentFile(b'# confidential', name='m.md'))
    draft = VenueSubmission.objects.create(manuscript=ms, venue=venue, status='draft')
    client = Client()
    client.cookies['flxee_admin_session'] = issue_session(editor.email)[0]
    return client, draft


@pytest.mark.django_db
@pytest.mark.parametrize('scope', ['editor', 'all'])
def test_editor_list_never_shows_drafts(editor_setup, scope):
    client, draft = editor_setup
    body = client.get('/api/admin/venue-submissions/', {'scope': scope}).json()
    assert str(draft.id) not in [item['id'] for item in body['items']]


@pytest.mark.django_db
@pytest.mark.parametrize('suffix', ['', 'download/', 'view/'])
def test_editor_cannot_open_a_draft(editor_setup, suffix):
    client, draft = editor_setup
    assert client.get(f'/api/admin/venue-submissions/{draft.id}/{suffix}').status_code == 404


@pytest.mark.django_db
def test_editor_sees_it_once_submitted(editor_setup):
    client, draft = editor_setup
    draft.status = 'submitted'
    draft.save(update_fields=['status'])
    assert client.get(f'/api/admin/venue-submissions/{draft.id}/').status_code == 200


def _request(remote, forwarded=None):
    extra = {'REMOTE_ADDR': remote}
    if forwarded is not None:
        extra['HTTP_X_FORWARDED_FOR'] = forwarded
    return RequestFactory().get('/', **extra)


def test_client_address_uses_the_hop_nginx_saw(monkeypatch):
    monkeypatch.setenv('TRUSTED_PROXIES', '127.0.0.1')
    # nginx appends the real address after whatever the client sent.
    assert client_address(_request('127.0.0.1', '6.6.6.6, 203.0.113.5')) == '203.0.113.5'
    assert client_address(_request('127.0.0.1', '203.0.113.5')) == '203.0.113.5'
    # A direct caller cannot pick its own address.
    assert client_address(_request('198.51.100.1', '6.6.6.6')) == '198.51.100.1'
    # No header from the proxy: the proxy address itself.
    assert client_address(_request('127.0.0.1')) == '127.0.0.1'


@pytest.mark.django_db
def test_forged_forwarded_header_does_not_reset_the_login_limit(monkeypatch):
    monkeypatch.setenv('TRUSTED_PROXIES', '127.0.0.1')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')
    monkeypatch.setenv('AUTHOR_LOGIN_MAX_FAILURES', '10')
    Author.objects.create(email='a@x.org', password_hash=hash_password('right-password'))
    codes = [
        Client(REMOTE_ADDR='127.0.0.1', HTTP_X_FORWARDED_FOR=f'10.0.0.{i}, 203.0.113.9').post(
            '/api/author/login/', data={'email': 'a@x.org', 'password': f'bad{i}'},
            content_type='application/json').status_code
        for i in range(12)
    ]
    assert codes[:10] == [401] * 10
    assert codes[10:] == [429, 429]


@pytest.mark.django_db
def test_one_account_is_limited_across_networks(monkeypatch):
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')
    monkeypatch.setenv('AUTHOR_LOGIN_MAX_FAILURES_PER_ACCOUNT', '3')
    Author.objects.create(email='a@x.org', password_hash=hash_password('right-password'))
    codes = [
        Client(REMOTE_ADDR=f'203.0.113.{i}').post(
            '/api/author/login/', data={'email': 'a@x.org', 'password': 'bad'},
            content_type='application/json').status_code
        for i in range(5)
    ]
    assert codes == [401, 401, 401, 429, 429]
    # Another account is not affected.
    Author.objects.create(email='b@x.org', password_hash=hash_password('pw-b-123456'))
    assert Client(REMOTE_ADDR='203.0.113.99').post(
        '/api/author/login/', data={'email': 'b@x.org', 'password': 'pw-b-123456'},
        content_type='application/json').status_code == 200
