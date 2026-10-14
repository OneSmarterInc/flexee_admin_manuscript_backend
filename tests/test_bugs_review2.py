"""Second bug review, 14 October: findings in the code after the 13 October fixes. Each failed before its fix."""
import json
from unittest import mock

import pytest

from review import submission_plan as plans
from review.models import SubmissionPlan, SubmissionPlanStep, VenueSubmission
from tests.test_audit_plan_medium import _packet
from tests.test_author_dashboard_actions import author_client, make_manuscript, make_venue  # noqa: F401
from tests.test_bugs_high import _claim_all, _editor, _venue_of
from tests.test_submission_plan import activate, generate, post, setup_plan, step


def _reject(packet):
    packet.status = 'rejected'
    packet.save(update_fields=['status'])
    plans.sync_from_submission(packet)


@pytest.mark.django_db
def test_a_packet_sent_while_paused_is_linked_after_unchanged():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    _claim_all(vs)
    plan = activate(client, generate(client, m))
    assert len(plan['steps']) >= 3
    first, second, third = plan['steps'][0], plan['steps'][1], plan['steps'][2]
    p1 = _packet(m, _venue_of(vs, first))
    assert client.post(f'/api/author/venue-submissions/{p1.id}/submit/').status_code == 200
    _reject(p1)
    assert SubmissionPlan.objects.get(id=plan['id']).status == 'paused'
    # While the plan waits for the author's answer, the author sends straight to the third venue.
    p3 = _packet(m, _venue_of(vs, third))
    assert client.post(f'/api/author/venue-submissions/{p3.id}/submit/').status_code == 200
    r = post(client, f'/api/author/plans/{plan["id"]}/revision/', answer='unchanged', note='fine as is')
    assert r.status_code == 200, r.json()
    states = {s.id: s.state for s in SubmissionPlanStep.objects.filter(plan_id=plan['id'])}
    import uuid
    # Expected (H2 docstring: "a packet sent ... while the plan was paused ... still belongs to the plan")
    assert states[uuid.UUID(third['id'])] == 'submitted', states


@pytest.mark.django_db
def test_a_paused_packet_rejection_reaches_the_plan():
    import uuid
    client, author = author_client()
    m, vs = setup_plan(client, author)
    _claim_all(vs)
    plan = activate(client, generate(client, m))
    first, second, third = plan['steps'][0], plan['steps'][1], plan['steps'][2]
    p1 = _packet(m, _venue_of(vs, first))
    assert client.post(f'/api/author/venue-submissions/{p1.id}/submit/').status_code == 200
    _reject(p1)
    p3 = _packet(m, _venue_of(vs, third))
    assert client.post(f'/api/author/venue-submissions/{p3.id}/submit/').status_code == 200
    post(client, f'/api/author/plans/{plan["id"]}/revision/', answer='unchanged', note='fine')
    _reject(p3)
    s3 = SubmissionPlanStep.objects.get(id=third['id'])
    s2 = SubmissionPlanStep.objects.get(id=second['id'])
    # skip second -> plan makes the venue that already rejected current again
    r = step(client, plan, second, 'skip', note='no')
    assert r.status_code == 200, r.json()
    s3.refresh_from_db()
    assert s3.state != 'current', 'a venue that rejected the manuscript became the current step again'


@pytest.mark.django_db
def test_start_review_never_overwrites_a_withdrawal():
    client, author = author_client()
    m = make_manuscript(author)
    venue = make_venue('Field Notes Journal', 'fnj')
    packet = _packet(m, venue, status='submitted')
    editor = _editor(venue)
    import review.editor_api as ed
    real = ed.check_org_access

    def withdraw_in_between(*a, **k):
        # the author's withdraw commits while the editor's request is between its read and its save
        assert post(client, f'/api/author/venue-submissions/{packet.id}/withdraw/').status_code == 200
        return real(*a, **k)

    with mock.patch.object(ed, 'check_org_access', side_effect=withdraw_in_between):
        r = editor.post(f'/api/admin/venue-submissions/{packet.id}/start-review/')
    packet.refresh_from_db()
    assert packet.status == 'withdrawn', f'withdrawal overwritten: {packet.status}'


@pytest.mark.django_db
def test_nothing_is_sent_after_another_venue_accepted():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    planned = {s['venue']['name'] for s in plan['steps']}
    outside = next(v for v in vs.values() if v.name not in planned)
    pk = _packet(m, outside, status='submitted')
    pk.status = 'accepted'
    pk.save(update_fields=['status'])
    plans.sync_from_submission(pk)
    first = plan['steps'][0]
    r = step(client, plan, first, 'submitted')
    assert r.status_code != 200, 'manuscript sent to a second venue after another accepted it'


@pytest.mark.django_db
def test_a_body_that_is_not_utf8_is_a_bad_request():
    client, author = author_client()
    client.raise_request_exception = False
    r = client.post('/api/author/change-password/', data=b'\xff\xfe\xfa', content_type='application/json')
    assert r.status_code == 400


@pytest.mark.django_db
def test_an_unknown_plan_is_not_found():
    import uuid
    client, _author = author_client()
    client.raise_request_exception = False
    r = post(client, f'/api/author/plans/{uuid.uuid4()}/activate/')
    assert r.status_code == 404
