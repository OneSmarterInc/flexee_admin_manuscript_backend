import os
from unittest.mock import patch
from datetime import datetime, timezone
import pytest
from django.test import RequestFactory
from django.db import DatabaseError
from review.models import Author, EditorUser
from review.auth import (issue_session, issue_author_session, read_session, read_author_session,
                         COOKIE_NAME, AUTHOR_COOKIE_NAME, _issued_before_password_change)
from review.views import admin_logout
from review.author_api import author_logout

pytestmark = pytest.mark.django_db

@pytest.mark.parametrize('author', [False, True])
def test_logout_revokes_only_current_session(author):
    account = (Author if author else EditorUser).objects.create(email='audit@example.com', password_hash='x')
    issue = issue_author_session if author else issue_session
    read = read_author_session if author else read_session
    cookie = AUTHOR_COOKIE_NAME if author else COOKIE_NAME
    identity = account.id if author else account.email
    first, _ = issue(identity)
    other, _ = issue(identity)
    request = RequestFactory().post('/logout/')
    request.COOKIES[cookie] = first
    assert read(request)
    assert (author_logout if author else admin_logout)(request).status_code == 200
    assert read(request) is None
    request.COOKIES[cookie] = other
    assert read(request)

@pytest.mark.parametrize('author', [False, True])
def test_same_second_change_revokes_old_session_and_allows_new(author):
    account = (Author if author else EditorUser).objects.create(email='audit@example.com', password_hash='x')
    issue = issue_author_session if author else issue_session
    read = read_author_session if author else read_session
    cookie = AUTHOR_COOKIE_NAME if author else COOKIE_NAME
    identity = account.id if author else account.email
    with patch('review.auth.time.time', return_value=2000000000.1):
        old, _ = issue(identity)
    account.password_changed_at = datetime.fromtimestamp(2000000000.5, timezone.utc)
    account.save()
    with patch('review.auth.time.time', return_value=2000000000.8):
        new, _ = issue(identity)
        request = RequestFactory().get('/')
        request.COOKIES[cookie] = old
        assert read(request) is None
        request.COOKIES[cookie] = new
        assert read(request)


def test_revocation_lookup_fails_closed():
    with patch('review.models.EditorUser.objects.filter', side_effect=DatabaseError('offline')):
        assert _issued_before_password_change({'u':'a@example.com','iat':1}, 'admin')
