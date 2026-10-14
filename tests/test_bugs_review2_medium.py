"""Bug review 2 (14 October), Medium findings (backend). Each test fails on the code before its fix."""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

import pytest
from django.test import Client
from django.utils import timezone

from review import claims_api
from review.auth import COOKIE_NAME, hash_password, issue_session, totp_code
from review.models import (AdminAuthEvent, EditorUser, RulesAttempt, SMTPSettings, VenueClaim, VenueIndexRun,
                           VenueMatch, VenueSubmission)
from review.services import author_agents as aa
from review.services import index_screening as scr
from review.services import review_engine as re_
from review.services import venue_index as vi
from review.services.index_report import screening_overturned
from tests.test_audit_plan_medium import _packet
from tests.test_author_dashboard_actions import author_client, env, make_manuscript, make_venue  # noqa: F401
from tests.test_bugs_high import _claim_all, _editor, _venue_of
from tests.test_submission_plan import activate, generate, post, setup_plan


def post_json(client, url, data, **extra):
    return client.post(url, data=json.dumps(data), content_type='application/json', **extra)


def _decide(ec, sub, decision, note='editor note'):
    return ec.post(f'/api/admin/venue-submissions/{sub.id}/decision/',
                   data=json.dumps({'decision': decision, 'note': note}), content_type='application/json')


def _assessment_setup():
    from tests.test_author_dashboard_actions import make_manuscript, make_venue
    client, author = author_client()
    m = make_manuscript(author)
    m.parsed_profile = {'semantic': {'summary': 'A study of AI inspection.'}}
    m.save(update_fields=['parsed_profile'])
    venue = make_venue('Field Notes Journal', 'fnj')
    sub = _packet(m, venue)   # packet_ready from an earlier assessment
    sub.editorial_brief = {'editor_summary': 'ORIGINAL brief the author submitted with'}
    sub.save(update_fields=['editorial_brief'])
    return client, m, sub


def _run_assessment_with(sub, during):
    from review.services import author_agents
    real_load = author_agents.load_manuscript_text

    def load_and_interleave(source):
        during()
        return real_load(source)

    with mock.patch.object(author_agents, 'load_manuscript_text', side_effect=load_and_interleave), \
            mock.patch.object(author_agents, 'ai_available', return_value=False), \
            mock.patch.object(author_agents, '_citation_checks', side_effect=RuntimeError('offline')):
        author_agents.run_venue_assessment(VenueSubmission.objects.get(id=sub.id))


@pytest.mark.django_db
def test_r5_rerun_assessment_finishing_after_submit_rewrites_the_brief_the_editor_reviews():
    client, m, sub = _assessment_setup()

    def author_submits():   # the author re-ran the assessment, then clicked Submit while it was running
        VenueSubmission.objects.filter(id=sub.id).update(status='submitted')

    _run_assessment_with(sub, author_submits)
    sub.refresh_from_db()
    assert sub.status == 'submitted'
    assert sub.editorial_brief.get('editor_summary') == 'ORIGINAL brief the author submitted with', \
        sub.editorial_brief.get('editor_summary')


@pytest.mark.django_db
def test_r5_assessment_of_old_version_marks_reset_packet_ready():
    client, m, sub = _assessment_setup()
    from review.models import ManuscriptVersion
    v2 = ManuscriptVersion.objects.create(manuscript=m, number=2, filename='v2.md', title='AI inspection v2')

    def new_version_uploaded():   # _reset_draft_packets: back to draft, on the new version
        VenueSubmission.objects.filter(id=sub.id).update(status='draft', version=v2, editorial_brief={})

    _run_assessment_with(sub, new_version_uploaded)
    sub.refresh_from_db()
    assert sub.status == 'draft', 'packet_ready with a brief built from version 1 text, carrying version 2'


@pytest.mark.django_db
def test_r6_revised_manuscript_can_go_back_to_the_venue_that_requested_the_revision():
    from tests.test_submission_plan import new_version
    client, author = author_client()
    m, vs = setup_plan(client, author)
    _claim_all(vs)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    venue = _venue_of(vs, first)
    p1 = _packet(m, venue)
    assert client.post(f'/api/author/venue-submissions/{p1.id}/submit/').status_code == 200
    assert _decide(_editor(venue), p1, 'revision_requested', 'Please shorten section 3').status_code == 200
    new_version(client, m)
    m.refresh_from_db()
    p2 = _packet(m, venue)             # the revised version's packet for the same venue
    r = client.post(f'/api/author/venue-submissions/{p2.id}/submit/')
    # The only way out today is to withdraw p1, which records the inviting venue as "withdrawn",
    # pauses the plan, and keeps the venue out of every regenerated plan.
    assert r.status_code == 200, r.json()


def _pause(input_tokens, output_tokens=500):
    return {'content': [{'type': 'text', 'text': 'working'}], 'stop_reason': 'pause_turn',
            'usage': {'input_tokens': input_tokens, 'output_tokens': output_tokens}}


def _pricing(monkeypatch, daily='1.00'):
    monkeypatch.setenv('AI_COST_ENFORCEMENT_ENABLED', 'true')
    monkeypatch.setenv('AI_DAILY_COST_LIMIT_USD', daily)
    monkeypatch.setenv('AI_ANTHROPIC_INPUT_USD_PER_MILLION', '3')
    monkeypatch.setenv('AI_ANTHROPIC_OUTPUT_USD_PER_MILLION', '15')


@pytest.mark.django_db
def test_agent_failure_after_a_billed_turn_still_records_the_spend(monkeypatch):
    """run_agent: turn 1 (pause_turn) is billed by Anthropic; if turn 2 raises, fail_ai_call stores
    actual_cost_usd=0 and the tokens of turn 1 vanish from the budget."""
    from review.models import AIUsageEvent
    from review.services.venue_discovery_agent import agent_settings, run_agent
    _pricing(monkeypatch, daily='50')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test')
    responses = [_pause(100_000, 2_000)]

    def create(settings, messages):
        if responses:
            return responses.pop(0)
        raise RuntimeError('overloaded')
    with pytest.raises(RuntimeError):
        run_agent('find journals', agent_settings(), create_message=create)
    event = AIUsageEvent.objects.get(operation='venue_discovery_agent')
    assert event.actual_cost_usd > 0, (event.status, event.actual_cost_usd)








def _book_text(words=28000):
    parts = []
    per = words // 12
    for n in range(1, 13):
        parts.append(f'Chapter {n} Title {n}')
        parts.append(('word ' * 40 + '\n\n') * (per // 40))
    return '\n'.join(parts)


def _ok_items(rubric):
    return json.dumps({'items': [{'id': i['id'], 'verdict': 'pass', 'evidence': 'ok', 'gap': ''} for i in rubric]})


def test_shared_qwen_review_is_sized_for_the_context_the_worker_will_accept(monkeypatch):
    """With the shared Qwen queue on, shared_qwen_chat_json caps num_ctx at 8192 and max_tokens at 2000,
    but review_engine sizes the prompt/chunks with OLLAMA_NUM_CTX (16384 in the production template).
    Every book chunk is then rejected as 'too large' and the review job fails."""
    import redis as redis_mod
    monkeypatch.delenv('MOCK_AI_REVIEW', raising=False)
    monkeypatch.setenv('AI_PROVIDER', 'ollama')
    monkeypatch.setenv('SHARED_QWEN_QUEUE_ENABLED', 'true')
    monkeypatch.setenv('SHARED_QWEN_POLL_INTERVAL', '0')
    monkeypatch.setenv('ENABLE_CLOUD_FALLBACK', 'false')
    monkeypatch.setenv('OLLAMA_NUM_CTX', '16384')
    monkeypatch.setenv('OLLAMA_NUM_PREDICT', '900')

    class FakeRedis:
        def __init__(self):
            self.results = {}

        def ping(self):
            return True

        def rpush(self, queue, raw):
            job = json.loads(raw)
            self.results['ai_result:' + job['job_id']] = json.dumps(
                {'ok': True, 'result': {'model': 'qwen-shared', 'content': _ok_items(re_.BOOK_JUDGMENT)}})

        def get(self, key):
            return self.results.get(key)

        def delete(self, key):
            self.results.pop(key, None)
    fake = FakeRedis()
    monkeypatch.setattr(redis_mod, 'from_url', lambda *a, **k: fake)
    disclosure = 'I used AI for copy-editing and for drafting figure captions; all text is my own.'
    text = _book_text()
    measured = re_.structural_checks(text, 'book')
    # Raises RuntimeError("Manuscript review prompt is too large ...") on current code.
    re_.judge_with_local_model(f"{text}\n\nAI-Use Disclosure (submitted with the manuscript):\n{disclosure}",
                               'Flexee SAD', re_.BOOK_JUDGMENT, 'book', disclosure, measured)


def test_chunked_review_does_not_return_a_book_because_one_chunk_cannot_see_the_front_matter(monkeypatch):
    """Same mechanism as H10, for the other whole-book criteria. 'teaches_claim' asks whether the chapters
    deliver what the introduction / table of contents promise. A middle chunk has no introduction, so a
    careful model fails it there; the worst verdict wins and the book is returned to the author even though
    every chunk that can see the promise passes it."""
    monkeypatch.delenv('MOCK_AI_REVIEW', raising=False)
    monkeypatch.setenv('ENABLE_CLOUD_FALLBACK', 'false')
    monkeypatch.setenv('OLLAMA_NUM_CTX', '16384')
    monkeypatch.setenv('OLLAMA_NUM_PREDICT', '900')
    disclosure = 'I used AI for copy-editing and for drafting figure captions; all text is my own.'
    text = _book_text()
    measured = re_.structural_checks(text, 'book')
    measured = dict(measured, checks=[dict(c, passed=True) for c in measured['checks']])  # structure aside

    def careful_model(prompt, **kw):
        manuscript = prompt.split('\nMANUSCRIPT:\n', 1)[1]
        sees_promise = 'Chapter 1 Title 1' in manuscript
        items = [{'id': i['id'], 'verdict': 'pass', 'evidence': 'ok', 'gap': ''} for i in re_.BOOK_JUDGMENT]
        for item in items:
            if item['id'] == 'teaches_claim' and not sees_promise:
                item['verdict'], item['gap'] = 'fail', 'No introduction or table of contents in this text.'
        return 'qwen', json.dumps({'items': items})
    monkeypatch.setattr(re_, 'ai_chat_json', careful_model)
    _m, _items, decision, _s, _l, meta = re_.judge_with_local_model(
        f"{text}\n\nAI-Use Disclosure (submitted with the manuscript):\n{disclosure}",
        'Flexee SAD', re_.BOOK_JUDGMENT, 'book', disclosure, measured)
    assert meta['mode'] == 'chunked' and meta['chunk_count'] > 1
    assert decision != re_.DECISION_FAIL


class _Rows(list):
    def order_by(self, *a):
        return self


def test_match_prompt_shows_the_evidence_ids_it_asks_the_model_to_cite():
    """_match_prompt (and _assessment_prompt) tell the model to cite 'IDs that exist in
    semantic_profile.evidence_points', but _profile_context drops evidence_points. The model never sees
    M001.., so manuscript evidence is either empty or a guessed id that passes validation and attaches an
    unrelated manuscript excerpt to the claim."""
    manuscript = SimpleNamespace(
        title='Agentic Ops', manuscript_type='research_article', abstract='AI agents in ops.',
        parsed_profile={'semantic': {
            'summary': 'AI agents in manufacturing.', 'topics': ['AI agents'], 'methods': ['pilot'],
            'contributions': [], 'limitations': [], 'coverage': {},
            'evidence_points': [{'id': 'M007', 'kind': 'method', 'text': 'controlled pilot at one plant',
                                 'source': {'type': 'manuscript', 'locator': 'lines 4-5'}}]}})
    venue = SimpleNamespace(name='Applied AI Review', venue_type='journal', editor_feedback=_Rows())
    match = SimpleNamespace(venue=venue, eligibility='eligible', reasons=[], gaps=[])
    config = SimpleNamespace(
        venue_id=None, version=1, aims_scope='Applied AI', article_types=[], accepted_methods=[],
        quality_threshold='', reviewer_criteria=[], policies={}, disclosures=[], reporting_standards=[],
        desk_rejection_rules=[], structured_desk_rejection_rules=[], required_submission_items=[],
        deadlines={}, submission_capacity={}, current_demand={})
    prompt = aa._match_prompt(manuscript, match, config)
    assert 'evidence_points' in prompt  # the instruction refers to it ...
    assert 'M007' in prompt, 'the evidence ids the model must cite are not in the prompt'


def superuser_client(email='root@example.com'):
    user = EditorUser.objects.create(email=email, password_hash=hash_password('root-password-123'),
                                     platform_superuser=True)
    client = Client()
    client.cookies[COOKIE_NAME] = issue_session(user.email)[0]
    return client, user


@pytest.mark.django_db
def test_smtp_settings_host_change_without_password_does_not_keep_stored_secret():
    client, _ = superuser_client()
    SMTPSettings.objects.create(id=1, host='smtp.real-provider.com', port=587, username='flexee',
                                password='s3cret-smtp-pass', use_tls=True)
    response = post_json(client, '/api/admin/smtp/', {'host': 'smtp.attacker.example', 'password': ''})
    smtp = SMTPSettings.objects.get(id=1)
    # Either refuse, or drop the old secret: never keep it for a new server.
    assert response.status_code == 400 or smtp.password != 's3cret-smtp-pass', \
        'stored SMTP password is now used for smtp.attacker.example by every outgoing email'


@pytest.mark.django_db
def test_smtp_test_blank_password_refused_when_tls_is_turned_off():
    client, _ = superuser_client()
    SMTPSettings.objects.create(id=1, host='smtp.real-provider.com', port=587, username='flexee',
                                password='s3cret-smtp-pass', use_tls=True, use_ssl=False)
    captured = {}

    def fake_connection(**kwargs):
        captured.update(kwargs)
        raise RuntimeError('stop before network')

    with mock.patch('django.core.mail.get_connection', side_effect=fake_connection):
        post_json(client, '/api/admin/smtp/test/', {'host': 'smtp.real-provider.com', 'port': 25, 'username': 'flexee',
                                               'password': '', 'use_tls': False, 'use_ssl': False,
                                               'test_email': 'x@example.com'})
    assert not (captured.get('password') == 's3cret-smtp-pass' and not captured.get('use_tls')
                and not captured.get('use_ssl')), 'stored SMTP password reused over a plaintext connection on port 25'


@pytest.mark.django_db
def test_second_approval_for_a_never_activated_editor_sends_a_set_password_link(monkeypatch):
    from tests.test_index_rules import journal
    sent = []
    monkeypatch.setattr(claims_api, '_send', lambda to, subject, body: sent.append(body) or {'sent': True})
    root, _ = superuser_client()
    email = 'asha@jisp.example.org'
    for n, title in enumerate(['Journal One of IS Practice', 'Journal Two of IS Practice']):
        record = journal(title, openalex_id=f'S77{n}', homepage=f'https://j{n}.example.org/')
        VenueClaim.objects.create(indexed=record, journal_name=title, name='Asha', email=email, role_title='Editor',
                                  status='pending_review', email_domain='jisp.example.org')
    first, second = VenueClaim.objects.order_by('created_at')
    assert post_json(root, f'/api/admin/claims/{first.id}/approve/', {}).status_code == 200
    # The editor never opened the first link (or it expired after 7 days). A second journal is approved:
    response = post_json(root, f'/api/admin/claims/{second.id}/approve/', {})
    assert response.status_code == 200
    assert EditorUser.objects.get(email=email).password_hash == '!unset'
    assert 'set-password?token=' in sent[-1], 'editor is told to "sign in with your existing account" but has ' \
                                               'no password and no way to get a new link'


@pytest.mark.django_db
def test_admin_login_stops_after_too_many_failures_for_one_account_from_many_networks():
    secret = 'JBSWY3DPEHPK3PXP'
    user = EditorUser.objects.create(email='root@example.com', password_hash=hash_password('right-password-12'),
                                     platform_superuser=True, totp_secret=secret)
    for i in range(25):
        post_json(Client(), '/api/admin/login/', {'username': user.email, 'password': 'right-password-12',
                                                  'totp': '000000'}, REMOTE_ADDR=f'203.0.113.{i + 1}')
    response = post_json(Client(), '/api/admin/login/', {'username': user.email, 'password': 'right-password-12',
                                                         'totp': totp_code(secret)}, REMOTE_ADDR='198.51.100.7')
    assert response.status_code == 429


@pytest.mark.django_db
def test_a_name_with_a_web_address_is_refused_at_sign_up():
    r = post_json(Client(), '/api/author/register/', {
        'email': 'victim@example.com', 'password': 'whatever123',
        'name': 'Your Flexee payout is waiting. Claim it at https://evil.example/login .'})
    assert r.status_code == 400 and r.json()['field'] == 'name'
    ok = post_json(Client(), '/api/author/register/', {'email': 'priya2@example.com', 'password': 'whatever123',
                                                       'name': "Dr. Priya O'Neil-Sharma"})
    assert ok.status_code == 201


@pytest.mark.django_db
def test_an_ai_outage_result_is_not_counted_as_semantic_matching_done():
    client, author = author_client()
    make_venue('Field Notes Journal', 'fnj')
    m = make_manuscript(author)
    client.post(f'/api/author/manuscripts/{m.id}/matches/run/')
    VenueMatch.objects.filter(manuscript=m).update(fit_summary='Semantic analysis unavailable')
    matches = client.get(f'/api/author/manuscripts/{m.id}/matches/').json()['matches']
    assert all(x['semantic_checked'] is False for x in matches)


@pytest.mark.django_db
def test_the_dashboard_shows_an_acceptance_before_a_newer_draft():
    client, author = author_client()
    m = make_manuscript(author)
    accepted = _packet(m, make_venue('Venue A', 'va'), status='accepted')
    _packet(m, make_venue('Venue B', 'vb'))  # a newer draft for another venue
    listing = client.get('/api/author/manuscripts/list/').json()['manuscripts'][0]
    assert listing['latest_submission']['id'] == str(accepted.id)


def test_smtp_errors_carry_detail_for_the_admin_screen():
    from review.views import _smtp_error
    assert _smtp_error('x') == {'error': 'x', 'detail': 'x'}
