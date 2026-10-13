"""Submission plan spec (8 October), sections 4 to 9.

Order: eligible with no changes, eligible with changes, needs changes; inside a band by compliance
distance, then fit. Never ratings, metrics or acceptance odds. One venue at a time. A decline never
advances the plan. Every step has a rationale. The author's reorder survives regeneration."""
import json
import re

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from review import submission_plan as plans
from review.gap_classes import COMPLIANCE_WEIGHTS, classify_violation, compliance_distance, effort_of
from review.models import (ManuscriptVersion, ReadinessAssessment, ReviewJob, SubmissionPlan, SubmissionPlanStep,
                           Venue, VenueMatch, VenueSubmission)
from review.tasks import run_submission_plan_task
from tests.test_author_dashboard_actions import author_client, env, make_manuscript, make_venue  # noqa: F401

BANNED = re.compile(r'probab|likel|chance|odds|%|top tier|\bA\*|ranked|rating', re.I)


def venue(name, slug, *, rules=None, types=('Practitioner article',), items=None, tier=Venue.TIER_VERIFIED_INDEX):
    """Verified from official pages by default: the author sends outside Flexee and marks it sent.
    A claimed venue's editors work on Flexee, so its step can only come from a real packet."""
    v = make_venue(name, slug, types=types)
    if v.trust_tier != tier:
        v.trust_tier = tier
        v.save(update_fields=['trust_tier'])
    config = v.agent_configs.get()
    if rules:
        config.structured_desk_rejection_rules = rules
    if items:
        config.required_submission_items = items
    config.save()
    return v


def setup_plan(client, author, similarity=None):
    """Six venues: two ready (strong/moderate fit), one light edit, one new section, one wrong type, one off-topic."""
    vs = {
        'ready_strong': venue('Applied Practice Review', 'apr'),
        'ready_moderate': venue('Bridge Journal', 'bridge', items=[{'label': 'Cover letter', 'required': True}]),
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


def run_job(response):
    assert response.status_code == 202, response.json()
    job_id = response.json()['job_id']
    job = ReviewJob.objects.get(id=job_id)
    run_submission_plan_task(job.id, job.reference_id)
    job.refresh_from_db()
    return job


def generate(client, m):
    """POST generate (202), run the queued job, return the plan payload."""
    job = run_job(client.post(f'/api/author/manuscripts/{m.id}/plan/generate/'))
    assert job.status == 'completed', job.error_message
    return client.get(f'/api/author/manuscripts/{m.id}/plan/').json()['plan']


def post(client, path, **body):
    return client.post(path, data=json.dumps(body), content_type='application/json')


def activate(client, plan):
    r = post(client, f'/api/author/plans/{plan["id"]}/activate/')
    assert r.status_code == 200, r.json()
    return r.json()['plan']


def step(client, plan, s, action, **body):
    return post(client, f'/api/author/plans/{plan["id"]}/steps/{s["id"]}/{action}/', **body)


def names(plan):
    return [s['venue']['name'] for s in plan['steps']]


def new_version(client, m, data=b'# Revised\n\nPractitioner implications\n\nMore.\n'):
    r = client.post(f'/api/author/manuscripts/{m.id}/versions/',
                    {'manuscript': SimpleUploadedFile('v2.md', data), 'change_note': 'Revised after review'})
    assert r.status_code == 201, r.json()
    ReadinessAssessment.objects.create(manuscript=m, status='completed',
                                       summary={'ready_for_matching': True, 'word_count': 450})
    return r.json()['version']['number']


def decline_first(client, m):
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    step(client, plan, first, 'submitted')
    body = step(client, plan, first, 'outcome', outcome='declined', note='Out of scope for us.').json()['plan']
    return plan, first, body


# ---------------------------------------------------------------------------
# Compliance distance
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


def test_compliance_distance_is_a_weighted_count_from_one_constant():
    light, medium, heavy = COMPLIANCE_WEIGHTS['light'], COMPLIANCE_WEIGHTS['medium'], COMPLIANCE_WEIGHTS['heavy']
    assert light < medium < heavy
    items = [{'class': 'edit'}, {'class': 'edit'}, {'class': 'section'}, {'class': 'prepare'}]
    assert compliance_distance(items) == 2 * light + medium  # preparation items are not changes
    assert compliance_distance([{'class': 'study'}]) == heavy
    assert effort_of([]) == ('ready', 0, 0)


# ---------------------------------------------------------------------------
# Generation and ordering (spec 5)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_generation_is_a_queued_job_and_produces_a_draft():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    response = client.post(f'/api/author/manuscripts/{m.id}/plan/generate/')
    assert response.status_code == 202 and response.json()['status'] == 'queued'
    assert SubmissionPlan.objects.filter(manuscript=m).count() == 0  # nothing until the job runs
    run_job_payload = ReviewJob.objects.get(id=response.json()['job_id'])
    run_submission_plan_task(run_job_payload.id, run_job_payload.reference_id)
    status = client.get(f'/api/author/jobs/{run_job_payload.id}/').json()
    assert status['status'] == 'completed'
    plan = client.get(f'/api/author/manuscripts/{m.id}/plan/').json()['plan']
    assert plan['status'] == 'draft' and plan['current_step_id'] is None
    assert {s['state'] for s in plan['steps']} == {'queued'}
    assert plan['generated_by'].startswith('plan-v2') and plan['manuscript_version'] == 1


@pytest.mark.django_db
def test_bands_come_first_regardless_of_fit_score():
    """Spec 9: eligible-no-changes ahead of eligible-with-changes ahead of needs-changes, whatever the fit."""
    client, author = author_client()
    # The light-edit and new-section venues get the closest scope; the ready ones the weakest.
    m, _ = setup_plan(client, author, similarity={'ready_strong': 0.45, 'ready_moderate': 0.41, 'edit': 0.95,
                                                   'section': 0.99, 'wrong_type': 0.8, 'off_topic': 0.2})
    plan = generate(client, m)
    assert names(plan) == ['Applied Practice Review', 'Bridge Journal', 'Concise Letters', 'Deep Practice']
    assert [s['band'] for s in plan['steps']] == [1, 1, 3, 3]
    # Inside the needs-changes band, the light edit comes before the new section despite a lower fit.
    assert plan['steps'][2]['required_changes'][0]['kind'] == 'light'
    assert plan['steps'][3]['required_changes'][0]['kind'] == 'medium'


@pytest.mark.django_db
def test_eligible_with_changes_sits_between_the_other_bands():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    VenueMatch.objects.filter(manuscript=m, venue=vs['ready_moderate']).update(
        gaps=['Tighten the abstract to the venue\'s style.'])
    plan = generate(client, m)
    assert names(plan) == ['Applied Practice Review', 'Bridge Journal', 'Concise Letters', 'Deep Practice']
    assert [s['band'] for s in plan['steps']] == [1, 2, 3, 3]
    assert plan['steps'][1]['band_label'] == 'Eligible, with changes'


@pytest.mark.django_db
def test_every_step_has_a_rationale_and_required_changes_include_venue_items():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    plan = generate(client, m)
    assert all(s['rationale'].strip() for s in plan['steps'])
    assert plan['steps'][0]['rationale'].startswith('No changes needed')
    assert 'Placed after' in plan['steps'][2]['rationale']
    bridge = plan['steps'][1]
    assert bridge['band'] == 1  # a cover letter to prepare is not a change to the manuscript
    assert [c['kind'] for c in bridge['required_changes']] == ['prepare']
    assert 'Cover letter' in bridge['required_changes'][0]['message']
    with pytest.raises(ValueError):
        SubmissionPlanStep(plan_id=plan['id'], order=99, venue_id=bridge['venue']['id'], rationale=' ',
                           effort_class='ready').save()


@pytest.mark.django_db
def test_ineligible_venues_are_listed_separately_with_the_reason():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    plan = generate(client, m)
    left_out = {item['venue']: item['reason'] for item in plan['not_included']}
    assert set(left_out) == {'Empirical Quarterly', 'Faraway Studies'}
    assert 'article type' in left_out['Empirical Quarterly'] and 'scope' in left_out['Faraway Studies']


@pytest.mark.django_db
def test_never_an_excluded_listed_tier_or_non_rule_tier_venue():
    """Spec 9: a plan never includes an excluded venue, a listed-tier venue, or a venue outside RULE_TIERS."""
    client, author = author_client()
    m, vs = setup_plan(client, author)
    Venue.objects.filter(id=vs['ready_strong'].id).update(excluded=True)
    Venue.objects.filter(id=vs['ready_moderate'].id).update(trust_tier=Venue.TIER_LISTED)
    Venue.objects.filter(id=vs['edit'].id).update(trust_tier='something_else')
    plan = generate(client, m)
    assert names(plan) == ['Deep Practice']
    assert all(Venue.objects.get(id=s['venue']['id']).trust_tier in Venue.RULE_TIERS for s in plan['steps'])


@pytest.mark.django_db
def test_plan_length_cap_defaults_to_eight(monkeypatch):
    assert plans.settings()['max_steps'] == 8
    monkeypatch.setenv('PLAN_MAX_STEPS', '2')
    client, author = author_client()
    m, _ = setup_plan(client, author)
    plan = generate(client, m)
    assert len(plan['steps']) == 2
    assert any('holds 2 venues' in item['reason'] for item in plan['not_included'])


@pytest.mark.django_db
def test_generation_needs_readiness_and_matches_and_reports_why():
    client, author = author_client()
    m = make_manuscript(author)
    r = client.post(f'/api/author/manuscripts/{m.id}/plan/generate/')
    assert r.status_code == 409 and r.json()['code'] == 'matches_needed'


@pytest.mark.django_db
def test_regenerating_replaces_a_plan_with_nothing_sent():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    first = generate(client, m)
    second = generate(client, m)
    assert first['id'] != second['id'] and second['previous_plan_id'] == first['id']
    assert SubmissionPlan.objects.get(id=first['id']).status == 'abandoned'
    assert SubmissionPlan.objects.filter(manuscript=m, status__in=SubmissionPlan.LIVE_STATUSES).count() == 1


# ---------------------------------------------------------------------------
# State machine (spec 6)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_activate_makes_step_one_current_and_one_venue_at_a_time():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    draft = generate(client, m)
    assert step(client, draft, draft['steps'][0], 'submitted').json()['code'] == 'plan_closed'  # still a draft
    plan = activate(client, draft)
    assert plan['status'] == 'active' and plan['position'] == 1
    assert plan['current_step_id'] == plan['steps'][0]['id'] and plan['steps'][0]['state'] == 'current'
    second = plan['steps'][1]
    assert step(client, plan, second, 'submitted').json()['code'] == 'bad_state'
    assert step(client, plan, plan['steps'][0], 'submitted').json()['plan']['steps'][0]['state'] == 'submitted'
    busy = client.post(f'/api/author/manuscripts/{m.id}/plan/generate/')
    assert busy.status_code == 409 and busy.json()['code'] == 'plan_live'


@pytest.mark.django_db
def test_a_declined_step_does_not_auto_advance():
    """Spec 9: the plan waits for the revision answer."""
    client, author = author_client()
    m, _ = setup_plan(client, author)
    plan, first, body = decline_first(client, m)
    assert body['status'] == 'paused' and body['waiting_for_author'] is True
    assert body['steps'][0]['state'] == 'declined' and body['steps'][0]['outcome_note'] == 'Out of scope for us.'
    assert body['steps'][1]['state'] == 'queued' and body['current_step_id'] is None  # never auto-advanced
    assert step(client, plan, body['steps'][1], 'submitted').json()['code'] == 'plan_closed'
    regen = client.post(f'/api/author/manuscripts/{m.id}/plan/generate/')
    assert regen.status_code == 409 and regen.json()['code'] == 'answer_needed'
    no_version = post(client, f'/api/author/plans/{plan["id"]}/revision/', answer='revised', version=1)
    assert no_version.json()['code'] == 'needs_revision'


@pytest.mark.django_db
def test_not_revised_makes_the_next_step_current_unchanged():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    plan, _first, _body = decline_first(client, m)
    no_reason = post(client, f'/api/author/plans/{plan["id"]}/revision/', answer='unchanged')
    assert no_reason.status_code == 400 and no_reason.json()['code'] == 'needs_reason'
    body = post(client, f'/api/author/plans/{plan["id"]}/revision/', answer='unchanged',
                note='Desk reject on scope, no reviewer comments.').json()['plan']
    assert body['status'] == 'active' and body['steps'][1]['state'] == 'current'
    assert body['steps'][0]['revision_answer'] == 'unchanged'


@pytest.mark.django_db
def test_a_new_version_regenerates_the_plan_without_the_declined_venue():
    """Spec 9: a new ManuscriptVersion triggers a regenerated plan that excludes venues that declined."""
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan, first, _ = decline_first(client, m)
    number = new_version(client, m)
    needs_matching = post(client, f'/api/author/plans/{plan["id"]}/revision/', answer='revised', version=number)
    assert needs_matching.json()['code'] == 'matches_needed'
    assert client.post(f'/api/author/manuscripts/{m.id}/matches/run/').status_code == 201
    for key, value in {'ready_strong': 0.72, 'ready_moderate': 0.5, 'edit': 0.65, 'section': 0.7}.items():
        VenueMatch.objects.filter(manuscript=m, version__number=number, venue=vs[key]).update(topic_similarity=value)
    job = run_job(post(client, f'/api/author/plans/{plan["id"]}/revision/', answer='revised', version=number))
    assert job.status == 'completed', job.error_message
    new = client.get(f'/api/author/manuscripts/{m.id}/plan/').json()['plan']
    assert new['id'] != plan['id'] and new['manuscript_version'] == number and new['status'] == 'draft'
    assert first['venue']['name'] not in names(new)
    assert any(i['venue'] == first['venue']['name'] and 'declined' in i['reason'] for i in new['not_included'])
    # Version 2 is 450 words and has the required section: both former gaps are gone.
    assert {s['band'] for s in new['steps']} == {1}
    old = SubmissionPlan.objects.get(id=plan['id'])
    assert old.status == 'abandoned' and new['previous_plan_id'] == plan['id']
    assert ManuscriptVersion.objects.get(manuscript=m, number=number).supersedes.number == 1


@pytest.mark.django_db
def test_an_author_reorder_survives_regeneration():
    """Spec 9."""
    client, author = author_client()
    m, _ = setup_plan(client, author)
    plan = generate(client, m)
    wanted = [plan['steps'][i]['id'] for i in (2, 0, 3, 1)]
    r = client.patch(f'/api/author/plans/{plan["id"]}/reorder/', data=json.dumps({'steps': wanted}),
                     content_type='application/json')
    assert r.status_code == 200, r.json()
    reordered = r.json()['plan']
    assert names(reordered) == ['Concise Letters', 'Applied Practice Review', 'Deep Practice', 'Bridge Journal']
    assert any(s['author_moved'] for s in reordered['steps'])
    again = generate(client, m)
    assert names(again) == names(reordered)
    assert 'You set this order' in again['steps'][0]['rationale']


@pytest.mark.django_db
def test_reorder_of_an_active_plan_moves_the_current_step():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    ids = [s['id'] for s in plan['steps']]
    body = client.patch(f'/api/author/plans/{plan["id"]}/reorder/', data=json.dumps({'steps': ids[1:] + ids[:1]}),
                        content_type='application/json').json()['plan']
    assert body['steps'][0]['venue']['name'] == 'Bridge Journal' and body['steps'][0]['state'] == 'current'
    bad = client.patch(f'/api/author/plans/{plan["id"]}/reorder/', data=json.dumps({'steps': ids[:2]}),
                       content_type='application/json')
    assert bad.status_code == 400 and bad.json()['code'] == 'bad_order'


@pytest.mark.django_db
def test_skip_and_remove_are_kept_out_of_the_regenerated_plan():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    skipped = step(client, plan, plan['steps'][0], 'skip', note='Editor is a co-author').json()['plan']
    assert skipped['steps'][0]['state'] == 'skipped' and skipped['steps'][1]['state'] == 'current'
    removed = step(client, plan, plan['steps'][3], 'remove').json()['plan']
    assert 'Deep Practice' not in names(removed)
    assert any(i['venue'] == 'Deep Practice' and i.get('removed_by_author') for i in removed['not_included'])
    again = generate(client, m)
    assert 'Applied Practice Review' not in names(again) and 'Deep Practice' not in names(again)


@pytest.mark.django_db
def test_acceptance_completes_the_plan():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    step(client, plan, first, 'submitted')
    done = step(client, plan, first, 'outcome', outcome='accepted').json()['plan']
    assert done['status'] == 'completed' and done['current_step_id'] is None


@pytest.mark.django_db
def test_abandon_ends_the_plan():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    ended = post(client, f'/api/author/plans/{plan["id"]}/abandon/').json()['plan']
    assert ended['status'] == 'abandoned'
    assert step(client, plan, plan['steps'][0], 'submitted').json()['code'] == 'plan_closed'


@pytest.mark.django_db
def test_a_flexee_venue_keeps_the_plan_in_step_and_a_rejection_pauses_it():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    submission = VenueSubmission.objects.create(manuscript=m, venue=vs['ready_strong'], status='submitted',
                                                submitted_at=timezone.now())
    plans.sync_from_submission(submission)
    s = SubmissionPlanStep.objects.get(id=first['id'])
    assert s.state == 'submitted' and s.venue_submission_id == submission.id
    submission.status = 'under_review'
    plans.sync_from_submission(submission)
    submission.status = 'rejected'
    plans.sync_from_submission(submission)
    s.refresh_from_db()
    assert s.state == 'declined' and s.outcome_reported_by == 'venue'
    assert SubmissionPlan.objects.get(id=plan['id']).status == 'paused'
    assert SubmissionPlanStep.objects.get(plan_id=plan['id'], order=2).state == 'queued'


@pytest.mark.django_db
def test_a_live_plan_freezes_the_version_it_was_built_on():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    generate(client, m)
    edit = client.post(f'/api/author/manuscripts/{m.id}/update/', {
        'title': 'x', 'author': 'Priya', 'manuscript_type': 'practitioner_article', 'disclosure': 'None',
        'attestation': 'true'})
    assert edit.status_code == 409 and edit.json()['code'] == 'manuscript_locked'


@pytest.mark.django_db
def test_other_authors_cannot_see_or_change_a_plan():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    plan = generate(client, m)
    other, _ = author_client('other@example.com')
    assert other.get(f'/api/author/manuscripts/{m.id}/plan/').status_code in (401, 403)
    assert other.post(f'/api/author/manuscripts/{m.id}/plan/generate/').status_code in (401, 403)
    assert post(other, f'/api/author/plans/{plan["id"]}/activate/').status_code in (401, 403)
    assert step(other, plan, plan['steps'][0], 'skip').status_code in (401, 403)


@pytest.mark.django_db
def test_no_odds_or_rating_language_anywhere_in_the_plan():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    decline_first(client, m)
    payload = client.get(f'/api/author/manuscripts/{m.id}/plan/').json()
    for s in payload['plan']['steps']:
        for text in (s['rationale'], s['effort'], s['scope'], s['band_label']):
            assert not BANNED.search(text), text
        for change in s['required_changes']:
            assert not BANNED.search(change['message'] or '')
    assert not BANNED.search(payload['plan']['how_ordered'])
    text = json.dumps(payload).lower()
    for key in ('probability', 'likelihood', 'odds', 'score'):
        assert f'"{key}' not in text


# --- 12 Oct audit: order inside a band follows the spec key; reasons are required ------------------

from types import SimpleNamespace  # noqa: E402

from review.submission_plan import order_key, rationale_for  # noqa: E402


def _entry(name, *, band=2, cls='edit', fit='strong', count=1, quantity=0, sim=0.8, distance=1, works=0):
    venue_obj = SimpleNamespace(name=name, works_count=works)
    return {'venue': venue_obj, 'match': SimpleNamespace(shortlist_rank=None), 'band': band, 'class': cls,
            'fit_band': fit, 'count': count, 'quantity': quantity, 'similarity': sim, 'distance': distance,
            'items': []}


def _order(*entries):
    return [e['venue'].name for e in sorted(entries, key=order_key)]


def test_inside_a_band_edits_come_before_a_new_section_whatever_the_count():
    four_edits = _entry('Four edits', cls='edit', count=4, distance=4)
    one_section = _entry('One section', cls='section', count=1, distance=3)
    assert _order(one_section, four_edits) == ['Four edits', 'One section']


def test_bands_still_come_first():
    needs_changes_edit = _entry('Band three', band=3, cls='edit')
    eligible_section = _entry('Band two', band=2, cls='section')
    assert _order(needs_changes_edit, eligible_section) == ['Band two', 'Band three']


def test_strong_fit_before_moderate_within_a_class_then_amount_then_similarity():
    moderate = _entry('Moderate', fit='moderate', sim=0.9)
    strong_more = _entry('Strong, more edits', count=2, sim=0.7)
    strong_less = _entry('Strong, one edit', count=1, sim=0.6)
    strong_closer = _entry('Strong, one edit, closer', count=1, sim=0.75)
    assert _order(moderate, strong_more, strong_less, strong_closer) == [
        'Strong, one edit, closer', 'Strong, one edit', 'Strong, more edits', 'Moderate']


def test_ties_are_broken_by_name_and_journal_metrics_do_not_matter():
    a = _entry('Alpha', works=10)
    b = _entry('Beta', works=100000)
    assert _order(b, a) == ['Alpha', 'Beta']


def test_rationale_names_the_kind_of_change():
    first, second = _entry('A', cls='edit'), _entry('B', cls='section')
    assert 'a new section rather than edits' in rationale_for(2, second, first)


@pytest.mark.django_db
def test_skip_needs_a_reason():
    client, author = author_client()
    m, _ = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    response = step(client, plan, plan['steps'][0], 'skip')
    assert response.status_code == 400 and response.json()['code'] == 'needs_reason'
