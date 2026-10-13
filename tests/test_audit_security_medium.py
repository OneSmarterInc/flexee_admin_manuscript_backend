"""12 October audit, security items 24, 25 and 26.

24 DNS rebinding: the address checked is the address connected to; a host that answers "public"
   and then "internal" never gets a connection to the internal address.
25 The public submit page counts toward the service-wide storage limit.
26 An authenticator code works once: the same code cannot be replayed.
"""
import os
import time
from unittest import mock

import httpcore
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from review.auth import hash_password, totp_code
from review.models import EditorUser, Submission
from review.services import venue_discovery as vd

# ---------------------------------------------------------------------------
# Item 24
# ---------------------------------------------------------------------------


def test_the_connection_refuses_an_internal_address(monkeypatch):
    monkeypatch.setattr(vd, '_resolve', lambda host: {'169.254.169.254'})
    with pytest.raises(httpcore.ConnectError, match='private or internal'):
        vd._PublicOnlyBackend().connect_tcp('journal.example', 443, timeout=1)


def test_dns_rebinding_after_the_check_is_still_refused(monkeypatch):
    """The first lookup (the URL check) says public; the second (the connection) says internal."""
    answers = iter([{'93.184.216.34'}, {'127.0.0.1'}])
    monkeypatch.setattr(vd, '_resolve', lambda host: next(answers))
    assert vd.validate_public_url('https://journal.example/guide') == 'https://journal.example/guide'
    with pytest.raises(httpcore.ConnectError, match='private or internal'):
        vd._PublicOnlyBackend().connect_tcp('journal.example', 443, timeout=1)


def test_a_mixed_answer_is_refused(monkeypatch):
    monkeypatch.setattr(vd, '_resolve', lambda host: {'93.184.216.34', '10.0.0.5'})
    with pytest.raises(httpcore.ConnectError):
        vd._PublicOnlyBackend().connect_tcp('journal.example', 443, timeout=1)


def test_a_public_address_is_connected_by_its_checked_ip(monkeypatch):
    monkeypatch.setattr(vd, '_resolve', lambda host: {'93.184.216.34'})
    with mock.patch.object(httpcore.SyncBackend, 'connect_tcp', return_value='stream') as connect:
        assert vd._PublicOnlyBackend().connect_tcp('journal.example', 443, timeout=1) == 'stream'
    assert connect.call_args.args[0] == '93.184.216.34'   # the checked address, not the name


def test_the_fetcher_uses_the_guarded_connection():
    fetcher = vd.SafeFetcher(vd.DiscoveryConfig.from_env())
    backend = fetcher.client._transport._pool._network_backend
    assert isinstance(backend, vd._PublicOnlyBackend)


# ---------------------------------------------------------------------------
# Item 25
# ---------------------------------------------------------------------------

FORM = {'type': 'article', 'author': 'Test', 'email': 'test@test.com', 'title': 'Test', 'disclosure': 'Test',
        'attestation': 'human-authored-with-ai-assistance'}


@pytest.mark.django_db
@mock.patch.dict(os.environ, {'AI_PROVIDER': 'mock', 'TOTAL_STORAGE_LIMIT_BYTES': '1000'})
def test_the_public_page_respects_the_service_storage_limit():
    Submission.objects.create(status='completed', kind='article', author_name='A', title='Old',
                              disclosure='x', attestation=True, manuscript_filename='old.md',
                              manuscript_file=SimpleUploadedFile('old.md', b'x' * 900), manuscript_bytes=900)
    response = Client().post('/api/submissions/', data={**FORM, 'manuscript': SimpleUploadedFile('new.md', b'y' * 200)})
    assert response.status_code == 413 and response.json()['code'] == 'storage_quota_exceeded'
    assert Submission.objects.count() == 1


@pytest.mark.django_db
@mock.patch.dict(os.environ, {'AI_PROVIDER': 'mock', 'TOTAL_STORAGE_LIMIT_BYTES': '100000'})
def test_the_public_page_still_accepts_within_the_limit():
    response = Client().post('/api/submissions/', data={**FORM, 'manuscript': SimpleUploadedFile('new.md', b'y' * 200)})
    assert response.status_code in (200, 201, 202)


# ---------------------------------------------------------------------------
# Item 26
# ---------------------------------------------------------------------------

SECRET = 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP'


@pytest.mark.django_db
@mock.patch.dict(os.environ, {'ADMIN_SESSION_SECRET': 'x', 'TEST_BYPASS_ORIGIN': '1'})
def test_an_authenticator_code_works_only_once():
    EditorUser.objects.create(email='root@x.org', password_hash=hash_password('a-long-password'),
                              platform_superuser=True, totp_secret=SECRET)
    code = totp_code(SECRET, int(time.time()))
    body = {'username': 'root@x.org', 'password': 'a-long-password', 'totp': code}
    first = Client().post('/api/admin/login/', data=body, content_type='application/json')
    assert first.status_code == 200
    replay = Client().post('/api/admin/login/', data=body, content_type='application/json')
    assert replay.status_code == 401
    assert EditorUser.objects.get(email='root@x.org').totp_last_step is not None


@pytest.mark.django_db
@mock.patch.dict(os.environ, {'ADMIN_SESSION_SECRET': 'x', 'TEST_BYPASS_ORIGIN': '1'})
def test_an_older_code_is_refused_after_a_newer_one():
    now = int(time.time())
    user = EditorUser.objects.create(email='root@x.org', password_hash=hash_password('a-long-password'),
                                     platform_superuser=True, totp_secret=SECRET)
    newer = totp_code(SECRET, now)
    older = totp_code(SECRET, now - 30)
    body = {'username': 'root@x.org', 'password': 'a-long-password'}
    assert Client().post('/api/admin/login/', data={**body, 'totp': newer},
                         content_type='application/json').status_code == 200
    if older != newer:
        assert Client().post('/api/admin/login/', data={**body, 'totp': older},
                             content_type='application/json').status_code == 401
    user.refresh_from_db()
    assert user.totp_last_step == now // 30
