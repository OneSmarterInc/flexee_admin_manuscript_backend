"""12 October audit, submission plan, medium items 13, 14, 15 and 17.

13 One venue at a time also for a direct Flexee submission.
14 A Flexee venue records its own decision; the author withdraws instead of overruling the editor.
15 A plan made for an older version cannot be started or used to send.
17 A failed regeneration after "revised" does not leave the plan paused for ever.
"""
from unittest import mock

import pytest
from django.utils import timezone

from review import submission_plan as plans
from review.models import ReviewJob, SubmissionPlan, SubmissionPlanStep, VenueSubmission
from review.tasks import run_submission_plan_task
from tests.test_author_dashboard_actions import author_client, env  # noqa: F401
from tests.test_submission_plan import activate, decline_first, generate, new_version, post, setup_plan, step


def _packet(m, venue, status='packet_ready'):
    return VenueSubmission.objects.create(manuscript=m, venue=venue, status=status, version=m.current_version,
                                          submitted_at=timezone.now() if status != 'packet_ready' else None)


# ---------------------------------------------------------------------------
# Item 13
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_no_direct_flexee_submission_while_a_planned_venue_is_deciding():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    assert step(client, plan, first, 'submitted').status_code == 200   # sent outside Flexee
    other = next(v for k, v in vs.items() if v.name != first['venue']['name'] and k != 'off_topic')
    packet = _packet(m, other)
    response = client.post(f'/api/author/venue-submissions/{packet.id}/submit/')
    assert response.status_code == 409 and response.json()['code'] == 'one_venue_at_a_time'
    assert first['venue']['name'] in response.json()['detail']
    packet.refresh_from_db()
    assert packet.status == 'packet_ready'


@pytest.mark.django_db
def test_no_second_flexee_submission_while_one_is_under_review():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    _packet(m, vs['ready_strong'], status='under_review')
    packet = _packet(m, vs['ready_moderate'])
    response = client.post(f'/api/author/venue-submissions/{packet.id}/submit/')
    assert response.status_code == 409 and response.json()['code'] == 'one_venue_at_a_time'


# ---------------------------------------------------------------------------
# Item 14
# ---------------------------------------------------------------------------

def _flexee_step(client, author):
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    venue = next(v for v in vs.values() if v.name == first['venue']['name'])
    submission = _packet(m, venue, status='submitted')
    plans.sync_from_submission(submission)
    assert SubmissionPlanStep.objects.get(id=first['id']).state == 'submitted'
    return m, plan, first, submission


@pytest.mark.django_db
def test_the_author_cannot_overrule_a_flexee_editor():
    client, author = author_client()
    _m, plan, first, _submission = _flexee_step(client, author)
    response = step(client, plan, first, 'outcome', outcome='accepted')
    assert response.status_code == 409 and response.json()['code'] == 'venue_decides'
    assert SubmissionPlanStep.objects.get(id=first['id']).state == 'submitted'


@pytest.mark.django_db
def test_the_editor_decision_still_reaches_the_plan():
    client, author = author_client()
    _m, plan, first, submission = _flexee_step(client, author)
    submission.status = 'accepted'
    submission.save(update_fields=['status'])
    plans.sync_from_submission(submission)
    s = SubmissionPlanStep.objects.get(id=first['id'])
    assert s.state == 'accepted' and s.outcome_reported_by == 'venue'


@pytest.mark.django_db
def test_the_author_can_withdraw_a_flexee_submission_and_the_plan_follows():
    client, author = author_client()
    _m, plan, first, submission = _flexee_step(client, author)
    response = post(client, f'/api/author/venue-submissions/{submission.id}/withdraw/', reason='Too slow')
    assert response.status_code == 200 and response.json()['submission']['status'] == 'withdrawn'
    assert SubmissionPlanStep.objects.get(id=first['id']).state == 'withdrawn'
    assert SubmissionPlan.objects.get(id=plan['id']).status == 'paused'   # asks: did you revise?
    # A decided submission cannot be withdrawn.
    again = post(client, f'/api/author/venue-submissions/{submission.id}/withdraw/')
    assert again.status_code == 409 and again.json()['code'] == 'not_live'


@pytest.mark.django_db
def test_only_the_manuscripts_author_can_withdraw():
    client, author = author_client()
    _m, _plan, _first, submission = _flexee_step(client, author)
    other, _ = author_client(email='someone-else@example.com')
    assert post(other, f'/api/author/venue-submissions/{submission.id}/withdraw/').status_code in (401, 403, 404)
    submission.refresh_from_db()
    assert submission.status == 'submitted'


# ---------------------------------------------------------------------------
# Item 15
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_plan_for_an_older_version_cannot_be_started():
    client, author = author_client()
    m, _vs = setup_plan(client, author)
    plan = generate(client, m)
    new_version(client, m)
    response = post(client, f'/api/author/plans/{plan["id"]}/activate/')
    assert response.status_code == 409 and response.json()['code'] == 'stale_plan'
    assert SubmissionPlan.objects.get(id=plan['id']).status == 'draft'


# ---------------------------------------------------------------------------
# Item 17
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_failed_regeneration_after_revised_asks_again():
    client, author = author_client()
    m, _vs = setup_plan(client, author)
    plan, first, _body = decline_first(client, m)
    number = new_version(client, m)
    assert client.post(f'/api/author/manuscripts/{m.id}/matches/run/').status_code == 201
    response = post(client, f'/api/author/plans/{plan["id"]}/revision/', answer='revised', version=number)
    assert response.status_code == 202, response.json()
    job = ReviewJob.objects.get(id=response.json()['job_id'])
    with mock.patch.object(plans, 'generate_plan', side_effect=RuntimeError('model down')):
        run_submission_plan_task(job.id, job.reference_id)
    job.refresh_from_db()
    assert job.status == 'failed'
    s = SubmissionPlanStep.objects.get(id=first['id'])
    assert s.revision_answer == '' and s.revision_version_id is None
    payload = client.get(f'/api/author/manuscripts/{m.id}/plan/').json()['plan']
    assert payload['status'] == 'paused' and payload['waiting_for_author'] is True
    assert any(e['action'] == 'revision_reopened' for e in payload['events'])
