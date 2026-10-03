import json
import time

import pytest
from django.test import Client

from review.auth import AUTHOR_COOKIE_NAME, hash_password, issue_author_session, verify_password
from review.models import AuditEvent, Author

URL = '/api/author/password-change/'


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'direct-change-secret')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')


@pytest.fixture
def author():
    return Author.objects.create(email='priya@example.com', name='Priya Raman',
                                 password_hash=hash_password('old-pass1'), email_verified=True)


def post(client=None, **body):
    return (client or Client()).post(URL, data=json.dumps(body), content_type='application/json')


@pytest.mark.django_db
def test_changes_password_without_signing_in(author):
    signed_in = Client()
    token, _ = issue_author_session(author.id)
    signed_in.cookies[AUTHOR_COOKIE_NAME] = token
    time.sleep(1.1)

    response = post(email=' Priya@Example.com ', current_password='old-pass1',
                    new_password='brand-new-pass', confirm_password='brand-new-pass')
    assert response.status_code == 200, response.content
    author.refresh_from_db()
    assert verify_password('brand-new-pass', author.password_hash)
    assert signed_in.get('/api/author/session/').status_code == 401  # other devices signed out
    login = Client().post('/api/author/login/', data=json.dumps({'email': 'priya@example.com', 'password': 'brand-new-pass'}),
                          content_type='application/json')
    assert login.status_code == 200
    event = AuditEvent.objects.get(action='author.password_changed')
    assert event.detail == {'method': 'sign_in_page'}


@pytest.mark.django_db
def test_wrong_password_and_unknown_email_get_the_same_reply(author):
    wrong = post(email='priya@example.com', current_password='nope', new_password='brand-new-pass', confirm_password='brand-new-pass')
    unknown = post(email='ghost@example.com', current_password='nope', new_password='brand-new-pass', confirm_password='brand-new-pass')
    assert wrong.status_code == unknown.status_code == 400
    assert wrong.json() == unknown.json()
    author.refresh_from_db()
    assert verify_password('old-pass1', author.password_hash)
    assert AuditEvent.objects.filter(action='author.password_change_failed').count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize('new,confirm,message', [
    ('short', 'short', 'at least 8'),
    ('brand-new-pass', 'other-pass-99', 'do not match'),
    ('old-pass1', 'old-pass1', 'different from the current'),
])
def test_validation(author, new, confirm, message):
    response = post(email='priya@example.com', current_password='old-pass1', new_password=new, confirm_password=confirm)
    assert response.status_code == 400 and message in response.json()['detail']


@pytest.mark.django_db
def test_email_is_required():
    assert post(email='', current_password='x', new_password='y' * 8, confirm_password='y' * 8).status_code == 400


@pytest.mark.django_db
def test_per_email_lockout_after_five_failures(author):
    for _ in range(5):
        post(email='priya@example.com', current_password='nope', new_password='brand-new-pass', confirm_password='brand-new-pass')
    blocked = post(email='priya@example.com', current_password='old-pass1', new_password='brand-new-pass', confirm_password='brand-new-pass')
    assert blocked.status_code == 429
    author.refresh_from_db()
    assert verify_password('old-pass1', author.password_hash)


@pytest.mark.django_db
def test_failures_count_toward_the_sign_in_lockout(author, monkeypatch):
    monkeypatch.setenv('AUTHOR_LOGIN_MAX_FAILURES', '3')
    for i in range(3):
        post(email=f'ghost{i}@example.com', current_password='nope', new_password='brand-new-pass', confirm_password='brand-new-pass')
    login = Client().post('/api/author/login/', data=json.dumps({'email': 'priya@example.com', 'password': 'old-pass1'}),
                          content_type='application/json')
    assert login.status_code == 429
