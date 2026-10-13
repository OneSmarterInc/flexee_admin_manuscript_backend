"""12 October audit leftovers: 28 (SMTP password encrypted), 26 (2FA set up in the invite link),
22 (admin plans page queries)."""
import importlib
import json
import os
from unittest import mock

import pytest
from django.apps import apps
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from review.auth import issue_session
from review.models import EditorUser, SMTPSettings
from review.secret_fields import PREFIX, decrypt_secret, encrypt_secret
from tests.test_author_dashboard_actions import author_client, env  # noqa: F401
from tests.test_submission_plan import generate, setup_plan


def _stored_password(row_id):
    with connection.cursor() as cursor:
        cursor.execute('SELECT password FROM review_smtpsettings WHERE id = %s', [row_id])
        return cursor.fetchone()[0]


# ---------------------------------------------------------------------------
# 28
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_smtp_password_is_stored_encrypted_and_read_back_plain():
    row = SMTPSettings.objects.create(id=1, host='smtp.example', username='u', password='s3cret-pass')
    stored = _stored_password(row.id)
    assert stored.startswith(PREFIX) and 's3cret-pass' not in stored
    assert SMTPSettings.objects.get(id=1).password == 's3cret-pass'


@pytest.mark.django_db
def test_a_password_stored_before_encryption_is_read_and_encrypted_by_the_migration():
    SMTPSettings.objects.create(id=1, host='smtp.example')
    with connection.cursor() as cursor:
        cursor.execute("UPDATE review_smtpsettings SET password = 'old-plain' WHERE id = 1")
    assert SMTPSettings.objects.get(id=1).password == 'old-plain'      # still readable
    migration = importlib.import_module('review.migrations.0042_smtp_password_encrypted')
    migration.encrypt_existing(apps, None)
    assert _stored_password(1).startswith(PREFIX)
    assert SMTPSettings.objects.get(id=1).password == 'old-plain'


def test_a_changed_key_gives_an_empty_secret_not_a_crash():
    token = encrypt_secret('abc')
    with mock.patch.dict(os.environ, {'SECRET_FIELD_KEY': 'A' * 43 + '='}):
        assert decrypt_secret(token) == ''
    assert decrypt_secret(token) == 'abc'


@pytest.mark.django_db
def test_mail_uses_the_plain_password(monkeypatch):
    from review.services import email_service
    SMTPSettings.objects.create(id=1, host='smtp.example', username='u', password='s3cret-pass',
                                sender_email='a@x.org')
    seen = {}

    def fake_connection(**kwargs):
        seen.update(kwargs)
        raise RuntimeError('stop here')
    monkeypatch.setattr(email_service, 'get_connection', fake_connection)
    try:
        email_service._send('to@x.org', 'Subject', 'Body')
    except Exception:  # noqa: BLE001  (the fake connection stops the send on purpose)
        pass
    assert seen.get('password') == 's3cret-pass'


# ---------------------------------------------------------------------------
# 26
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_invite_link_sets_up_two_step_sign_in(monkeypatch):
    from django.core import signing
    from review.claims_api import SET_PASSWORD_SALT, _password_fingerprint
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')
    user = EditorUser.objects.create(email='ed@journal.org', password_hash='!unset')
    token = signing.dumps({'user': str(user.id), 'fp': _password_fingerprint(user)}, salt=SET_PASSWORD_SALT)
    response = Client().post('/api/admin/set-password/', data=json.dumps(
        {'token': token, 'password': 'a-long-password-1', 'confirm': 'a-long-password-1'}),
        content_type='application/json')
    assert response.status_code == 200, response.json()
    uri = response.json()['totp_setup_uri']
    user.refresh_from_db()
    assert uri.startswith('otpauth://totp/') and user.totp_secret and user.totp_secret in uri


@pytest.mark.django_db
def test_sign_in_does_not_hand_out_a_secret_to_an_invited_editor(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'x')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')
    from review.auth import hash_password
    EditorUser.objects.create(email='ed@journal.org', password_hash=hash_password('a-long-password-1'),
                              totp_secret='JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP', platform_superuser=True)
    body = Client().post('/api/admin/verify-password/', data=json.dumps(
        {'username': 'ed@journal.org', 'password': 'a-long-password-1'}), content_type='application/json').json()
    assert 'totp_setup_uri' not in body


# ---------------------------------------------------------------------------
# 22
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_admin_plans_page_does_not_query_per_step(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'x')
    client, author = author_client()
    m, _vs = setup_plan(client, author)
    generate(client, m)
    EditorUser.objects.create(email='root@x.org', password_hash='x', platform_superuser=True)
    admin = Client()
    admin.cookies['flxee_admin_session'] = issue_session('root@x.org')[0]
    with CaptureQueriesContext(connection) as queries:
        response = admin.get('/api/admin/plans/')
    plans = response.json()['plans']
    steps = sum(len(p['steps']) for p in plans)
    assert response.status_code == 200 and len(plans) == 1 and steps >= 4
    assert len(queries) < steps * 2   # before: several queries for every step
