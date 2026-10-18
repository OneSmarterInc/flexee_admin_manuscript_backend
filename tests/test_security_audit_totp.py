"""Security audit, A07: a password on its own must not enrol the second factor.

admin_verify_password used to mint a fresh authenticator secret and hand back the otpauth:// URI for
any account whose secret was blank, to anyone who presented the correct password. A phished or reused
editor password was then enough to register the attacker's own authenticator and finish signing in —
the one thing the second factor exists to stop. Enrolment belongs to the emailed set-password page.

These fail on main at a8af296.
"""
import json
import os
from unittest.mock import patch

import pytest
from django.test import Client

from review.auth import hash_password, start_totp_setup, totp_code
from review.models import EditorUser, Membership, Organization


PASSWORD = 'a-long-enough-editor-password'


@pytest.fixture
def env():
    with patch.dict(os.environ, {
        'ADMIN_SESSION_SECRET': 'totp-audit-secret-that-is-long-enough-for-anybody',
        'DJANGO_SECRET_KEY': 'totp-audit-django-secret-long-enough-for-signing',
    }, clear=False):
        yield


def _editor(email, *, with_totp, superuser=False):
    user = EditorUser.objects.create(email=email, password_hash=hash_password(PASSWORD),
                                     platform_superuser=superuser)
    if not superuser:
        org = Organization.objects.create(name=f'Office of {email}')
        Membership.objects.create(user=user, organization=org, role='owner')
    if with_totp:
        start_totp_setup(user)
        user.refresh_from_db()
    return user


def _password_step(email, password=PASSWORD):
    return Client(raise_request_exception=False).post(
        '/api/admin/verify-password/',
        data=json.dumps({'username': email, 'password': password}),
        content_type='application/json')


@pytest.mark.django_db
def test_the_password_step_never_hands_out_an_authenticator_secret(env):
    """The attacker has the password and nothing else. They must not be able to enrol."""
    _editor('legacy@journal.example', with_totp=False)
    response = _password_step('legacy@journal.example')

    assert response.status_code == 403, response.content
    body = response.json()
    assert body.get('code') == 'totp_not_enrolled'
    assert 'totp_setup_uri' not in body, 'the authenticator secret was handed to the caller'
    assert 'otpauth' not in response.content.decode(), 'the authenticator secret leaked in the body'
    # And nothing was written: a later, legitimate set-password link still sets the account up.
    assert EditorUser.objects.get(email='legacy@journal.example').totp_secret == ''


@pytest.mark.django_db
def test_a_superuser_without_an_authenticator_cannot_enrol_from_the_sign_in_page(env):
    _editor('root@flexee.example', with_totp=False, superuser=True)
    response = _password_step('root@flexee.example')
    assert response.status_code == 403, response.content
    assert EditorUser.objects.get(email='root@flexee.example').totp_secret == ''


@pytest.mark.django_db
def test_an_editor_who_is_enrolled_still_signs_in_normally(env):
    """The fix must not break the ordinary two-step sign-in."""
    user = _editor('enrolled@journal.example', with_totp=True)
    first = _password_step('enrolled@journal.example')
    assert first.status_code == 200, first.content
    ticket = first.json()['login_ticket']

    second = Client(raise_request_exception=False).post(
        '/api/admin/login/',
        data=json.dumps({'username': 'enrolled@journal.example', 'ticket': ticket,
                         'totp': totp_code(user.totp_secret)}),
        content_type='application/json')
    assert second.status_code == 200, second.content


@pytest.mark.django_db
def test_a_wrong_password_still_looks_the_same_as_before(env):
    """The new 403 must not become an oracle: a wrong password answers 401 as it always did, so the
    response does not reveal which accounts have no authenticator."""
    _editor('enrolled2@journal.example', with_totp=True)
    _editor('legacy2@journal.example', with_totp=False)

    assert _password_step('enrolled2@journal.example', 'wrong-password').status_code == 401
    assert _password_step('legacy2@journal.example', 'wrong-password').status_code == 401
    assert _password_step('nobody-at-all@journal.example', 'wrong-password').status_code == 401
