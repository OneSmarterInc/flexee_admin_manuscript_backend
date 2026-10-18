"""Flow review: the author's own dead ends, on the backend side.

Each test fails on main at bfc4a06.
"""
import json
import re

import pytest
from django.test import Client

from review.models import Author
from tests.test_author_dashboard_actions import env  # noqa: F401


def _register(client=None, email='new.author@example.org'):
    client = client or Client(raise_request_exception=False)
    return client, client.post('/api/author/register/', data=json.dumps(
        {'email': email, 'name': 'A New Author', 'password': 'a-long-enough-password'}),
        content_type='application/json')


# F5: the verification link had no page behind it --------------------------------------------------

@pytest.mark.django_db
def test_the_verification_link_sends_the_author_to_the_portal(env, monkeypatch, settings):  # noqa: F811
    """The link was built against the API, which answers in bare JSON. Every new author clicked it and
    landed on a blank tab reading {"detail": "Email verified successfully"}: no branding, nothing to
    click, and their open workspace tab still refusing uploads until they thought to reload."""
    sent = []
    from review import author_api
    monkeypatch.setattr(author_api, '_send_email',
                        lambda **kw: sent.append(kw) or {'sent': True})
    monkeypatch.setenv('FRONTEND_ORIGINS', 'https://manuscripts.flexee.example')
    _client, created = _register()
    assert created.status_code in {200, 201}, created.content
    assert sent, 'no verification email was sent'
    link = re.search(r'https?://\S+', sent[-1]['body']).group(0)
    assert link.startswith('https://manuscripts.flexee.example/author/verify-email?token='), link
    assert '/api/' not in link, 'the author is still sent to the API'


@pytest.mark.django_db
def test_an_old_verification_link_still_lands_somewhere_useful(env, monkeypatch):  # noqa: F811
    """Links already in inboxes point at the API. A browser opening one is sent on to the portal page
    rather than shown JSON."""
    monkeypatch.setenv('FRONTEND_ORIGINS', 'https://manuscripts.flexee.example')
    author = Author.objects.create(email='old.link@example.org', name='Old Link',
                                   password_hash='x', email_verified=False)
    from django.core.signing import dumps
    token = dumps({'author_id': str(author.id)})
    response = Client().get(f'/api/author/verify-email/?token={token}', HTTP_ACCEPT='text/html')
    assert response.status_code == 302, response.content
    assert response['Location'].startswith('https://manuscripts.flexee.example/author/verify-email?token=')


@pytest.mark.django_db
def test_clicking_the_verification_link_twice_is_not_an_error(env, monkeypatch):  # noqa: F811
    """The author who opens the link, then re-opens the tab from history, used to be told the token was
    invalid even though their account was verified. Nothing on that screen said they were fine."""
    monkeypatch.delenv('FRONTEND_ORIGINS', raising=False)
    author = Author.objects.create(email='twice@example.org', name='Twice',
                                   password_hash='x', email_verified=False)
    from django.core.signing import dumps
    token = dumps({'author_id': str(author.id)})
    first = Client().get(f'/api/author/verify-email/?token={token}')
    assert first.status_code == 200, first.content
    author.refresh_from_db()
    assert author.email_verified is True

    second = Client().get(f'/api/author/verify-email/?token={token}')
    assert second.status_code == 200, second.content
    assert second.json().get('ok') is True


@pytest.mark.django_db
def test_an_expired_verification_link_says_which_problem_it_is(env, monkeypatch):  # noqa: F811
    """'Invalid or expired token' covered both an expired link, which a resend fixes, and a tampered
    one, which it does not. The page could not tell the author which of the two they had."""
    monkeypatch.delenv('FRONTEND_ORIGINS', raising=False)
    broken = Client().get('/api/author/verify-email/?token=not-a-real-token')
    assert broken.status_code == 400
    assert broken.json().get('code') == 'token_invalid'

    missing = Client().get('/api/author/verify-email/')
    assert missing.status_code == 400
    assert missing.json().get('code') == 'token_missing'
