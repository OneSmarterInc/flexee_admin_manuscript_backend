"""A planned venue whose editors work on Flexee is sent a real packet, never marked "sent" by hand.

"I have sent it" on such a venue would skip the editor: the step would show as sent while the editor
received nothing, and the author would then record the outcome. Outside venues keep "I have sent it"."""
import pytest

from review.models import SubmissionPlanStep, Venue
from tests.test_audit_plan_medium import _packet
from tests.test_author_dashboard_actions import author_client, env  # noqa: F401
from tests.test_submission_plan import activate, generate, setup_plan, step


def _first_on_flexee(client, author):
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    venue = next(v for v in vs.values() if v.name == first['venue']['name'])
    venue.trust_tier = Venue.TIER_CLAIMED
    venue.save(update_fields=['trust_tier'])
    return m, plan, first, venue


@pytest.mark.django_db
def test_a_flexee_venue_cannot_be_marked_sent_by_hand():
    client, author = author_client()
    m, plan, first, venue = _first_on_flexee(client, author)
    response = step(client, plan, first, 'submitted')
    assert response.status_code == 409 and response.json()['code'] == 'send_packet'
    assert venue.name in response.json()['detail']
    assert SubmissionPlanStep.objects.get(id=first['id']).state == 'current'


@pytest.mark.django_db
def test_the_plan_says_which_venues_take_a_flexee_packet():
    client, author = author_client()
    m, plan, first, venue = _first_on_flexee(client, author)
    steps = client.get(f'/api/author/manuscripts/{m.id}/plan/').json()['plan']['steps']
    flags = {s['venue']['name']: s['venue']['on_flexee'] for s in steps}
    assert flags[venue.name] is True
    assert all(flag is False for name, flag in flags.items() if name != venue.name)


@pytest.mark.django_db
def test_sending_the_packet_moves_the_flexee_step_to_sent():
    client, author = author_client()
    m, plan, first, venue = _first_on_flexee(client, author)
    packet = _packet(m, venue)
    response = client.post(f'/api/author/venue-submissions/{packet.id}/submit/')
    assert response.status_code == 200, response.json()
    planned = SubmissionPlanStep.objects.get(id=first['id'])
    assert planned.state == 'submitted' and planned.venue_submission_id == packet.id


@pytest.mark.django_db
def test_an_outside_venue_is_still_marked_sent_by_hand():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    assert first['venue']['on_flexee'] is False
    assert step(client, plan, first, 'submitted').status_code == 200
    assert SubmissionPlanStep.objects.get(id=first['id']).state == 'submitted'
