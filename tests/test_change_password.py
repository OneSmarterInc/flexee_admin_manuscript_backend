import json
import time
from unittest.mock import patch

import pytest
from django.test import Client

from review.auth import (
    AUTHOR_COOKIE_NAME, COOKIE_NAME, hash_password, issue_author_session, issue_session, verify_password,
)
from review.models import AuditEvent, Author, EditorUser


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'change-password-secret')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')


def admin_client(user):
    client = Client()
    token, _ = issue_session(user.email)
    client.cookies[COOKIE_NAME] = token
    return client


def author_client(author):
    client = Client()
    token, _ = issue_author_session(author.id)
    client.cookies[AUTHOR_COOKIE_NAME] = token
    return client


def post(client, path, **body):
    return client.post(path, data=json.dumps(body), content_type='application/json')


@pytest.fixture
def admin():
    return EditorUser.objects.create(email='admin@example.com', password_hash=hash_password('old-admin-password'),
                                     platform_superuser=True)


@pytest.fixture
def author():
    return Author.objects.create(email='author@example.com', name='Priya Raman',
                                 password_hash=hash_password('old-pass1'), email_verified=True)


ADMIN_URL = '/api/admin/change-password/'
AUTHOR_URL = '/api/author/change-password/'


@pytest.mark.django_db
def test_admin_changes_password_and_other_sessions_end(admin):
    other_browser = admin_client(admin)
    time.sleep(1.1)  # sessions are compared by whole seconds
    client = admin_client(admin)
    response = post(client, ADMIN_URL, current_password='old-admin-password',
                    new_password='new-admin-password-1', confirm_password='new-admin-password-1')
    assert response.status_code == 200, response.content
    admin.refresh_from_db()
    assert verify_password('new-admin-password-1', admin.password_hash)
    assert admin.password_changed_at is not None
    # This browser got a fresh session and stays signed in.
    assert client.get('/api/admin/session/').json()['authenticated'] is True
    # A session from another browser, issued before the change, no longer works.
    assert other_browser.get('/api/admin/session/').json()['authenticated'] is False
    assert other_browser.get('/api/admin/audit-events/').status_code == 401
    assert AuditEvent.objects.filter(action='admin.password_changed', actor_id=admin.id).count() == 1


@pytest.mark.django_db
def test_admin_wrong_current_password(admin):
    client = admin_client(admin)
    response = post(client, ADMIN_URL, current_password='nope', new_password='new-admin-password-1',
                    confirm_password='new-admin-password-1')
    assert response.status_code == 400
    assert response.json()['field'] == 'current_password'
    admin.refresh_from_db()
    assert verify_password('old-admin-password', admin.password_hash)
    event = AuditEvent.objects.get(action='admin.password_change_failed')
    assert 'nope' not in json.dumps(event.detail)


@pytest.mark.django_db
@pytest.mark.parametrize('new,confirm,message', [
    ('short-pw', 'short-pw', 'at least 12'),
    ('new-admin-password-1', 'different-password-2', 'do not match'),
    ('old-admin-password', 'old-admin-password', 'different from the current'),
    ('', '', 'Enter your current password'),
])
def test_admin_validation(admin, new, confirm, message):
    client = admin_client(admin)
    response = post(client, ADMIN_URL, current_password='old-admin-password', new_password=new, confirm_password=confirm)
    assert response.status_code == 400
    assert message in response.json()['detail']


@pytest.mark.django_db
def test_admin_change_requires_session():
    response = post(Client(), ADMIN_URL, current_password='x', new_password='y' * 12, confirm_password='y' * 12)
    assert response.status_code == 401


@pytest.mark.django_db
def test_admin_attempts_are_rate_limited(admin):
    client = admin_client(admin)
    for _ in range(5):
        post(client, ADMIN_URL, current_password='wrong', new_password='new-admin-password-1',
             confirm_password='new-admin-password-1')
    response = post(client, ADMIN_URL, current_password='old-admin-password', new_password='new-admin-password-1',
                    confirm_password='new-admin-password-1')
    assert response.status_code == 429


@pytest.mark.django_db
def test_author_changes_password_and_other_sessions_end(author):
    other_browser = author_client(author)
    time.sleep(1.1)
    client = author_client(author)
    response = post(client, AUTHOR_URL, current_password='old-pass1', new_password='new-pass-22',
                    confirm_password='new-pass-22')
    assert response.status_code == 200, response.content
    author.refresh_from_db()
    assert verify_password('new-pass-22', author.password_hash)
    assert client.get('/api/author/session/').status_code == 200
    assert other_browser.get('/api/author/session/').status_code == 401
    event = AuditEvent.objects.get(action='author.password_changed')
    assert event.actor_email == 'author@example.com' and event.actor_role == 'author'
    # The new password works for sign-in.
    login = post(Client(), '/api/author/login/', email='author@example.com', password='new-pass-22')
    assert login.status_code == 200, login.content


@pytest.mark.django_db
def test_author_wrong_current_password_and_min_length(author):
    client = author_client(author)
    wrong = post(client, AUTHOR_URL, current_password='nope', new_password='new-pass-22', confirm_password='new-pass-22')
    assert wrong.status_code == 400 and wrong.json()['field'] == 'current_password'
    short = post(client, AUTHOR_URL, current_password='old-pass1', new_password='short', confirm_password='short')
    assert short.status_code == 400 and 'at least 8' in short.json()['detail']


@pytest.mark.django_db
def test_author_change_requires_session():
    response = post(Client(), AUTHOR_URL, current_password='x', new_password='y' * 8, confirm_password='y' * 8)
    assert response.status_code == 401


@pytest.mark.django_db
def test_editors_do_not_see_author_password_events(author):
    from review.models import Membership, Organization
    client = author_client(author)
    post(client, AUTHOR_URL, current_password='old-pass1', new_password='new-pass-22', confirm_password='new-pass-22')
    editor = EditorUser.objects.create(email='editor@example.com', password_hash='x')
    Membership.objects.create(user=editor, organization=Organization.objects.create(name='Org'), role='editor')
    actions = [e['action'] for e in admin_client(editor).get('/api/admin/audit-events/').json()['events']]
    assert 'author.password_changed' not in actions
