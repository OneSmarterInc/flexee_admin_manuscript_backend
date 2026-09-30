import json
import re
import time

import pytest
from django.core import mail
from django.test import Client, override_settings

from review.auth import AUTHOR_COOKIE_NAME, hash_password, issue_author_session, verify_password
from review.models import AuditEvent, Author

REQUEST = '/api/author/password-reset/'
CONFIRM = '/api/author/password-reset/confirm/'


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'reset-secret')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')
    monkeypatch.setenv('AUTHOR_PORTAL_BASE_URL', 'https://manuscripts.example.org')


@pytest.fixture
def author():
    return Author.objects.create(email='priya@example.com', name='Priya Raman',
                                 password_hash=hash_password('old-pass1'), email_verified=False)


def post(client, path, **body):
    return client.post(path, data=json.dumps(body), content_type='application/json')


def reset_token():
    body = mail.outbox[-1].body
    match = re.search(r'https://manuscripts\.example\.org/author/reset-password\?token=(\S+)', body)
    assert match, body
    return match.group(1)


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
def test_full_reset_flow(author):
    other_session = Client()
    token, _ = issue_author_session(author.id)
    other_session.cookies[AUTHOR_COOKIE_NAME] = token
    time.sleep(1.1)

    response = post(Client(), REQUEST, email='Priya@Example.com ')
    assert response.status_code == 200
    assert 'If an author account exists' in response.json()['detail']
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ['priya@example.com']
    link_token = reset_token()

    check = Client().get(CONFIRM, {'token': link_token})
    assert check.status_code == 200 and check.json() == {'valid': True, 'email': 'priya@example.com'}

    done = post(Client(), CONFIRM, token=link_token, new_password='brand-new-pass', confirm_password='brand-new-pass')
    assert done.status_code == 200, done.content
    author.refresh_from_db()
    assert verify_password('brand-new-pass', author.password_hash)
    assert author.email_verified is True
    # Old sessions end; the new password signs in; the link cannot be reused.
    assert other_session.get('/api/author/session/').status_code == 401
    assert post(Client(), '/api/author/login/', email='priya@example.com', password='brand-new-pass').status_code == 200
    again = post(Client(), CONFIRM, token=link_token, new_password='another-pass-9', confirm_password='another-pass-9')
    assert again.status_code == 400 and again.json()['code'] == 'invalid_token'
    actions = list(AuditEvent.objects.values_list('action', flat=True))
    assert 'author.password_reset_requested' in actions and 'author.password_reset_completed' in actions


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
def test_unknown_email_gets_same_reply_and_no_email():
    known_shape = post(Client(), REQUEST, email='nobody@example.com')
    assert known_shape.status_code == 200
    assert 'If an author account exists' in known_shape.json()['detail']
    assert len(mail.outbox) == 0


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
def test_validation_on_confirm(author):
    post(Client(), REQUEST, email='priya@example.com')
    token = reset_token()
    mismatch = post(Client(), CONFIRM, token=token, new_password='brand-new-pass', confirm_password='other-pass-99')
    assert mismatch.status_code == 400 and 'do not match' in mismatch.json()['detail']
    short = post(Client(), CONFIRM, token=token, new_password='short', confirm_password='short')
    assert short.status_code == 400 and 'at least 8' in short.json()['detail']
    author.refresh_from_db()
    assert verify_password('old-pass1', author.password_hash)


@pytest.mark.django_db
def test_tampered_or_expired_token(author, monkeypatch):
    from django.core.signing import dumps
    from review import author_api
    assert Client().get(CONFIRM, {'token': 'garbage'}).status_code == 400
    wrong_salt = dumps({'aid': str(author.id), 'fp': author_api._password_fingerprint(author)})
    assert Client().get(CONFIRM, {'token': wrong_salt}).status_code == 400
    good = dumps({'aid': str(author.id), 'fp': author_api._password_fingerprint(author)}, salt=author_api.PASSWORD_RESET_SALT)
    monkeypatch.setattr(author_api, 'PASSWORD_RESET_MAX_AGE_SECONDS', -1)
    assert Client().get(CONFIRM, {'token': good}).status_code == 400


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
def test_link_dies_when_password_changes_another_way(author):
    post(Client(), REQUEST, email='priya@example.com')
    token = reset_token()
    author.password_hash = hash_password('changed-elsewhere')
    author.save(update_fields=['password_hash'])
    assert Client().get(CONFIRM, {'token': token}).status_code == 400


@pytest.mark.django_db
@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
def test_requests_are_rate_limited_per_email(author):
    for _ in range(5):
        response = post(Client(), REQUEST, email='priya@example.com')
        assert response.status_code == 200
    assert len(mail.outbox) == 3


@pytest.mark.django_db
def test_request_needs_an_email():
    assert post(Client(), REQUEST, email='').status_code == 400
