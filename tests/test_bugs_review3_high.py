from types import SimpleNamespace
"""High findings of review 3 (C1, P1, Y1, F1 backend part). Each test failed before its fix."""
import json
import re
import uuid
from datetime import timedelta

import pytest
from django.core import mail
from django.test import Client
from django.utils import timezone

from review.auth import hash_password
from review.models import Author, ReviewJob
from review.services.email_service import build_html_email
from tests.test_audit_plan_medium import _packet
from tests.test_author_dashboard_actions import author_client, env  # noqa: F401
from tests.test_bugs_high import _claim_all, _editor, _venue_of
from tests.test_submission_plan import activate, generate, post, setup_plan, step


def _hrefs(html):
    return re.findall(r'href="([^"]*)"', html)


def _decide(ec, sub, decision, note='editor note'):
    return ec.post(f'/api/admin/venue-submissions/{sub.id}/decision/',
                   data=json.dumps({'decision': decision, 'note': note}), content_type='application/json')


def _current(body):
    return next((s for s in body['steps'] if s['id'] == body['current_step_id']), None)


# C1: HTML email links were cut at the first letter "s" -----------------------------------------------

def test_c1_html_email_link_is_not_cut_at_the_letter_s():
    url = 'https://app.flexee.org/author/reset-password?token=abc'
    html = build_html_email('Reset', f'Open the link below:\n\n{url}\n\nThanks')
    assert _hrefs(html) == [url]


@pytest.mark.django_db
def test_c1_password_reset_email_html_link_opens_the_reset_page(monkeypatch):
    monkeypatch.setenv('AUTHOR_PORTAL_BASE_URL', 'https://flexee.org')
    Author.objects.create(email='reset@example.com', name='Priya', password_hash=hash_password('pass12345'),
                          email_verified=True)
    r = Client().post('/api/author/password-reset/', data=json.dumps({'email': 'reset@example.com'}),
                      content_type='application/json')
    assert r.status_code == 200
    message = mail.outbox[-1]
    reset_url = re.search(r'(https://flexee\.org/author/reset-password\?token=\S+)', message.body).group(1)
    assert reset_url in _hrefs(message.alternatives[0][0])


# P1: a venue that already declined was proposed again as the current step ----------------------------

@pytest.mark.django_db
def test_p1_rejection_while_plan_is_draft_is_skipped_on_start():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    _claim_all(vs)
    plan = generate(client, m)
    first = plan['steps'][0]
    venue = _venue_of(vs, first)
    sub = _packet(m, venue)
    assert client.post(f'/api/author/venue-submissions/{sub.id}/submit/').status_code == 200
    assert _decide(_editor(venue), sub, 'rejected').status_code == 200
    body = activate(client, plan)
    current = _current(body)
    assert current is None or current['venue']['name'] != first['venue']['name']


@pytest.mark.django_db
def test_p1_rejection_while_plan_is_paused_is_skipped_after_unchanged():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first, second = plan['steps'][0], plan['steps'][1]
    assert step(client, plan, first, 'submitted').status_code == 200
    assert step(client, plan, first, 'outcome', outcome='declined', note='No').status_code == 200
    venue2 = _venue_of(vs, second)
    venue2.trust_tier = 'claimed'
    venue2.save(update_fields=['trust_tier'])
    sub = _packet(m, venue2)
    assert client.post(f'/api/author/venue-submissions/{sub.id}/submit/').status_code == 200
    assert _decide(_editor(venue2), sub, 'rejected').status_code == 200
    r = post(client, f'/api/author/plans/{plan["id"]}/revision/', answer='unchanged', note='go on')
    assert r.status_code == 200, r.json()
    body = r.json()['plan']
    current = _current(body)
    assert current is None or current['venue']['name'] != second['venue']['name']
    skipped = next(s for s in body['steps'] if s['id'] == second['id'])
    assert skipped['state'] == 'skipped'


# Y1: a job swept while it waited behind a long task was then dropped by _claim_job -------------------

@pytest.mark.django_db
def test_y1_job_swept_in_the_queue_still_runs_when_the_worker_reaches_it(monkeypatch):
    from review import tasks
    calls = []
    monkeypatch.setattr(tasks, 'run_semantic_readiness', lambda manuscript: calls.append(manuscript))
    monkeypatch.setattr(tasks.Manuscript.objects, 'get', lambda **kw: SimpleNamespace())
    ref = str(uuid.uuid4())
    job = ReviewJob.objects.create(job_type='semantic_readiness', reference_id=ref, status='queued')
    ReviewJob.objects.filter(id=job.id).update(created_at=timezone.now() - timedelta(minutes=15))
    tasks.sweep_stuck_jobs_task()
    job.refresh_from_db()
    assert job.status == 'failed'
    tasks.run_semantic_readiness_task(job.id, ref)
    job.refresh_from_db()
    assert calls and job.status == 'completed'


@pytest.mark.django_db
def test_y1_a_job_that_failed_for_another_reason_is_not_run_again(monkeypatch):
    from review import tasks
    calls = []
    monkeypatch.setattr(tasks, 'run_semantic_readiness', lambda manuscript: calls.append(manuscript))
    monkeypatch.setattr(tasks.Manuscript.objects, 'get', lambda **kw: SimpleNamespace())
    ref = str(uuid.uuid4())
    job = ReviewJob.objects.create(job_type='semantic_readiness', reference_id=ref, status='failed',
                                   error_message='Model error.')
    tasks.run_semantic_readiness_task(job.id, ref)
    job.refresh_from_db()
    assert not calls and job.status == 'failed'


# F1 (backend part): the plan tells the page when the editors asked for a revision --------------------

@pytest.mark.django_db
def test_f1_plan_step_carries_the_packet_status():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    venue = _venue_of(vs, first)
    venue.trust_tier = 'claimed'
    venue.save(update_fields=['trust_tier'])
    sub = _packet(m, venue)
    assert client.post(f'/api/author/venue-submissions/{sub.id}/submit/').status_code == 200
    assert _decide(_editor(venue), sub, 'revision_requested').status_code == 200
    steps = client.get(f'/api/author/manuscripts/{m.id}/plan/').json()['plan']['steps']
    row = next(s for s in steps if s['id'] == first['id'])
    assert row['state'] == 'submitted' and row['venue_submission_status'] == 'revision_requested'
    assert all(s['venue_submission_status'] is None for s in steps if s['id'] != first['id'])


@pytest.mark.django_db
def test_f1_a_new_packet_to_the_same_venue_after_a_revision_request_is_allowed():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    venue = _venue_of(vs, first)
    venue.trust_tier = 'claimed'
    venue.save(update_fields=['trust_tier'])
    sub = _packet(m, venue)
    assert client.post(f'/api/author/venue-submissions/{sub.id}/submit/').status_code == 200
    assert _decide(_editor(venue), sub, 'revision_requested').status_code == 200
    r = client.post(f'/api/author/manuscripts/{m.id}/submissions/', data=json.dumps({'venue_id': str(venue.id)}),
                    content_type='application/json')
    assert r.status_code in (200, 201), r.json()
    assert r.json()['submission']['id'] != str(sub.id) and r.json()['submission']['status'] == 'draft'
