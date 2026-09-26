import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from review.auth import AUTHOR_COOKIE_NAME, issue_author_session
from review.models import Author, Manuscript


@pytest.fixture(autouse=True)
def storage_env(monkeypatch, settings, tmp_path):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'storage-quota-test-secret')
    settings.MEDIA_ROOT = tmp_path


def _author_client(author):
    client = Client()
    token, _ = issue_author_session(author.id)
    client.cookies[AUTHOR_COOKIE_NAME] = token
    return client


def _upload(client, *, size=16, title='Quota manuscript'):
    return client.post(
        '/api/author/manuscripts/',
        data={
            'title': title,
            'author': 'Quota Author',
            'email': 'quota@example.com',
            'manuscript_type': 'research_article',
            'disclosure': 'No competing interests.',
            'attestation': 'human-authored-with-ai-assistance',
            'manuscript': SimpleUploadedFile(
                'quota.md',
                b'x' * size,
                content_type='text/markdown',
            ),
        },
    )


@pytest.mark.django_db
def test_manuscript_upload_requires_authenticated_verified_author():
    anonymous = Client()
    response = _upload(anonymous)
    assert response.status_code == 401

    author = Author.objects.create(
        email='unverified@example.com',
        name='Unverified',
        password_hash='x',
        email_verified=False,
    )
    response = _upload(_author_client(author))
    assert response.status_code == 403


@pytest.mark.django_db
def test_author_storage_quota_blocks_upload(monkeypatch):
    monkeypatch.setenv('AUTHOR_STORAGE_LIMIT_BYTES', '100')
    monkeypatch.setenv('TOTAL_STORAGE_LIMIT_BYTES', '10000')

    author = Author.objects.create(
        email='author-limit@example.com',
        name='Author Limit',
        password_hash='x',
        email_verified=True,
    )
    Manuscript.objects.create(
        author_account=author,
        author_name='Author Limit',
        author_email=author.email,
        title='Existing',
        manuscript_type='research_article',
        disclosure='none',
        manuscript_filename='existing.md',
        manuscript_file=SimpleUploadedFile('existing.md', b'a' * 80, content_type='text/markdown'),
        manuscript_bytes=80,
        manuscript_sha256='a' * 64,
    )

    response = _upload(_author_client(author), size=30)
    assert response.status_code == 413
    payload = response.json()
    assert payload['code'] == 'storage_quota_exceeded'
    assert payload['scope'] == 'author'
    assert payload['limit_bytes'] == 100


@pytest.mark.django_db
def test_global_storage_quota_blocks_upload(monkeypatch):
    monkeypatch.setenv('AUTHOR_STORAGE_LIMIT_BYTES', '10000')
    monkeypatch.setenv('TOTAL_STORAGE_LIMIT_BYTES', '100')

    first = Author.objects.create(
        email='first@example.com',
        name='First',
        password_hash='x',
        email_verified=True,
    )
    second = Author.objects.create(
        email='second@example.com',
        name='Second',
        password_hash='x',
        email_verified=True,
    )
    Manuscript.objects.create(
        author_account=first,
        author_name='First',
        author_email=first.email,
        title='Existing global',
        manuscript_type='research_article',
        disclosure='none',
        manuscript_filename='existing.md',
        manuscript_file=SimpleUploadedFile('existing.md', b'a' * 80, content_type='text/markdown'),
        manuscript_bytes=80,
        manuscript_sha256='b' * 64,
    )

    response = _upload(_author_client(second), size=30)
    assert response.status_code == 413
    payload = response.json()
    assert payload['code'] == 'storage_quota_exceeded'
    assert payload['scope'] == 'global'
    assert payload['limit_bytes'] == 100


@pytest.mark.django_db
def test_csrf_is_enforced_for_unsafe_api_requests(monkeypatch):
    monkeypatch.setenv('AUTHOR_REGISTER_MAX', '100')
    client = Client(enforce_csrf_checks=True)

    rejected = client.post(
        '/api/author/register/',
        data='{"name":"CSRF User","email":"csrf-no-token@example.com","password":"password123"}',
        content_type='application/json',
    )
    assert rejected.status_code == 403

    token_response = client.get('/api/csrf/')
    assert token_response.status_code == 200
    token = token_response.json()['csrfToken']

    accepted = client.post(
        '/api/author/register/',
        data='{"name":"CSRF User","email":"csrf-token@example.com","password":"password123"}',
        content_type='application/json',
        HTTP_X_CSRFTOKEN=token,
    )
    assert accepted.status_code == 201
