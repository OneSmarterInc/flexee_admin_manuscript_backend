import json

import pytest
from django.test import Client

from review.auth import cookie_samesite, hash_password
from review.models import Author


@pytest.mark.parametrize('env,secure,expected', [
    (None, False, 'Strict'),
    (None, True, 'Strict'),
    ('lax', False, 'Lax'),
    ('None', True, 'None'),
    ('None', False, 'Lax'),      # browsers drop SameSite=None without Secure
    ('bogus', True, 'Strict'),
])
def test_cookie_samesite(monkeypatch, env, secure, expected):
    if env is None:
        monkeypatch.delenv('COOKIE_SAMESITE', raising=False)
    else:
        monkeypatch.setenv('COOKIE_SAMESITE', env)
    assert cookie_samesite(secure) == expected


@pytest.mark.django_db
def test_local_author_login_cookie_is_usable(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'cookie-secret')
    monkeypatch.setenv('COOKIE_SAMESITE', 'None')  # even if set, plain HTTP must not get SameSite=None
    monkeypatch.delenv('COOKIE_SECURE', raising=False)
    Author.objects.create(email='a@example.com', name='A', password_hash=hash_password('password1'), email_verified=True)
    client = Client()
    response = client.post('/api/author/login/', data=json.dumps({'email': 'a@example.com', 'password': 'password1'}),
                           content_type='application/json')
    assert response.status_code == 200
    cookie = response.cookies['flxee_author_session']
    assert cookie['samesite'] == 'Lax'
    assert client.get('/api/author/session/').status_code == 200
