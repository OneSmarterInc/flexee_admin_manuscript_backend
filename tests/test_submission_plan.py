"""8 October instructions, 2.5: the ordered submission plan.

Order by compliance distance, then fit. Never ratings, metrics or acceptance odds. One venue at a
time. A decline never advances the plan. Every position has a reason."""
import json
import re

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from review import submission_plan as plans
from review.gap_classes import classify_violation, effort_of
from review.models import PlanPosition, ReadinessAssessment, SubmissionPlan, Venue, VenueMatch, VenueSubmission
from tests.test_author_dashboard_actions import author_client, env, make_manuscript, make_venue  # noqa: F401

BANNED = re.compile(r'probab|likel|chance|odds|%|top tier|\bA\*|ranked|rating', re.I)


def venue(name, slug, *, rules=None, types=('Practitioner article',)):
    v = make_venue(name, slug, types=types)
    if rules:
        config = v.agent_configs.get()
        config.structured_desk_rejection_rules = rules
        config.save()
    return v


def setup_plan(client, author, similarity=None):
    """Six venues: two ready (strong/moderate fit), one edit, one new section, one wrong type, one off-topic."""
    vs = {
        'ready_strong': venue('Applied Practice Review', 'apr'),
        'ready_moderate': venue('Bridge Journal', 'bridge'),
        'edit': venue('Concise Letters', 'concise', rules=[
            {'field': 'word_count', 'operator': '>', 'value': 500, 'message': ''}]),
        'section': venue('Deep Practice', 'deep', rules=[
            {'field': 'required_sections', 'operator': 'missing_any', 'value': ['Practitioner implications'],
             'message': ''}]),
        'wrong_type': venue('Empirical Quarterly', 'eq', types=('Research article',)),
        'off_topic': venue('Faraway Studies', 'far'),
    }
    m = make_manuscript(author)
    assert client.post(f'/api/author/manuscripts/{m.id}/matches/run/').status_code == 201
    sims = similarity or {'ready_strong': 0.72, 'ready_moderate': 0.5, 'edit': 0.65, 'section': 0.7,
                          'wrong_type': 0.8, 'off_topic': 0.2}
    for key, value in sims.items():
        VenueMatch.objects.filter(manuscript=m, venue=vs[key]).update(topic_similarity=value)
    return m, vs


def build(client, m, **body):
    return client.post(f'/api/author/manuscripts/{m.id}/plan/', data=json.dumps(body), content_type='application/json')


def act(client, plan, position, action, **body):
    return client.post(f'/api/author/plans/{plan["id"]}/positions/{position["id"]}/{action}/',
                       data=json.dumps(body), content_type='application/json')


def new_version(client, m, data=b'# Revised\n\nPractitioner implications\n\nMore.\n'):
    r = client.post(f'/api/author/manuscripts/{m.id}/versions/',
                    {'manuscript': SimpleUploadedFile('v2.md', data), 'change_note': 'Revised after review'})
    assert r.status_code == 201, r.json()
    ReadinessAssessment.objects.create(manuscript=m, status='completed',
                                       summary={'ready_for_matching': True, 'word_count': 450})
    return r.json()['version']['number']


# ---------------------------------------------------------------------------
# Gap classes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('violation, cls, qty', [
    ({'field': 'word_count', 'operator': '>', 'value': 8000, 'actual': 8500}, 'edit', 500),
    ({'field': 'word_count', 'operator': '<', 'value': 3000, 'actual': 2000}, 'section', 1000),
    ({'field': 'reference_count', 'operator': '>', 'value': 50, 'actual': 70}, 'edit', 20),
    ({'field': 'required_sections', 'operator': 'missing_any', 'value': ['A', 'B'],
      'actual': {'required': ['A', 'B'], 'missing': ['A', 'B']}}, 'section', 2),
    ({'field': 'manuscript_type', 'operator': 'in', 'value': ['book'], 'actual': 'book'}, 'out', None),
    ({'field': 'disclosure', 'operator': 'empty', 'value': None, 'actual': ''}, 'edit', 1),
    ({'field': 'something_new', 'operator': '?', 'value': 1, 'actual': 2}, 'section', None),
])
def test_violations_are_classified(violation, cls, qty):
    item = classify_violation(violation)
    assert (item['class'], item['quantity']) == (cls, qty)
    assert item['message']


def test_effort_takes_the_hardest_gap():
    items = [{'class': 'edit', 'quantity': 100}, {'class': 'section', 'quantity': 1}, {'class': 'edit', 'quantity': 5}]
    assert effort_of(items) == ('section', 1, 1)
    assert effort_of([]) == ('ready', 0, 0)
    assert effort_of(items + [{'class': 'out', 'quantity': None}])[0] == 'out'


# ---------------------------------------------------------------------------
# Building and ordering
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_plan_orders_by_compliance_distance_then_fit():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    response = build(client, m)
    assert response.status_code == 201, response.json()
    plan = response.json()['plan']
    assert [p['venue']['name'] for p in plan['positions']] == [
        'Applied Practice Review', 'Bridge Journal', 'Concise Letters', 'Deep Practice']
    assert [p['effort_class'] for p in plan['positions']] == ['ready', 'ready', 'edit', 'section']
    assert plan['positions'][2]['changes'][0]['amount'] == 100  # 600 words against a 500 limit
    assert all(p['reason'].strip() for p in plan['positions'])
    assert plan['positions'][0]['reason'].startswith('First: no changes needed')
    assert 'Placed after' in plan['positions'][3]['reason']
    left_out = {item['venue']: item['reason'] for item in plan['not_included']}
    assert set(left_out) == {'Empirical Quarterly', 'Faraway Studies'}
    assert 'article type' in left_out['Empirical Quarterly'] and 'scope' in left_out['Faraway Studies']
    assert plan['built_on_version'] == 1 and plan['current_position_id'] == plan['positions'][0]['id']


@pytest.mark.django_db
def test_order_ignores_tier_and_anything_but_fit_and_changes():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    first = [p['venue']['name'] for p in build(client, m).json()['plan']['positions']]
    Venue.objects.filter(id=vs['ready_moderate'].id).update(trust_tier='claimed')
    Venue.objects.filter(id=vs['ready_strong'].id).update(trust_tier='verified_index')
    second = [p['venue']['name'] for p in build(client, m, replace=True).json()['plan']['positions']]
    assert first == second


@pytest.mark.django_db
def test_plan_length_cap(monkeypatch):
    monkeypatch.setenv('PLAN_MAX_POSITIONS', '2')
    client, author = author_client()
    m, _ = setup_plan(client, author)
    plan = build(client, m).json()['plan']
    assert len(plan['positions']) == 2
    assert any('holds 2 venues' in item['reason'] for item in plan['not_included'])


@pytest.mark.django_db
def test_plan_needs_readiness_and_matches_and_only_one_active():
    client, author = author_client()
    m = make_manuscript(author)
    assert build(client, m).json()['code'] == 'matches_needed'
    m2, _ = setup_plan(client, author)
    assert build(client, m2).status_code == 201
    again = build(client, m2)
    assert again.status_code == 409 and again.json()['code'] == 'plan_exists'
    assert build(client, m2, replace=True).status_code == 201
    assert SubmissionPlan.objects.filter(manuscript=m2, status='active').count() == 1


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_one_venue_at_a_time_and_in_order():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    plan = build(client, m).json()['plan']
    first, second = plan['positions'][0], plan['positions'][1]
    out_of_order = act(client, plan, second, 'start')
    assert out_of_order.status_code == 409 and out_of_order.json()['code'] == 'out_of_order'
    assert act(client, plan, first, 'start').status_code == 200
    assert act(client, plan, first, 'submitted').status_code == 200
    busy = act(client, plan, second, 'start')
    assert busy.status_code == 409 and busy.json()['code'] == 'one_at_a_time'


@pytest.mark.django_db
def test_a_decline_stops_the_plan_until_the_author_answers():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    plan = build(client, m).json()['plan']
    first, second = plan['positions'][0], plan['positions'][1]
    act(client, plan, first, 'start')
    act(client, plan, first, 'submitted')
    body = act(client, plan, first, 'outcome', outcome='declined').json()['plan']
    assert body['positions'][0]['state'] == 'awaiting_author' and body['waiting_for_author'] is True
    assert body['positions'][1]['state'] == 'queued'  # never auto-advanced
    assert act(client, plan, second, 'start').json()['code'] == 'one_at_a_time'

    no_reason = act(client, plan, first, 'answer', answer='unchanged', reason='short', confirm=True)
    assert no_reason.json()['code'] == 'needs_reason'
    unconfirmed = act(client, plan, first, 'answer', answer='unchanged',
                      reason='Desk reject on scope, no reviewer comments to act on.')
    assert unconfirmed.json()['code'] == 'needs_confirmation'
    no_version = act(client, plan, first, 'answer', answer='revised', version=1)
    assert no_version.json()['code'] == 'needs_revision'
    ok = act(client, plan, first, 'answer', answer='unchanged',
             reason='Desk reject on scope, no reviewer comments to act on.', confirm=True)
    assert ok.status_code == 200 and ok.json()['plan']['positions'][0]['state'] == 'closed'
    assert act(client, plan, second, 'start').status_code == 200


@pytest.mark.django_db
def test_revised_answer_rechecks_the_remaining_venues_against_the_new_version():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = build(client, m).json()['plan']
    first = plan['positions'][0]
    act(client, plan, first, 'start')
    act(client, plan, first, 'submitted')
    act(client, plan, first, 'outcome', outcome='declined')
    number = new_version(client, m)
    needs_matching = act(client, plan, first, 'answer', answer='revised', version=number)
    assert needs_matching.json()['code'] == 'matches_needed'
    assert client.post(f'/api/author/manuscripts/{m.id}/matches/run/').status_code == 201
    for key, value in {'ready_moderate': 0.5, 'edit': 0.65, 'section': 0.7}.items():
        VenueMatch.objects.filter(manuscript=m, version__number=number, venue=vs[key]).update(topic_similarity=value)
    body = act(client, plan, first, 'answer', answer='revised', version=number).json()['plan']
    remaining = [p for p in body['positions'] if p['state'] == 'queued']
    # Version 2 is 450 words and has the required section: both former gaps are gone.
    assert [p['effort_class'] for p in remaining] == ['ready', 'ready', 'ready']
    assert [p['venue']['name'] for p in remaining][0] == 'Deep Practice'  # strongest fit among ready venues
    assert all('re-checked against version 2' in p['reason'] for p in remaining)
    assert body['positions'][0]['revision_version'] == number


@pytest.mark.django_db
def test_revise_and_resubmit_needs_a_newer_version_then_acceptance_completes_the_plan():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    plan = build(client, m).json()['plan']
    first = plan['positions'][0]
    act(client, plan, first, 'start')
    act(client, plan, first, 'submitted')
    act(client, plan, first, 'outcome', outcome='revise_resubmit')
    assert act(client, plan, first, 'resubmit', version=1).json()['code'] == 'needs_revision'
    number = new_version(client, m)
    assert act(client, plan, first, 'resubmit', version=number).json()['plan']['positions'][0]['submitted_version'] == 2
    done = act(client, plan, first, 'outcome', outcome='accepted').json()['plan']
    assert done['status'] == 'completed' and done['current_position_id'] is None


@pytest.mark.django_db
def test_skip_needs_a_reason_and_stop_ends_the_plan():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    plan = build(client, m).json()['plan']
    first = plan['positions'][0]
    assert act(client, plan, first, 'skip', reason='').json()['code'] == 'needs_reason'
    assert act(client, plan, first, 'skip', reason='Editor is a co-author').json()['plan']['positions'][0]['state'] == 'skipped'
    stopped = client.post(f'/api/author/plans/{plan["id"]}/stop/', data='{}', content_type='application/json')
    assert stopped.json()['plan']['status'] == 'stopped'
    assert act(client, plan, plan['positions'][1], 'start').json()['code'] == 'plan_closed'


@pytest.mark.django_db
def test_a_flexee_venue_keeps_the_plan_in_step_and_a_rejection_stops_it():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = build(client, m).json()['plan']
    first = plan['positions'][0]
    act(client, plan, first, 'start')
    submission = VenueSubmission.objects.create(manuscript=m, venue=vs['ready_strong'], status='submitted',
                                                submitted_at=timezone.now())
    plans.sync_from_submission(submission)
    position = PlanPosition.objects.get(id=first['id'])
    assert position.state == 'submitted' and position.venue_submission_id == submission.id
    submission.status = 'under_review'
    plans.sync_from_submission(submission)
    submission.status = 'rejected'
    plans.sync_from_submission(submission)
    position.refresh_from_db()
    assert position.state == 'awaiting_author' and position.outcome_reported_by == 'venue'
    assert PlanPosition.objects.get(plan_id=plan['id'], order=2).state == 'queued'


@pytest.mark.django_db
def test_an_active_plan_freezes_the_version_it_was_built_on():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    build(client, m)
    edit = client.post(f'/api/author/manuscripts/{m.id}/update/', {
        'title': 'x', 'author': 'Priya', 'manuscript_type': 'practitioner_article', 'disclosure': 'None',
        'attestation': 'true'})
    assert edit.status_code == 409 and edit.json()['code'] == 'manuscript_locked'


@pytest.mark.django_db
def test_other_authors_cannot_see_or_change_a_plan():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    plan = build(client, m).json()['plan']
    other, _ = author_client('other@example.com')
    assert other.get(f'/api/author/manuscripts/{m.id}/plan/').status_code in (401, 403)
    assert act(other, plan, plan['positions'][0], 'start').status_code in (401, 403)


@pytest.mark.django_db
def test_no_odds_or_rating_language_anywhere_in_the_plan():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    plan = build(client, m).json()['plan']
    first = plan['positions'][0]
    act(client, plan, first, 'start')
    act(client, plan, first, 'submitted')
    act(client, plan, first, 'outcome', outcome='declined')
    payload = client.get(f'/api/author/manuscripts/{m.id}/plan/').json()
    for position in payload['plan']['positions']:
        assert not BANNED.search(position['reason']), position['reason']
        assert not BANNED.search(position['effort']) and not BANNED.search(position['scope'])
        for change in position['changes']:
            assert not BANNED.search(change['message'] or '')
    assert not BANNED.search(payload['plan']['how_ordered'])
    text = json.dumps(payload).lower()
    for key in ('probability', 'likelihood', 'odds', 'score'):
        assert f'"{key}' not in text
