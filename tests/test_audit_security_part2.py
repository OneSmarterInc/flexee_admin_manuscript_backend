"""12 October audit, items 27 and 31 (backend side).

27 The admin screen no longer keeps the password for the second step: the password step returns a
   short-lived ticket, and the authenticator step sends the ticket instead of the password.
31 The password-reset page checks its link with a POST (no token in a GET address).
"""
import os
import time
from unittest import mock

import pytest
from django.core import signing
from django.test import Client

from review import views
from review.auth import hash_password, totp_code
from review.models import Author, EditorUser

SECRET = 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP'
ENV = {'ADMIN_SESSION_SECRET': 'x', 'TEST_BYPASS_ORIGIN': '1'}


def _admin():
    return EditorUser.objects.create(email='root@x.org', password_hash=hash_password('a-long-password'),
                                     platform_superuser=True, totp_secret=SECRET)


def _post(client, path, body):
    return client.post(path, data=body, content_type='application/json')


@pytest.mark.django_db
@mock.patch.dict(os.environ, ENV)
def test_the_second_step_uses_a_ticket_not_the_password():
    _admin()
    client = Client()
    first = _post(client, '/api/admin/verify-password/', {'username': 'root@x.org', 'password': 'a-long-password'})
    ticket = first.json()['login_ticket']
    assert first.status_code == 200 and ticket and 'a-long-password' not in ticket
    second = _post(client, '/api/admin/login/', {'ticket': ticket, 'totp': totp_code(SECRET, int(time.time()))})
    assert second.status_code == 200 and second.json()['username'] == 'root@x.org'


@pytest.mark.django_db
@mock.patch.dict(os.environ, ENV)
def test_a_forged_or_expired_ticket_is_refused():
    user = _admin()
    code = totp_code(SECRET, int(time.time()))
    assert _post(Client(), '/api/admin/login/', {'ticket': 'not-a-ticket', 'totp': code}).status_code == 401
    old = signing.dumps({'u': str(user.id), 'fp': views._password_fingerprint(user)}, salt=views.LOGIN_TICKET_SALT)
    with mock.patch('django.core.signing.time.time', return_value=time.time() + views.LOGIN_TICKET_MAX_AGE + 5):
        assert _post(Client(), '/api/admin/login/', {'ticket': old, 'totp': code}).status_code == 401


@pytest.mark.django_db
@mock.patch.dict(os.environ, ENV)
def test_a_password_change_voids_an_issued_ticket():
    user = _admin()
    ticket = _post(Client(), '/api/admin/verify-password/',
                   {'username': 'root@x.org', 'password': 'a-long-password'}).json()['login_ticket']
    user.password_hash = hash_password('a-new-long-password')
    user.save(update_fields=['password_hash'])
    response = _post(Client(), '/api/admin/login/', {'ticket': ticket, 'totp': totp_code(SECRET, int(time.time()))})
    assert response.status_code == 401


@pytest.mark.django_db
@mock.patch.dict(os.environ, ENV)
def test_a_ticket_still_needs_the_authenticator_code():
    _admin()
    ticket = _post(Client(), '/api/admin/verify-password/',
                   {'username': 'root@x.org', 'password': 'a-long-password'}).json()['login_ticket']
    assert _post(Client(), '/api/admin/login/', {'ticket': ticket, 'totp': '000000'}).status_code == 401


@pytest.mark.django_db
@mock.patch.dict(os.environ, ENV)
def test_older_screens_that_resend_the_password_still_work():
    _admin()
    body = {'username': 'root@x.org', 'password': 'a-long-password', 'totp': totp_code(SECRET, int(time.time()))}
    assert _post(Client(), '/api/admin/login/', body).status_code == 200


@pytest.mark.django_db
@mock.patch.dict(os.environ, ENV)
def test_the_reset_link_is_checked_with_a_post():
    from review.author_api import PASSWORD_RESET_SALT, _password_fingerprint
    author = Author.objects.create(email='a@x.org', name='A', password_hash=hash_password('old-password-123'))
    token = signing.dumps({'aid': str(author.id), 'fp': _password_fingerprint(author)}, salt=PASSWORD_RESET_SALT)
    ok = _post(Client(), '/api/author/password-reset/confirm/', {'token': token, 'check': True})
    assert ok.status_code == 200 and ok.json() == {'valid': True, 'email': 'a@x.org'}
    before = author.password_hash
    author.refresh_from_db()
    assert author.password_hash == before   # checking never changes the password
    bad = _post(Client(), '/api/author/password-reset/confirm/', {'token': 'nope', 'check': True})
    assert bad.status_code == 400 and bad.json()['code'] == 'invalid_token'
