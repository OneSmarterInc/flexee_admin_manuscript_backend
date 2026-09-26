import io
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

from review.auth import issue_session
from review.models import (
    Author,
    EditorUser,
    Manuscript,
    Membership,
    Organization,
    Venue,
    VenueSubmission,
)
from review.storage_security import (
    UploadSecurityError,
    sanitize_original_filename,
    validate_manuscript_zip,
)


def _zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return buf.getvalue()


def test_sanitize_original_filename_removes_paths_and_control_characters():
    assert sanitize_original_filename('../folder\\evil\r\nname.pdf') == 'evil_name.pdf'
    assert sanitize_original_filename('..') == 'upload'


def test_valid_manuscript_zip_is_accepted(monkeypatch):
    monkeypatch.setenv('MANUSCRIPT_ZIP_MAX_FILES', '5')
    payload = _zip_bytes([
        ('chapters/chapter-1.md', b'# Chapter 1\nSafe text'),
        ('notes/readme.txt', b'ignored'),
    ])

    result = validate_manuscript_zip(payload)

    assert result['entries'] == 2
    assert result['supported_manuscript_files'] == 1
    assert result['uncompressed_bytes'] > 0


def test_zip_path_traversal_is_rejected():
    payload = _zip_bytes([
        ('../outside.md', b'unsafe'),
        ('chapter.md', b'safe'),
    ])

    with pytest.raises(UploadSecurityError, match='unsafe member path'):
        validate_manuscript_zip(payload)


def test_zip_symlink_is_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as archive:
        symlink = zipfile.ZipInfo('linked.md')
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(symlink, 'target.md')

    with pytest.raises(UploadSecurityError, match='symlink'):
        validate_manuscript_zip(buf.getvalue())


def test_zip_compression_bomb_ratio_is_rejected(monkeypatch):
    monkeypatch.setenv('MANUSCRIPT_ZIP_MAX_COMPRESSION_RATIO', '20')
    monkeypatch.setenv('MANUSCRIPT_ZIP_MAX_UNCOMPRESSED_BYTES', str(5 * 1024 * 1024))
    payload = _zip_bytes([
        ('chapter.md', b'A' * (2 * 1024 * 1024)),
    ])

    with pytest.raises(UploadSecurityError, match='compression ratio'):
        validate_manuscript_zip(payload)


@pytest.mark.django_db
def test_author_zip_traversal_is_rejected_before_persistence(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path
    payload = _zip_bytes([
        ('../outside.md', b'unsafe'),
        ('chapter.md', b'safe'),
    ])
    upload = SimpleUploadedFile(
        'book.zip',
        payload,
        content_type='application/zip',
    )

    response = Client().post('/api/author/manuscripts/', {
        'title': 'Unsafe archive',
        'author': 'Author',
        'email': 'author@example.com',
        'manuscript_type': 'book',
        'disclosure': 'AI was used for editing assistance.',
        'attestation': 'true',
        'manuscript': upload,
    })

    assert response.status_code == 400
    assert 'unsafe member path' in response.json()['detail']
    assert Manuscript.objects.count() == 0
    assert not list(tmp_path.rglob('*'))


@pytest.mark.django_db
def test_authenticated_manuscript_download_is_attachment_and_not_cached(tmp_path, settings, monkeypatch):
    settings.MEDIA_ROOT = tmp_path
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'storage-security-test-secret')

    org = Organization.objects.create(name='Private Files Org', organization_type='journal')
    venue = Venue.objects.create(
        organization=org,
        name='Private Files Journal',
        slug='private-files-journal',
        venue_type='journal',
    )
    author = Author.objects.create(
        email='private-author@example.com',
        password_hash='dummy',
        name='Private Author',
        email_verified=True,
    )
    manuscript = Manuscript.objects.create(
        author_account=author,
        author_name='Private Author',
        author_email=author.email,
        title='Private Manuscript',
        manuscript_type='research_article',
        disclosure='AI editing assistance disclosed.',
        manuscript_filename='private.md',
        manuscript_file=SimpleUploadedFile(
            'private.md',
            b'# private manuscript',
            content_type='text/markdown',
        ),
        manuscript_bytes=20,
        manuscript_sha256='a' * 64,
    )
    submission = VenueSubmission.objects.create(
        manuscript=manuscript,
        venue=venue,
        status='submitted',
    )
    editor = EditorUser.objects.create(
        email='private-editor@example.com',
        password_hash='dummy',
    )
    Membership.objects.create(user=editor, organization=org, role='editor')

    token, _ = issue_session(editor.email)
    client = Client()
    client.cookies['flxee_admin_session'] = token

    response = client.get(f'/api/admin/venue-submissions/{submission.id}/download/')

    assert response.status_code == 200
    assert response['Content-Disposition'].startswith('attachment;')
    assert 'private.md' in response['Content-Disposition']
    assert response['Cache-Control'] == 'private, no-store, max-age=0'
    assert response['X-Content-Type-Options'] == 'nosniff'
    assert response['X-Frame-Options'] == 'DENY'
    assert response['Content-Security-Policy'] == 'sandbox'

    # Neither the old conventional path nor the private pseudo-URL is routed by Django.
    assert client.get('/media/author_manuscripts/private.md').status_code == 404
    assert client.get('/__private_media__/author_manuscripts/private.md').status_code == 404


def _production_env(tmp_path):
    env = os.environ.copy()
    env.update({
        'DJANGO_ENV': 'production',
        'DJANGO_SECRET_KEY': 'production-test-secret-key',
        'DATABASE_URL': 'postgresql://test:test@localhost:5432/test',
        'DJANGO_ALLOWED_HOSTS': 'api.example.test',
        'FRONTEND_ORIGINS': 'https://app.example.test',
        'ADMIN_SESSION_SECRET': 'production-session-secret',
        'PRIVATE_MEDIA_ROOT': str(tmp_path),
        'SECURE_SSL_REDIRECT': 'true',
        'SENTRY_DSN': '',
        'TRUSTED_PROXIES': '127.0.0.1',
    })
    return env


def _import_settings(env):
    return subprocess.run(
        [
            sys.executable,
            '-c',
            (
                'import config.settings as s; '
                'print(s.PRODUCTION); '
                'print(s.MEDIA_ROOT); '
                'print(s.ALLOWED_HOSTS); '
                'print(s.SECURE_SSL_REDIRECT)'
            ),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_production_settings_accept_explicit_private_storage(tmp_path):
    result = _import_settings(_production_env(tmp_path))

    assert result.returncode == 0, result.stderr
    assert 'True' in result.stdout
    assert str(tmp_path.resolve()) in result.stdout
    assert 'api.example.test' in result.stdout


def test_production_settings_reject_missing_private_media_root(tmp_path):
    env = _production_env(tmp_path)
    env.pop('PRIVATE_MEDIA_ROOT', None)

    result = _import_settings(env)

    assert result.returncode != 0
    assert 'PRIVATE_MEDIA_ROOT must be configured in production' in result.stderr


def test_production_settings_reject_wildcard_allowed_hosts(tmp_path):
    env = _production_env(tmp_path)
    env['DJANGO_ALLOWED_HOSTS'] = '*'

    result = _import_settings(env)

    assert result.returncode != 0
    assert 'DJANGO_ALLOWED_HOSTS may not contain *' in result.stderr


def test_production_settings_reject_insecure_frontend_origin(tmp_path):
    env = _production_env(tmp_path)
    env['FRONTEND_ORIGINS'] = 'http://app.example.test'

    result = _import_settings(env)

    assert result.returncode != 0
    assert 'FRONTEND_ORIGINS must use https://' in result.stderr
