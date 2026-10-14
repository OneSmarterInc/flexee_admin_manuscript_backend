"""Medium findings of the 13 October bug review (backend: M1 to M8, M14). Each test failed before its fix."""
import json
from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from review.models import DiscoveredVenue, SMTPSettings, SubmissionPlanStep, Venue, VenueDiscoveryRun, VenueIndexRun
from review.services import freshness as fr
from review.services import review_engine as re_
from review.services import venue_discovery as vd
from review.services import venue_index as vi
from tests.test_audit_plan_medium import _packet
from tests.test_author_dashboard_actions import author_client, env, make_venue  # noqa: F401
from tests.test_index_rules import env as index_env  # noqa: F401
from tests.test_submission_plan import activate, generate, post, setup_plan, step
from tests.test_venue_discovery import make_fetcher


def _venue_of(vs, s):
    return next(v for v in vs.values() if v.name == s['venue']['name'])


# M1 -----------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_m1_a_decided_packet_cannot_be_linked_to_a_plan_step():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    old = _packet(m, _venue_of(vs, first), status='withdrawn')
    r = step(client, plan, first, 'submitted', venue_submission_id=str(old.id))
    assert r.status_code == 400
    assert SubmissionPlanStep.objects.get(id=first['id']).state == 'current'


# M2 -----------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_m2_an_outside_venue_sent_by_packet_records_its_own_answer_and_frees_the_manuscript():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    venue = _venue_of(vs, first)
    assert venue.trust_tier == Venue.TIER_VERIFIED_INDEX
    packet = _packet(m, venue)
    assert client.post(f'/api/author/venue-submissions/{packet.id}/submit/').status_code == 200
    r = step(client, plan, first, 'outcome', outcome='declined', note='They said no by email.')
    assert r.status_code == 200, r.json()
    packet.refresh_from_db()
    assert packet.status == 'rejected' and packet.decision['recorded_by'] == 'author'
    assert SubmissionPlanStep.objects.get(id=first['id']).state == 'declined'


# M3 -----------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_m3_a_venue_the_manuscript_was_withdrawn_from_is_not_proposed_again():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    packet = _packet(m, _venue_of(vs, first))
    assert client.post(f'/api/author/venue-submissions/{packet.id}/submit/').status_code == 200
    assert post(client, f'/api/author/venue-submissions/{packet.id}/withdraw/', reason='no reply').status_code == 200
    assert post(client, f'/api/author/plans/{plan["id"]}/revision/', answer='unchanged', note='same').status_code == 200
    new = generate(client, m)
    assert first['venue']['name'] not in [s['venue']['name'] for s in new['steps']]
    reasons = {x['venue']: x['reason'] for x in new['not_included']}
    assert reasons[first['venue']['name']] == 'The manuscript was withdrawn from this venue.'


# M4 and M5 ---------------------------------------------------------------------------------------

def _review_env(monkeypatch, ctx):
    monkeypatch.delenv('MOCK_AI_REVIEW', raising=False)
    monkeypatch.setenv('ENABLE_CLOUD_FALLBACK', 'false')
    monkeypatch.setenv('OLLAMA_NUM_CTX', str(ctx))
    monkeypatch.setenv('OLLAMA_NUM_PREDICT', '2000')


MEASURED = {'total_words': 2000, 'chapters': [], 'figures': 0, 'checks': []}


def test_m4_no_usable_chunk_fails_the_review_instead_of_passing_it(monkeypatch):
    _review_env(monkeypatch, 4096)
    text = '\n\n'.join(['Words here. ' * 200] * 6)
    with patch.object(re_, 'ai_chat_json', return_value=('fake', '[]')):
        with pytest.raises(RuntimeError):
            re_.judge_with_local_model(text, '', re_.ARTICLE_JUDGMENT, 'article', '', MEASURED)


def test_m5_items_null_is_read_as_no_judgment_not_a_crash(monkeypatch):
    _review_env(monkeypatch, 32000)
    answer = json.dumps({'items': None, 'editor_summary': 'a', 'author_letter': 'b'})
    with patch.object(re_, 'ai_chat_json', return_value=('fake', answer)):
        _m, items, decision, *_ = re_.judge_with_local_model('short text', '', re_.ARTICLE_JUDGMENT, 'article', '',
                                                             MEASURED)
    assert items and all(i['verdict'] == 'needs_work' for i in items)
    assert decision != re_.DECISION_PASS


# M6 -----------------------------------------------------------------------------------------------

def test_m6_minutes_above_the_run_limit_are_refused():
    with pytest.raises(CommandError, match='at most'):
        call_command('import_venue_index', '--minutes', str(vi.MAX_RUN_MINUTES + 1))


@pytest.mark.django_db
def test_m6_force_never_closes_a_run_that_may_still_be_inside_the_longest_limit():
    run, _ = vi.start_index_run(mode='full', trigger='command')
    VenueIndexRun.objects.filter(id=run.id).update(status='processing',
                                                   created_at=timezone.now() - timedelta(minutes=31))
    assert vi.force_close_runs() == 0
    VenueIndexRun.objects.filter(id=run.id).update(created_at=timezone.now() - timedelta(minutes=34))
    assert vi.force_close_runs() == 1


# M7 -----------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_m7_claude_recheck_leaves_venue_index_journals_to_the_index(monkeypatch):
    from review.services import venue_discovery_agent as vda
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test')
    monkeypatch.setenv('VENUE_DISCOVERY_CATEGORIES', '')
    old = timezone.now() - timedelta(days=30)
    idx = DiscoveredVenue.objects.create(name='Index Journal', normalized_name='index journal', origin='index',
                                         website_url='https://idx.example/j', discovery_status='new',
                                         first_discovered_at=old, last_checked_at=old)
    sent = []
    monkeypatch.setattr(vda, 'run_agent', lambda prompt, settings, **kw: (sent.append(prompt) or
                                                                           ('{"venues": []}', [], {})))
    config = vd.DiscoveryConfig.from_env()
    config.max_rechecks_per_run, config.recheck_after_hours = 10, 24
    vd._run_claude_agent(VenueDiscoveryRun.objects.create(status='processing'), config)
    assert sent == []
    idx.refresh_from_db()
    assert idx.last_error == ''


# M8 -----------------------------------------------------------------------------------------------

HOME = """<html><head><title>Journal of Digital Operations</title></head><body>
<p>We publish original research articles.</p>
<a href="/jdo/special-issues">Special issues</a></body></html>"""
CFP = """<html><head><title>Special issues - call for papers</title></head><body>
<h2>Special Issue: AI in Supply Chain Resilience</h2>
<p>Submission deadline: 15 March 2027</p></body></html>"""


@pytest.mark.django_db
def test_m8_the_weekly_check_keeps_a_call_still_open_in_the_americas():
    now = datetime(2027, 3, 16, 3, 0, tzinfo=dt_tz.utc)  # evening of 15 March in the Americas
    venue = make_venue('Journal of Digital Operations', 'jdo')
    Venue.objects.filter(id=venue.id).update(trust_tier=Venue.TIER_VERIFIED_INDEX, last_verified_at=now,
                                             source_urls=['https://ops.example/jdo'])
    venue.refresh_from_db()
    fetcher, config = make_fetcher({'https://ops.example/jdo': HOME,
                                    'https://ops.example/jdo/special-issues': CFP})
    assert fr.check_calls(venue, fetcher, config, now=now) == 1


# M14 ----------------------------------------------------------------------------------------------

@pytest.mark.django_db
def test_m14_a_blank_password_never_sends_the_saved_one_to_another_host():
    from django.test import Client

    from review.auth import COOKIE_NAME, issue_session
    from review.models import EditorUser
    from review.auth import hash_password
    EditorUser.objects.create(email='root@flexee.org', password_hash=hash_password('x' * 14), platform_superuser=True)
    SMTPSettings.objects.create(id=1, host='smtp.saved.example', username='mailer', password='s3cret')
    c = Client()
    c.cookies[COOKIE_NAME] = issue_session('root@flexee.org')[0]
    with patch('django.core.mail.EmailMultiAlternatives.send', return_value=1) as send:
        r = c.post('/api/admin/smtp/test/', data=json.dumps({'host': 'evil.example', 'username': 'mailer',
                                                             'password': ''}), content_type='application/json')
        assert r.status_code == 400 and send.call_count == 0
        r = c.post('/api/admin/smtp/test/', data=json.dumps({'host': 'smtp.saved.example', 'username': 'mailer',
                                                             'password': '', 'test_email': 'a@b.org'}),
                   content_type='application/json')
        assert r.status_code == 200 and send.call_count == 1


# M11 (backend half) -------------------------------------------------------------------------------

@pytest.mark.django_db
def test_m11_choosing_the_same_venue_again_reuses_the_open_draft():
    from tests.test_author_dashboard_actions import make_manuscript
    client, author = author_client()
    venue = make_venue('Field Notes Journal', 'fnj')
    m = make_manuscript(author)
    client.post(f'/api/author/manuscripts/{m.id}/matches/run/')
    first = post(client, f'/api/author/manuscripts/{m.id}/submissions/', venue_id=str(venue.id))
    again = post(client, f'/api/author/manuscripts/{m.id}/submissions/', venue_id=str(venue.id))
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()['submission']['id'] == first.json()['submission']['id']
    from review.models import VenueSubmission
    assert VenueSubmission.objects.filter(manuscript=m, venue=venue).count() == 1
