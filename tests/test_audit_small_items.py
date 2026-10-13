"""12 October audit, small items: 22 (fit floor without a score), 32 (sign-in timing, sign out other
devices, optional purge of old public submission files), 38 (screening skips refusing sites,
deadlines that end everywhere)."""
import os
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from review import auth, views
from review.auth import AUTHOR_COOKIE_NAME, issue_author_session
from review.models import IndexedVenue, Submission, VenueMatch
from review.services import freshness
from tests.test_author_dashboard_actions import author_client, env  # noqa: F401
from tests.test_submission_plan import generate, setup_plan


# ---------------------------------------------------------------------------
# 22
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_without_a_similarity_score_an_off_topic_venue_is_left_out():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    VenueMatch.objects.filter(manuscript=m).update(topic_similarity=None)   # embeddings off
    with mock.patch('review.submission_plan._keyword_fit',
                    side_effect=lambda match: 0.0 if match.venue_id == vs['ready_strong'].id else 0.5):
        plan = generate(client, m)
    names = [s['venue']['name'] for s in plan['steps']]
    assert vs['ready_strong'].name not in names and vs['ready_moderate'].name in names
    assert any('too few topics' in (n.get('reason') or '') for n in plan['not_included'])


# ---------------------------------------------------------------------------
# 32
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_an_unknown_email_costs_a_password_check_too(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'x')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')
    calls = []
    real = views.verify_password
    monkeypatch.setattr(views, 'verify_password', lambda p, h: calls.append(h) or real(p, h))
    response = Client().post('/api/admin/verify-password/', data={'username': 'nobody@x.org', 'password': 'pw'},
                             content_type='application/json')
    assert response.status_code == 401 and len(calls) == 1      # hashed against a dummy, like a real account
    from review import author_api
    seen = []
    monkeypatch.setattr(author_api, 'verify_password', lambda p, h: seen.append(h) or False)
    response = Client().post('/api/author/login/', data={'email': 'nobody@x.org', 'password': 'pw'},
                             content_type='application/json')
    assert response.status_code == 401 and seen == [author_api._dummy_password_hash()]


@pytest.mark.django_db
def test_sign_out_other_devices_ends_old_sessions_and_keeps_this_one():
    client, author = author_client()
    with mock.patch.object(auth.time, 'time', return_value=time.time() - 30):
        old_token, _ = issue_author_session(author.id)        # another device, signed in earlier
    other = Client()
    other.cookies[AUTHOR_COOKIE_NAME] = old_token
    assert other.get('/api/author/manuscripts/list/').status_code == 200
    response = client.post('/api/author/sign-out-other-devices/')
    assert response.status_code == 200
    assert other.get('/api/author/manuscripts/list/').status_code == 401     # the other device is out
    assert client.get('/api/author/manuscripts/list/').status_code == 200    # this browser stays in


@pytest.mark.django_db
def test_sign_out_other_devices_needs_a_session():
    assert Client().post('/api/author/sign-out-other-devices/').status_code == 401


def _old_public_submission(days_old):
    s = Submission.objects.create(status='completed', kind='article', author_name='A', title='Old', disclosure='x',
                                  attestation=True, manuscript_filename='old.md', manuscript_bytes=3,
                                  manuscript_file=SimpleUploadedFile('old.md', b'abc'))
    Submission.objects.filter(id=s.id).update(created_at=timezone.now() - timedelta(days=days_old))
    s.refresh_from_db()
    return s


@pytest.mark.django_db
def test_old_public_files_are_kept_unless_retention_is_turned_on(django_capture_on_commit_callbacks):
    from review.tasks import purge_old_public_submission_files
    s = _old_public_submission(800)
    name, storage = s.manuscript_file.name, s.manuscript_file.storage
    with django_capture_on_commit_callbacks(execute=True):
        assert purge_old_public_submission_files(timezone.now()) == 0
    assert storage.exists(name)
    with mock.patch.dict(os.environ, {'PUBLIC_SUBMISSION_RETENTION_DAYS': '365'}):
        with django_capture_on_commit_callbacks(execute=True):
            assert purge_old_public_submission_files(timezone.now()) == 1
    s.refresh_from_db()
    assert not s.manuscript_file and not storage.exists(name) and s.title == 'Old'   # the record stays


# ---------------------------------------------------------------------------
# 38
# ---------------------------------------------------------------------------

def test_a_call_stays_open_until_its_deadline_has_ended_everywhere():
    # 15 March, 08:00 UTC: it is still 14 March in Hawaii (UTC-10) and Baker Island (UTC-12).
    now = datetime(2027, 3, 15, 8, 0, tzinfo=dt_timezone.utc)
    assert freshness.deadline_today(now).isoformat() == '2027-03-14'
    # 16 March, 12:30 UTC: 15 March has ended everywhere.
    later = datetime(2027, 3, 16, 12, 30, tzinfo=dt_timezone.utc)
    assert freshness.deadline_today(later).isoformat() == '2027-03-16'


@pytest.mark.django_db
def test_screening_skips_a_site_that_refused_automated_reading(monkeypatch):
    from review.services import index_screening as scr
    from tests.test_index_rules import journal
    monkeypatch.setenv('VENUE_INDEX_PAGE_WORKERS', '1')
    monkeypatch.setenv('VENUE_INDEX_PAGE_DELAY_SECONDS', '0')
    # Both have a catalogue concern, so screening would read their pages.
    refused = journal('Refusing Journal', openalex_id='SR', homepage='https://refuses.example/j',
                      rules_status='blocked', rules_read_at=timezone.now() - timedelta(days=5), screening_points=2)
    open_one = journal('Open Journal', openalex_id='SO', homepage='https://open.example/j', screening_points=2)
    asked = []
    monkeypatch.setattr(scr, 'page_evidence', lambda record, fetcher: asked.append(record.title) or ([], ''))
    run = mock.Mock(pages_checked=0)
    scr.gather_page_evidence(run, 'business-is', lambda: True, fetcher=object())
    assert 'Refusing Journal' not in asked and 'Open Journal' in asked
    refused.refresh_from_db()
    assert 'not requested again' in refused.last_error
    assert IndexedVenue.objects.get(id=open_one.id).pages_checked_at is not None
