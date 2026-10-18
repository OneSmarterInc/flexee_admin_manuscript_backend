"""Security audit: the CSRF cookie is not readable by JavaScript.

The token itself is public by design — the protection is that another origin cannot read it, which the
CORS allowlist enforces. But this frontend takes the token from the JSON body of /api/csrf/ and never
from document.cookie, so the cookie can be HttpOnly at no cost, and an XSS on one of our own pages
then cannot lift the token out of the cookie jar.

The second test is the one that matters on a future change: it proves saves still work with the flag
on, so nobody reverts it after seeing a 403 somewhere unrelated.
"""
import json

import pytest
from django.conf import settings
from django.test import Client

from review.auth import COOKIE_NAME, hash_password, issue_session
from review.models import EditorUser, Organization, Venue


@pytest.mark.django_db
def test_the_csrf_cookie_is_not_readable_from_javascript():
    response = Client().get('/api/csrf/')
    assert response.status_code == 200, response.content
    assert response.json().get('csrfToken'), 'the token is still returned in the body'
    cookie = response.cookies.get(settings.CSRF_COOKIE_NAME)
    assert cookie is not None, 'no CSRF cookie was set'
    assert cookie['httponly'], 'the CSRF cookie can be read by page scripts'


@pytest.mark.django_db
def test_an_unsafe_request_still_succeeds_with_the_token_from_the_body(monkeypatch):
    """The frontend reads the token from the response body and sends it as X-CSRFToken. That has to
    keep working, or someone will turn the flag back off."""
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'csrf-audit-secret-long-enough-for-signing')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')
    org = Organization.objects.create(name='CSRF Audit Office')
    venue = Venue.objects.create(organization=org, name='CSRF Journal', slug='csrf-journal',
                                 venue_type='journal')
    owner = EditorUser.objects.create(email='csrf-owner@example.org',
                                      password_hash=hash_password('x' * 14))
    from review.models import Membership
    Membership.objects.create(user=owner, organization=org, role='owner')

    client = Client(enforce_csrf_checks=True)
    client.cookies[COOKIE_NAME] = issue_session(owner.email)[0]
    token = client.get('/api/csrf/').json()['csrfToken']

    saved = client.patch(
        f'/api/admin/venues/{venue.id}/',
        data=json.dumps({'description': 'Edited with a token read from the response body.'}),
        content_type='application/json',
        HTTP_X_CSRFTOKEN=token,
    )
    assert saved.status_code == 200, saved.content
    venue.refresh_from_db()
    assert venue.description.startswith('Edited with a token')


@pytest.mark.django_db
def test_an_unsafe_request_without_the_header_is_still_refused():
    """The flag must not be mistaken for the protection: no header, no write."""
    client = Client(enforce_csrf_checks=True)
    client.get('/api/csrf/')
    refused = client.post('/api/journals/claim/', data=json.dumps({'kind': 'venue', 'key': 'x'}),
                          content_type='application/json')
    assert refused.status_code == 403, refused.content
