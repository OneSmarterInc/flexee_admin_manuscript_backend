"""High findings of the 13 October bug review (H1 to H10). Each test failed before its fix."""
import json
from unittest import mock
from unittest.mock import patch

import pytest
from django.test import Client

from review import submission_plan as plans
from review.auth import COOKIE_NAME, hash_password, issue_session
from review.models import (EditorUser, Membership, SubmissionPlanStep, Venue, VenueDiscoveryRun, VenueMatch,
                           VenueSubmission)
from tests.test_audit_plan_medium import _packet
from tests.test_author_dashboard_actions import author_client, env, make_manuscript, make_venue  # noqa: F401
from tests.test_submission_plan import activate, generate, post, setup_plan, step


def _claim_all(vs):
    for v in vs.values():
        v.trust_tier = Venue.TIER_CLAIMED
        v.save(update_fields=['trust_tier'])


def _venue_of(vs, s):
    return next(v for v in vs.values() if v.name == s['venue']['name'])


# H1 -----------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_h1_transfer_after_revision_request_frees_the_plan():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    _claim_all(vs)
    plan = activate(client, generate(client, m))
    first, second = plan['steps'][0], plan['steps'][1]
    sub = _packet(m, _venue_of(vs, first), status='submitted')
    plans.sync_from_submission(sub)
    sub.status = 'revision_requested'
    sub.save(update_fields=['status'])
    r = post(client, f'/api/author/venue-submissions/{sub.id}/transfer/', venue_id=str(_venue_of(vs, second).id))
    assert r.status_code == 201, r.json()
    assert SubmissionPlanStep.objects.get(id=first['id']).state == 'withdrawn'
    target = VenueSubmission.objects.get(id=r.json()['submission']['id'])
    from review.author_api import _live_venue_elsewhere
    assert _live_venue_elsewhere(target) == ''


# H2 -----------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_h2_packet_sent_before_start_is_linked_and_the_decline_reaches_the_plan():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    _claim_all(vs)
    plan = generate(client, m)
    first = plan['steps'][0]
    packet = _packet(m, _venue_of(vs, first))
    assert client.post(f'/api/author/venue-submissions/{packet.id}/submit/').status_code == 200
    activate(client, plan)
    s = SubmissionPlanStep.objects.get(id=first['id'])
    assert s.state == 'submitted' and s.venue_submission_id == packet.id
    packet.status = 'rejected'
    packet.save(update_fields=['status'])
    plans.sync_from_submission(packet)
    assert SubmissionPlanStep.objects.get(id=first['id']).state == 'declined'


@pytest.mark.django_db
def test_h2_packet_sent_to_a_queued_venue_becomes_the_current_step():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    _claim_all(vs)
    plan = activate(client, generate(client, m))
    first, second = plan['steps'][0], plan['steps'][1]
    packet = _packet(m, _venue_of(vs, second))
    assert client.post(f'/api/author/venue-submissions/{packet.id}/submit/').status_code == 200
    assert SubmissionPlanStep.objects.get(id=second['id']).state == 'submitted'
    assert SubmissionPlanStep.objects.get(id=first['id']).state == 'queued'


@pytest.mark.django_db
def test_h2_a_rejection_the_plan_never_saw_still_keeps_the_venue_out():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    _claim_all(vs)
    plan = generate(client, m)
    first = plan['steps'][0]
    packet = _packet(m, _venue_of(vs, first))
    assert client.post(f'/api/author/venue-submissions/{packet.id}/submit/').status_code == 200
    packet.status = 'rejected'
    packet.save(update_fields=['status'])
    plans.sync_from_submission(packet)
    new = generate(client, m)
    assert first['venue']['name'] not in [s['venue']['name'] for s in new['steps']]


@pytest.mark.django_db
def test_h2_a_plan_cannot_start_while_a_venue_outside_it_is_deciding():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    _claim_all(vs)
    plan = generate(client, m)
    planned = {s['venue']['name'] for s in plan['steps']}
    outside = next(v for v in vs.values() if v.name not in planned and v.name != 'Faraway Studies')
    _packet(m, outside, status='under_review')
    r = post(client, f'/api/author/plans/{plan["id"]}/activate/')
    assert r.status_code == 409 and r.json()['code'] == 'one_venue_at_a_time'


# H3 -----------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_h3_i_have_sent_it_is_refused_while_a_flexee_venue_is_deciding():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    other = next(v for v in vs.values() if v.name != first['venue']['name'] and v.name != 'Faraway Studies')
    _packet(m, other, status='submitted')
    r = step(client, plan, first, 'submitted')
    assert r.status_code == 409 and r.json()['code'] == 'one_venue_at_a_time'
    assert SubmissionPlanStep.objects.get(id=first['id']).state == 'current'


# H4 -----------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_h4_a_second_submit_of_the_same_packet_is_refused_and_sends_no_second_email():
    client, author = author_client()
    m = make_manuscript(author)
    venue = make_venue('Field Notes Journal', 'fnj')
    packet = _packet(m, venue)
    with mock.patch('review.author_api._send_email') as send:
        assert client.post(f'/api/author/venue-submissions/{packet.id}/submit/').status_code == 200
        assert client.post(f'/api/author/venue-submissions/{packet.id}/submit/').status_code == 409
    assert send.call_count == 1


@pytest.mark.django_db
def test_h4_withdraw_after_the_editor_decided_is_refused():
    client, author = author_client()
    m = make_manuscript(author)
    venue = make_venue('Field Notes Journal', 'fnj')
    packet = _packet(m, venue, status='accepted')
    r = post(client, f'/api/author/venue-submissions/{packet.id}/withdraw/')
    assert r.status_code == 409
    packet.refresh_from_db()
    assert packet.status == 'accepted'


# H5 -----------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_h5_no_new_plan_after_acceptance():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    step(client, plan, first, 'submitted')
    assert step(client, plan, first, 'outcome', outcome='accepted').status_code == 200
    r = client.post(f'/api/author/manuscripts/{m.id}/plan/generate/')
    assert r.status_code == 409 and r.json()['code'] == 'already_accepted'


# H6 and H9 ----------------------------------------------------------------------------------------

def _editor(venue, email='private.editor@journal.org'):
    ed = EditorUser.objects.create(email=email, password_hash=hash_password('x' * 14))
    Membership.objects.create(user=ed, organization=venue.organization, role='editor')
    c = Client()
    c.cookies[COOKIE_NAME] = issue_session(ed.email)[0]
    return c


@pytest.mark.django_db
def test_h6_the_author_never_sees_who_decided():
    client, author = author_client()
    m = make_manuscript(author)
    v = make_venue('FNJ', 'fnj')
    s = VenueSubmission.objects.create(manuscript=m, venue=v, status='submitted')
    ec = _editor(v)
    r = ec.post(f'/api/admin/venue-submissions/{s.id}/decision/',
                data=json.dumps({'decision': 'rejected', 'note': 'out of scope'}), content_type='application/json')
    assert r.status_code == 200, r.content
    body = client.get(f'/api/author/venue-submissions/{s.id}/').json()
    assert 'private.editor@journal.org' not in json.dumps(body)
    assert body['submission']['decision']['note'] == 'out of scope'
    editor_view = ec.get(f'/api/admin/venue-submissions/{s.id}/').json()['submission']
    assert editor_view['decision']['decided_by'] == 'private.editor@journal.org'


@pytest.mark.django_db
def test_h9_the_editor_sees_what_the_author_gave_for_the_venue_requirements():
    client, author = author_client()
    m = make_manuscript(author)
    v = make_venue('FNJ', 'fnj')
    config = v.agent_configs.get()
    config.required_submission_items = [{'key': 'cover_letter', 'label': 'Cover letter', 'type': 'text',
                                         'required': True}]
    config.save()
    s = VenueSubmission.objects.create(manuscript=m, venue=v, venue_config=config, status='submitted',
                                       packet={'requirement_responses': {'cover_letter': 'Dear editors'}})
    detail = _editor(v).get(f'/api/admin/venue-submissions/{s.id}/').json()['submission']
    assert detail['requirements']['configured'] is True
    assert [i['key'] for i in detail['requirements']['items']] == ['cover_letter']


# H7 -----------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_h7_matches_say_whether_semantic_matching_already_ran():
    client, author = author_client()
    make_venue('Field Notes Journal', 'fnj')
    m = make_manuscript(author)
    matches = client.post(f'/api/author/manuscripts/{m.id}/matches/run/').json()['matches']
    assert matches and all(x['semantic_checked'] is False for x in matches)
    VenueMatch.objects.filter(manuscript=m).update(fit_summary='Strong scope fit for applied AI quality work.')
    matches = client.get(f'/api/author/manuscripts/{m.id}/matches/').json()['matches']
    assert all(x['semantic_checked'] is True for x in matches)


# H8 -----------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_h8_stop_ends_a_claude_agent_discovery_run(monkeypatch):
    from review.services import venue_discovery as vd
    from review.services import venue_discovery_agent as vda
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test')
    monkeypatch.setenv('VENUE_DISCOVERY_CATEGORIES', 'journal,publisher,conference')
    run = VenueDiscoveryRun.objects.create(status='queued')
    calls = []

    def fake_run_agent(prompt, settings, **kw):
        calls.append(1)
        VenueDiscoveryRun.objects.filter(id=run.id).update(status='failed', summary='Stopped by admin@x.')
        return '{"venues": []}', [], {'searches': 0, 'tool_errors': []}
    monkeypatch.setattr(vda, 'run_agent', fake_run_agent)
    config = vd.DiscoveryConfig.from_env()
    config.enabled, config.mode, config.max_rechecks_per_run = True, 'claude_agent', 0
    vd.run_discovery(run, config=config)
    run.refresh_from_db()
    assert len(calls) == 1
    assert run.status == 'failed' and run.summary.startswith('Stopped by admin@x.')


# H10 ----------------------------------------------------------------------------------------------

def test_h10_every_chunk_sees_the_ai_use_disclosure(monkeypatch):
    from review.services import review_engine as re_
    monkeypatch.delenv('MOCK_AI_REVIEW', raising=False)
    monkeypatch.setenv('ENABLE_CLOUD_FALLBACK', 'false')
    monkeypatch.setenv('OLLAMA_NUM_CTX', '4096')
    monkeypatch.setenv('OLLAMA_NUM_PREDICT', '2000')

    def careful_model(prompt, **kw):
        items = [{'id': i['id'], 'verdict': 'pass', 'evidence': 'ok', 'gap': ''} for i in re_.ARTICLE_JUDGMENT]
        for item in items:
            if item['id'] == 'ai_disclosure' and 'AI-Use Disclosure' not in prompt:
                item['verdict'] = 'fail'
        return 'fake', json.dumps({'items': items, 'editor_summary': 'x', 'author_letter': 'y'})

    body = '\n\n'.join(['Paragraph about the business problem and what was tried. ' * 20] * 12)
    disclosure = 'I used Claude to edit grammar only.'
    text = f'{body}\n\nAI-Use Disclosure (submitted with the manuscript):\n{disclosure}'
    measured = {'total_words': 2000, 'chapters': [], 'figures': 0, 'checks': []}
    with patch.object(re_, 'ai_chat_json', side_effect=careful_model):
        _model, items, decision, *_rest, meta = re_.judge_with_local_model(
            text, '', re_.ARTICLE_JUDGMENT, 'article', disclosure, measured)
    assert meta['mode'] == 'chunked' and meta['chunk_count'] > 1
    assert {i['id']: i['verdict'] for i in items}['ai_disclosure'] == 'pass'
    assert decision == re_.DECISION_PASS


# H4 under real concurrency (PostgreSQL only: SQLite has no row locks) ---------------------------------

@pytest.mark.django_db(transaction=True)
def test_h4_two_sends_at_the_same_moment_only_one_goes_through():
    import threading

    from django.db import connection, connections

    from review import author_api
    if connection.vendor != 'postgresql':
        pytest.skip('needs row locks (PostgreSQL)')
    client, author = author_client()
    m = make_manuscript(author)
    a = _packet(m, make_venue('Venue A', 'va'))
    b = _packet(m, make_venue('Venue B', 'vb'))
    real_check = author_api._live_venue_elsewhere
    barrier = threading.Barrier(2, timeout=2)

    def slow_check(item):
        try:
            barrier.wait()  # without the lock both sends reach this point together
        except threading.BrokenBarrierError:
            pass            # with the lock the second send is still waiting for it
        return real_check(item)

    results = []

    def send(packet):
        try:
            results.append(client.post(f'/api/author/venue-submissions/{packet.id}/submit/').status_code)
        finally:
            connections.close_all()

    with mock.patch.object(author_api, '_live_venue_elsewhere', side_effect=slow_check), \
            mock.patch('review.author_api._send_email'):
        threads = [threading.Thread(target=send, args=(p,)) for p in (a, b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
    assert sorted(results) == [200, 409]
    assert VenueSubmission.objects.filter(manuscript=m, status='submitted').count() == 1
