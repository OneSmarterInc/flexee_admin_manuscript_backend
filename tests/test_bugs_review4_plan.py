"""Review 4a: the author submission flow and the submission plan. Each test asserts the CORRECT behaviour
and fails on commit aad7783 because of the bug it documents."""
import json
from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from review import submission_plan as plans
from review.models import (Manuscript, ReadinessAssessment, SubmissionPlan, SubmissionPlanStep, Venue, VenueMatch,
                           VenueSubmission)
from review.tasks import sweep_retention_task
from tests.test_audit_plan_medium import _packet
from tests.test_author_dashboard_actions import author_client, env  # noqa: F401
from tests.test_bugs_high import _claim_all, _editor, _venue_of
from tests.test_submission_plan import activate, generate, names, new_version, post, setup_plan, step


def _decide(ec, sub, decision, note='editor note'):
    if isinstance(ec, Venue):   # one editor per decision, each with its own address
        _decide.n += 1
        ec = _editor(ec, email=f'editor{_decide.n}@journal.org')
    return ec.post(f'/api/admin/venue-submissions/{sub.id}/decision/',
                   data=json.dumps({'decision': decision, 'note': note}), content_type='application/json')


_decide.n = 0


def _claim(venue):
    venue.trust_tier = Venue.TIER_CLAIMED
    venue.save(update_fields=['trust_tier'])
    return venue


def _rematch(client, m, vs, number):
    """Readiness is recorded by new_version(); run matching for the new version and give it similarities."""
    assert client.post(f'/api/author/manuscripts/{m.id}/matches/run/').status_code == 201
    for key, value in {'ready_strong': 0.72, 'ready_moderate': 0.5, 'edit': 0.65, 'section': 0.7}.items():
        VenueMatch.objects.filter(manuscript=m, version__number=number, venue=vs[key]).update(topic_similarity=value)


# P1: the retention sweep purges a manuscript whose plan is still live -------------------------------------

@pytest.mark.django_db
def test_p1_retention_sweep_keeps_a_manuscript_whose_plan_is_still_live(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first, second = plan['steps'][0], plan['steps'][1]
    venue = _claim(_venue_of(vs, first))
    config = venue.agent_configs.get()
    config.retention_days = 30
    config.save()
    packet = VenueSubmission.objects.create(manuscript=m, venue=venue, venue_config=config, status='packet_ready',
                                            version=m.current_version)
    assert client.post(f'/api/author/venue-submissions/{packet.id}/submit/').status_code == 200
    assert _decide(venue, packet, 'rejected').status_code == 200
    assert SubmissionPlan.objects.get(id=plan['id']).status == 'paused'
    assert post(client, f'/api/author/plans/{plan["id"]}/revision/', answer='unchanged',
                note='Desk reject on scope.').status_code == 200
    assert step(client, plan, second, 'submitted').status_code == 200   # now with an outside venue
    # Thirty days pass: the rejecting venue's copy expires while the plan is still being worked.
    VenueSubmission.objects.filter(id=packet.id).update(retention_expires_at=timezone.now() - timedelta(days=1))
    sweep_retention_task()
    m.refresh_from_db()
    assert SubmissionPlan.objects.get(id=plan['id']).status == 'active'
    assert m.content_purged_at is None, 'the manuscript was purged while its plan is active and a venue is deciding'
    assert m.manuscript_file and m.current_readiness().exists() and m.current_matches().exists()


# P2: acceptance by a venue outside the plan never completes (or frees) the plan --------------------------

@pytest.mark.django_db
def test_p2_acceptance_by_an_unplanned_venue_completes_an_active_plan():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    outside = _claim(vs['wrong_type'])           # not in the plan; the author sends there directly
    sub = _packet(m, outside)
    assert client.post(f'/api/author/venue-submissions/{sub.id}/submit/').status_code == 200
    assert _decide(outside, sub, 'accepted').status_code == 200
    body = client.get(f'/api/author/manuscripts/{m.id}/plan/').json()['plan']
    assert body['status'] == 'completed', body['status']
    assert body['current_step_id'] is None


@pytest.mark.django_db
def test_p2_acceptance_by_an_unplanned_venue_while_paused_does_not_leave_the_plan_waiting():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    assert step(client, plan, first, 'submitted').status_code == 200
    assert step(client, plan, first, 'outcome', outcome='declined', note='No').status_code == 200
    outside = _claim(vs['wrong_type'])
    sub = _packet(m, outside)
    assert client.post(f'/api/author/venue-submissions/{sub.id}/submit/').status_code == 200
    assert _decide(outside, sub, 'accepted').status_code == 200
    body = client.get(f'/api/author/manuscripts/{m.id}/plan/').json()['plan']
    # Every answer to "did you revise?" is refused (already_accepted), so the plan must not be left asking.
    assert body['status'] == 'completed' and body['waiting_for_author'] is False, body['status']


# P3: after a revised packet to the inviting venue is declined, the plan cannot move on ------------------

@pytest.mark.django_db
def test_p3_after_the_revised_resubmission_is_declined_the_author_can_move_on():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first, second = plan['steps'][0], plan['steps'][1]
    venue = _claim(_venue_of(vs, first))
    p1 = _packet(m, venue)
    assert client.post(f'/api/author/venue-submissions/{p1.id}/submit/').status_code == 200
    assert _decide(venue, p1, 'revision_requested', 'Shorten section 3').status_code == 200
    number = new_version(client, m)
    _rematch(client, m, vs, number)
    m.refresh_from_db()
    p2 = _packet(m, venue)                     # the revised version goes back to the inviting venue
    assert client.post(f'/api/author/venue-submissions/{p2.id}/submit/').status_code == 200
    assert _decide(venue, p2, 'rejected', 'Still not for us').status_code == 200
    assert SubmissionPlan.objects.get(id=plan['id']).status == 'paused'
    # The author did revise (version 2 is newer than the version the plan was made for).
    revised = post(client, f'/api/author/plans/{plan["id"]}/revision/', answer='revised', version=number)
    if revised.status_code != 202:
        # The other answer must then at least leave a plan the author can work: the next venue can be sent to.
        unchanged = post(client, f'/api/author/plans/{plan["id"]}/revision/', answer='unchanged', note='same text')
        assert unchanged.status_code == 200, unchanged.json()
        body = unchanged.json()['plan']
        current = next(s for s in body['steps'] if s['id'] == body['current_step_id'])
        sent = step(client, plan, current, 'submitted')
        assert sent.status_code == 200, (revised.json(), sent.json())


# P4: a packet sent to the venue the author already marked "sent" is never linked ----------------------------

@pytest.mark.django_db
def test_p4_a_packet_to_the_venue_marked_sent_by_hand_follows_the_step_and_is_closed_with_it():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first, second = plan['steps'][0], plan['steps'][1]
    venue = _venue_of(vs, first)                # outside Flexee: "I have sent it" is allowed ...
    assert step(client, plan, first, 'submitted').status_code == 200
    packet = _packet(m, venue)                  # ... and the author also sends the packet from the venue page
    assert client.post(f'/api/author/venue-submissions/{packet.id}/submit/').status_code == 200
    assert SubmissionPlanStep.objects.get(id=first['id']).venue_submission_id == packet.id
    assert step(client, plan, first, 'outcome', outcome='declined', note='They said no.').status_code == 200
    packet.refresh_from_db()
    assert packet.status not in plans.LIVE_SUBMISSION_STATUSES, 'the packet still holds the manuscript'
    assert post(client, f'/api/author/plans/{plan["id"]}/revision/', answer='unchanged', note='same').status_code == 200
    r = step(client, plan, second, 'submitted')
    assert r.status_code == 200, r.json()


# P5: the packet replaced by a revised resubmission counts as "withdrawn from this venue" ------------------

@pytest.mark.django_db
def test_p5_a_venue_deciding_on_the_revised_resubmission_is_not_listed_as_withdrawn():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    venue = _claim(vs['ready_strong'])
    p1 = _packet(m, venue)
    assert client.post(f'/api/author/venue-submissions/{p1.id}/submit/').status_code == 200
    assert _decide(venue, p1, 'revision_requested', 'Shorten section 3').status_code == 200
    number = new_version(client, m)
    _rematch(client, m, vs, number)
    m.refresh_from_db()
    p2 = _packet(m, venue)
    assert client.post(f'/api/author/venue-submissions/{p2.id}/submit/').status_code == 200
    p2.refresh_from_db()
    assert p2.status == 'submitted'
    plan = generate(client, m)
    reasons = {i['venue']: i['reason'] for i in plan['not_included']}
    assert venue.name in names(plan), reasons.get(venue.name)
    body = activate(client, plan)
    row = next(s for s in body['steps'] if s['venue']['name'] == venue.name)
    assert row['state'] == 'submitted' and row['venue_submission_id'] == str(p2.id)


# P6: the decision email never says which venue decided ------------------------------------------------------

@pytest.mark.django_db
def test_p6_the_acceptance_email_names_the_venue_that_accepted():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    venue = _claim(vs['ready_strong'])
    sub = _packet(m, venue)
    assert client.post(f'/api/author/venue-submissions/{sub.id}/submit/').status_code == 200
    mail.outbox.clear()
    assert _decide(venue, sub, 'accepted', 'Welcome aboard').status_code == 200
    message = mail.outbox[-1]
    assert venue.name in message.subject + message.body, message.body


@pytest.mark.django_db
def test_p1b_a_venue_copy_still_being_decided_is_not_purged_from_under_the_editor(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    venue = _claim(_venue_of(vs, plan['steps'][0]))
    config = venue.agent_configs.get()
    config.retention_days = 30
    config.save()
    packet = VenueSubmission.objects.create(manuscript=m, venue=venue, venue_config=config, status='packet_ready',
                                            version=m.current_version)
    assert client.post(f'/api/author/venue-submissions/{packet.id}/submit/').status_code == 200
    editor = _editor(venue, email='slow.editor@journal.org')
    assert editor.post(f'/api/admin/venue-submissions/{packet.id}/start-review/').status_code == 200
    VenueSubmission.objects.filter(id=packet.id).update(retention_expires_at=timezone.now() - timedelta(days=1))
    sweep_retention_task()
    packet.refresh_from_db()
    assert packet.status == 'under_review'
    assert SubmissionPlanStep.objects.get(id=plan['steps'][0]['id']).state == 'submitted'
    # The venue is still deciding on it: its copy, and the manuscript, must still be there.
    assert editor.get(f'/api/admin/venue-submissions/{packet.id}/download/').status_code == 200
    m.refresh_from_db()
    assert m.content_purged_at is None


# P7: choosing a venue for a purged manuscript is a server error -----------------------------------------------

@pytest.mark.django_db
def test_p7_choosing_a_section_rule_venue_for_a_purged_manuscript_is_not_a_server_error():
    client, author = author_client()
    client.raise_request_exception = False
    m, vs = setup_plan(client, author)
    Manuscript.objects.filter(id=m.id).update(content_purged_at=timezone.now(), manuscript_file='')
    m.venue_matches.all().delete()
    r = post(client, f'/api/author/manuscripts/{m.id}/submissions/', venue_id=str(vs['section'].id))
    assert r.status_code < 500, r.status_code


# P8: a new version drops the cover letter typed into a draft packet (files are kept, text answers are lost) ----

@pytest.mark.django_db
def test_p8_a_new_version_keeps_the_answers_typed_into_a_draft_packet():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    venue = vs['ready_moderate']                 # asks for a cover letter
    config = venue.agent_configs.get()
    config.required_submission_items = [{'key': 'cover_letter', 'label': 'Cover letter', 'type': 'text',
                                         'required': True}]
    config.save()
    draft = VenueSubmission.objects.create(manuscript=m, venue=venue, venue_config=config, status='packet_ready',
                                           version=m.current_version)
    r = post(client, f'/api/author/venue-submissions/{draft.id}/requirements/',
             responses={'cover_letter': 'Dear editors, this manuscript reports a field study.'})
    assert r.status_code == 200, r.json()
    new_version(client, m)
    draft.refresh_from_db()
    assert draft.status == 'draft' and draft.version.number == 2
    answers = (draft.packet or {}).get('requirement_responses') or {}
    assert answers.get('cover_letter') == 'Dear editors, this manuscript reports a field study.', draft.packet


# P9: the confirmation email promises an editorial decision from a venue that has no editor on Flexee ---------

@pytest.mark.django_db
def test_p9_the_confirmation_for_a_venue_outside_flexee_does_not_promise_an_editorial_decision():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    venue = vs['ready_strong']
    assert not plans.reviews_on_flexee(venue)    # verified from official pages: nobody decides on Flexee
    sub = _packet(m, venue)
    mail.outbox.clear()
    assert client.post(f'/api/author/venue-submissions/{sub.id}/submit/').status_code == 200
    body = mail.outbox[-1].body
    assert 'editorial team will review' not in body.lower() and 'editorial queue' not in body.lower(), body


# P10: a venue excluded after the plan was made still becomes the current step ---------------------------------

@pytest.mark.django_db
def test_p10_a_venue_excluded_after_planning_is_never_made_current():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = generate(client, m)
    first = plan['steps'][0]
    Venue.objects.filter(id=first['venue']['id']).update(excluded=True,
                                                         exclusion_reason={'reason': 'predatory publisher'})
    body = activate(client, plan)
    current = next((s for s in body['steps'] if s['id'] == body['current_step_id']), None)
    assert current is not None and current['venue']['id'] != first['venue']['id'], \
        'the author is told to send the manuscript to a venue Flexee excluded'
    # Nor can a packet be made for it, so the step cannot even be worked.
    r = post(client, f'/api/author/manuscripts/{m.id}/submissions/', venue_id=first['venue']['id'])
    assert r.status_code == 404


# P11: a plan whose last venue declined stays "active" with no venue to work --------------------------------------

@pytest.mark.django_db
def test_p11_a_plan_with_no_venue_left_is_not_left_active_with_nothing_current():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    steps = plan['steps']
    for s in steps[1:]:
        assert step(client, plan, s, 'skip', note='Not this year').status_code == 200
    assert step(client, plan, steps[0], 'submitted').status_code == 200
    assert step(client, plan, steps[0], 'outcome', outcome='declined', note='No').status_code == 200
    body = post(client, f'/api/author/plans/{plan["id"]}/revision/', answer='unchanged', note='same').json()['plan']
    assert not (body['status'] == 'active' and body['current_step_id'] is None), \
        f'plan is {body["status"]} with nothing current and nothing queued'
