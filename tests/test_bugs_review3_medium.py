"""Medium and Low findings of review 3 (plan, packets, claims, matches). Each test failed before its fix."""
import json
import os
from unittest.mock import patch

import pytest
from django.test import Client

from review.models import SubmissionPlan, VenueAgentConfig, VenueSubmission
from tests.test_audit_plan_medium import _packet
from tests.test_author_dashboard_actions import author_client, env, make_manuscript, make_venue  # noqa: F401
from tests.test_bugs_high import _editor, _venue_of
from tests.test_submission_plan import activate, generate, post, setup_plan, step
from tests.test_venue_claims import claim, listed, outbox  # noqa: F401


def _decide(ec, sub, decision, note='editor note'):
    return ec.post(f'/api/admin/venue-submissions/{sub.id}/decision/',
                   data=json.dumps({'decision': decision, 'note': note}), content_type='application/json')


# P2: acceptance by a planned venue while the plan is paused never completes the plan --------------------

@pytest.mark.django_db
def test_p2_acceptance_by_planned_venue_during_pause_completes_the_plan():
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
    assert _decide(_editor(venue2), sub, 'accepted').status_code == 200
    p = SubmissionPlan.objects.get(id=plan['id'])
    # The plan is left 'paused', waiting for an answer every route refuses (answer -> 409 already_accepted).
    r = post(client, f'/api/author/plans/{plan["id"]}/revision/', answer='unchanged', note='go on')
    assert p.status == 'completed' or r.status_code == 200, (p.status, r.json())


# P3: the anonymization step of the venue assessment writes back a stale packet ---------------------------

_OK = json.dumps({k: {'summary': 'Ok', 'venue_fields': [], 'manuscript_evidence_ids': []} for k in
                  ('outlet_fit', 'policy_compliance', 'contribution', 'methods', 'citation_integrity')}
                 | {'editor_summary': 'x', 'unresolved_risks': [], 'reviewer_expertise': []})


@pytest.mark.django_db
@patch.dict(os.environ, {'AI_PROVIDER': 'mock'})
def test_p3_anonymization_check_does_not_drop_requirement_answers_saved_meanwhile():
    from django.core.files.uploadedfile import SimpleUploadedFile
    from review.models import Manuscript, Organization, Venue
    from review.services.author_agents import run_venue_assessment
    org = Organization.objects.create(name='Org')
    venue = Venue.objects.create(name='Blind Venue', slug='blind-venue', organization=org)
    config = VenueAgentConfig.objects.create(venue=venue, policies={'blind_review': True})
    m = Manuscript.objects.create(title='T', manuscript_filename='t.md', parsed_profile={'semantic': {'summary': 'Ok'}})
    m.manuscript_file.save('t.md', SimpleUploadedFile('t.md', b'Clean text.'))
    sub = VenueSubmission.objects.create(manuscript=m, venue=venue, venue_config=config, status='draft', packet={})
    stale = VenueSubmission.objects.get(id=sub.id)

    def chat(*a, **k):
        if chat.n == 0:
            # While the model reads the text, the author saves the cover letter answer.
            VenueSubmission.objects.filter(id=sub.id).update(
                packet={'requirement_responses': {'cover_letter': 'Dear editor'}})
        chat.n += 1
        return ('mock', json.dumps({'status': 'blocked', 'issues': []}) if chat.n == 1 else _OK)
    chat.n = 0
    with patch('review.services.author_agents.ai_chat_json', side_effect=chat):
        run_venue_assessment(stale)
    sub.refresh_from_db()
    assert (sub.packet or {}).get('requirement_responses') == {'cover_letter': 'Dear editor'}, sub.packet


@pytest.mark.django_db
@patch.dict(os.environ, {'AI_PROVIDER': 'mock'})
def test_p4_blocked_rerun_does_not_leave_a_sendable_packet():
    from django.core.files.uploadedfile import SimpleUploadedFile
    from review.models import Manuscript, Organization, Venue
    from review.services.author_agents import run_venue_assessment
    org = Organization.objects.create(name='Org')
    venue = Venue.objects.create(name='Blind Venue', slug='blind-venue', organization=org)
    config = VenueAgentConfig.objects.create(venue=venue, policies={'blind_review': True})
    m = Manuscript.objects.create(title='T', manuscript_filename='t.md', parsed_profile={'semantic': {'summary': 'Ok'}})
    m.manuscript_file.save('t.md', SimpleUploadedFile('t.md', b'John Doe wrote this.'))
    sub = VenueSubmission.objects.create(manuscript=m, venue=venue, venue_config=config, status='packet_ready',
                                         packet={'editorial_brief_ready': True}, editorial_brief={'editor_summary': 'old'})
    with patch('review.services.author_agents.ai_chat_json',
               return_value=('mock', json.dumps({'status': 'blocked', 'issues': [{'type': 'name'}]}))):
        run_venue_assessment(sub)
    sub.refresh_from_db()
    assert sub.packet['anonymization']['status'] == 'blocked'
    assert sub.status != 'packet_ready', 'a blocked (not anonymised) packet can still be sent'


@pytest.mark.django_db
def test_p5_assessment_endpoint_does_not_change_a_sent_packet():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    sub = _packet(m, vs['ready_strong'])
    assert client.post(f'/api/author/venue-submissions/{sub.id}/submit/').status_code == 200
    sub.refresh_from_db()
    before = dict(sub.packet or {})
    r = post(client, f'/api/author/venue-submissions/{sub.id}/assessment/run/', override_anonymization=True)
    sub.refresh_from_db()
    assert sub.packet == before, (r.status_code, sub.packet)
    assert r.status_code == 409, r.status_code


# C2 ------

@pytest.mark.django_db
def test_claim_confirmation_does_not_carry_a_web_address_in_the_name(outbox):  # noqa: F811
    """C2: the A6 fix covers only sign-up. A public claim puts any name (200 chars, links allowed) into an
    email Flexee sends to any address."""
    record = listed()
    response = claim(record, email='victim@example.com',
                     name='Your Flexee payout is waiting, claim it at https://evil.example/login')
    sent_link = any('https://evil.example/login' in m['body'] for m in outbox)
    assert response.status_code == 400 or not sent_link


@pytest.mark.django_db
def test_c2_a_normal_claim_name_is_still_accepted(outbox):  # noqa: F811
    assert claim(listed()).status_code in (200, 201, 202)


@pytest.mark.django_db
def test_p2_acceptance_marks_the_planned_step_accepted():
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
    assert _decide(_editor(venue2), sub, 'accepted').status_code == 200
    body = client.get(f'/api/author/manuscripts/{m.id}/plan/').json()['plan']
    assert body['status'] == 'completed'
    row = next(s for s in body['steps'] if s['id'] == second['id'])
    assert row['state'] == 'accepted'


@pytest.mark.django_db
def test_f2_a_venue_without_configuration_is_not_offered_for_semantic_matching():
    from review.author_api import _match_payload
    from review.models import VenueMatch
    client, author = author_client()
    m = make_manuscript(author)
    with_config = make_venue('With config', 'with-config')
    without = make_venue('No config', 'no-config')
    without.agent_configs.update(active=False)
    version = m.current_version
    rows = {}
    for venue in (with_config, without):
        rows[venue.slug] = VenueMatch.objects.create(manuscript=m, version=version, venue=venue,
                                                     eligibility='eligible', fit_summary='x')
    assert _match_payload(rows['with-config'])['semantic_possible'] is True
    assert _match_payload(rows['no-config'])['semantic_possible'] is False


@pytest.mark.django_db
def test_c2_public_form_refuses_a_link_in_the_author_name():
    from django.core.files.uploadedfile import SimpleUploadedFile
    r = Client().post('/api/submissions/', {
        'type': 'article', 'author': 'Claim at https://evil.example', 'email': 'a@example.com',
        'title': 'T', 'disclosure': 'd', 'attestation': 'human-authored-with-ai-assistance',
        'manuscript': SimpleUploadedFile('m.md', b'# Title\n\ntext'),
    })
    assert r.status_code == 400 and 'web addresses' in r.json()['detail']
