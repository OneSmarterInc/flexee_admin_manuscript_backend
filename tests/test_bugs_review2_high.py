"""Bug review 2 (14 October), High findings: P3, A1, X6, X1, X3, X4. Each test fails on the code before its fix."""
import json

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from review.models import IndexedVenue, ReviewJob, VenueDiscoveryRun, VenueIndexRun
from review.services import index_screening as scr
from review.services import venue_index as vi
from tests.test_audit_plan_medium import _packet
from tests.test_author_dashboard_actions import author_client, env  # noqa: F401
from tests.test_bugs_high import _editor
from tests.test_index_rules import journal
from tests.test_submission_plan import activate, generate, post, setup_plan, step


def _post_json(client, url, data, **extra):
    return client.post(url, data=json.dumps(data), content_type='application/json', **extra)


def _decide(ec, sub, decision, note='editor note'):
    return ec.post(f'/api/admin/venue-submissions/{sub.id}/decision/',
                   data=json.dumps({'decision': decision, 'note': note}), content_type='application/json')


@pytest.mark.django_db
def test_r3_draft_plan_cannot_start_after_the_manuscript_was_accepted():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = generate(client, m)
    planned = {s['venue']['name'] for s in plan['steps']}
    outside = next(v for v in vs.values() if v.name not in planned and v.name != 'Faraway Studies')
    sub = _packet(m, outside)
    assert client.post(f'/api/author/venue-submissions/{sub.id}/submit/').status_code == 200
    assert _decide(_editor(outside), sub, 'accepted').status_code == 200
    r = post(client, f'/api/author/plans/{plan["id"]}/activate/')
    assert r.status_code == 409, r.json()


@pytest.mark.django_db
def test_r3_accepted_manuscript_cannot_be_sent_to_another_venue():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    sub = _packet(m, vs['ready_strong'])
    assert client.post(f'/api/author/venue-submissions/{sub.id}/submit/').status_code == 200
    assert _decide(_editor(vs['ready_strong']), sub, 'accepted').status_code == 200
    other = _packet(m, vs['edit'])
    r = client.post(f'/api/author/venue-submissions/{other.id}/submit/')
    assert r.status_code == 409, r.json()


@pytest.mark.django_db
def test_r3_paused_plan_answer_unchanged_after_outside_acceptance_still_sends_on():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    step(client, plan, first, 'submitted')
    step(client, plan, first, 'outcome', outcome='declined', note='No')
    planned = {s['venue']['name'] for s in plan['steps']}
    outside = next(v for v in vs.values() if v.name not in planned and v.name != 'Faraway Studies')
    sub = _packet(m, outside)
    assert client.post(f'/api/author/venue-submissions/{sub.id}/submit/').status_code == 200
    assert _decide(_editor(outside), sub, 'accepted').status_code == 200
    r = post(client, f'/api/author/plans/{plan["id"]}/revision/', answer='unchanged', note='go on')
    assert r.status_code == 409, r.json()['plan']['status']


@pytest.mark.django_db
def test_r3_i_have_sent_it_after_acceptance_elsewhere_is_refused():
    client, author = author_client()
    m, vs = setup_plan(client, author)
    plan = activate(client, generate(client, m))
    first = plan['steps'][0]
    planned = {s['venue']['name'] for s in plan['steps']}
    outside = next(v for v in vs.values() if v.name not in planned and v.name != 'Faraway Studies')
    sub = _packet(m, outside)
    assert client.post(f'/api/author/venue-submissions/{sub.id}/submit/').status_code == 200
    assert _decide(_editor(outside), sub, 'accepted').status_code == 200
    r = step(client, plan, first, 'submitted')
    assert r.status_code == 409, r.json()['plan']['steps'][0]['state']


@pytest.mark.django_db
def test_manuscript_access_token_is_revoked_by_password_change():
    client, author = author_client()
    upload = SimpleUploadedFile('paper.md', b'# Title\n\nBody text.\n', content_type='text/markdown')
    created = client.post('/api/author/manuscripts/', {
        'title': 'T', 'author': 'Priya', 'disclosure': 'None', 'attestation': 'true',
        'manuscript_type': 'practitioner_article', 'manuscript': upload,
    })
    assert created.status_code == 201, created.content
    manuscript_id = created.json()['manuscript']['id']
    token = created.json()['access_token']

    changed = _post_json(client, '/api/author/change-password/', {
        'current_password': 'pass12345', 'new_password': 'brand-new-pass-1', 'confirm_password': 'brand-new-pass-1'})
    assert changed.status_code == 200
    _post_json(client, '/api/author/sign-out-other-devices/', {})

    # Someone who copied the token earlier (sessionStorage on a shared/compromised machine), no cookie at all:
    thief = Client()
    response = thief.get(f'/api/author/manuscripts/{manuscript_id}/file/', HTTP_X_MANUSCRIPT_TOKEN=token)
    assert response.status_code in (401, 403), 'the bearer token still downloads the manuscript after the ' \
                                               'author changed the password and signed out other devices'


@pytest.mark.django_db
@pytest.mark.parametrize('left_as', ['processing', 'failed'])
def test_a_redelivered_review_job_is_not_run_again(monkeypatch, left_as):
    """Q_CLUSTER timeout=1800, retry=1860, max_attempts unset (0 = unlimited). A job the worker killed at
    30 minutes is re-delivered; the sweeper has meanwhile marked it failed (or it is still 'processing').
    The discovery/index tasks refuse a re-delivered run, but the ReviewJob tasks start over: the job flips
    back to 'processing', every AI call is made again, and a job that always needs >30 min loops forever."""
    from review import tasks
    calls = []
    monkeypatch.setattr(tasks, 'run_semantic_readiness', lambda manuscript: calls.append(manuscript))
    monkeypatch.setattr(tasks.Manuscript.objects, 'get', lambda **kw: object())
    job = ReviewJob.objects.create(job_type='semantic_readiness', reference_id='0' * 36, status=left_as)
    tasks.run_semantic_readiness_task(job.id, '0' * 36)
    job.refresh_from_db()
    assert calls == [], f'the AI work ran again on a re-delivered job (status now {job.status})'


@pytest.mark.django_db
def test_claude_agent_mode_respects_the_run_time_limit(monkeypatch):
    """search_api mode stops at max_run_minutes(); claude_agent mode only checks for 'stopped'."""
    from review.services import venue_discovery as vd
    from review.services import venue_discovery_agent as vda
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test')
    monkeypatch.setenv('VENUE_DISCOVERY_CATEGORIES', 'journal,publisher,conference')
    monkeypatch.setattr(vd, 'max_run_minutes', lambda: 0)  # time budget already used up
    run = VenueDiscoveryRun.objects.create(status='queued')
    calls = []

    def fake_run_agent(prompt, settings, **kw):
        calls.append(1)
        return '{"venues": []}', [], {'searches': 0, 'tool_errors': []}
    monkeypatch.setattr(vda, 'run_agent', fake_run_agent)
    config = vd.DiscoveryConfig.from_env()
    config.enabled, config.mode, config.max_rechecks_per_run = True, 'claude_agent', 0
    vd.run_discovery(run, config=config)
    assert calls == [], f'{len(calls)} paid agent calls made after the time limit'


def _pause(input_tokens, output_tokens=500):
    return {'content': [{'type': 'text', 'text': 'working'}], 'stop_reason': 'pause_turn',
            'usage': {'input_tokens': input_tokens, 'output_tokens': output_tokens}}


def _pricing(monkeypatch, daily='1.00'):
    monkeypatch.setenv('AI_COST_ENFORCEMENT_ENABLED', 'true')
    monkeypatch.setenv('AI_DAILY_COST_LIMIT_USD', daily)
    monkeypatch.setenv('AI_ANTHROPIC_INPUT_USD_PER_MILLION', '3')
    monkeypatch.setenv('AI_ANTHROPIC_OUTPUT_USD_PER_MILLION', '15')


@pytest.mark.django_db
def test_agent_continuations_cannot_push_spend_past_the_daily_ceiling(monkeypatch):
    """The reservation counts the prompt + tool content once, but every pause_turn continuation resends
    the whole conversation, so input tokens grow per turn. A run that was allowed under a $1.00 ceiling
    can complete at well over $1.00."""
    from decimal import Decimal
    from review.ai_usage import _committed_cost_since
    from review.services.venue_discovery_agent import agent_settings, run_agent
    from django.utils import timezone
    _pricing(monkeypatch, daily='1.00')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test')
    responses = [_pause(100_000), _pause(120_000), _pause(140_000), _pause(160_000),
                 {'content': [{'type': 'text', 'text': '{"venues": []}'}], 'stop_reason': 'end_turn',
                  'usage': {'input_tokens': 180_000, 'output_tokens': 500}}]
    run_agent('find journals', agent_settings(), create_message=lambda s, m: responses.pop(0))
    now = timezone.now()
    spent = _committed_cost_since(now.replace(hour=0, minute=0, second=0, microsecond=0), now)
    assert spent <= Decimal('1.00'), f'spent USD {spent} under a USD 1.00 daily ceiling'


class _SyncExecutor:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def map(self, fn, items):
        return [fn(i) for i in items]


@pytest.mark.django_db
def test_enrichment_does_not_undo_a_reviewers_exclusion_made_while_it_was_checking(monkeypatch):
    """_enrichment_pass loads a batch, fetches Crossref/DOAJ facts (seconds per batch), then done.save()
    writes the WHOLE row from the copy loaded before the network calls. An admin who excludes the journal
    in that window has the exclusion silently reverted (excluded=False, status back), while the live Venue
    keeps excluded=True."""
    import concurrent.futures
    monkeypatch.setattr(concurrent.futures, 'ThreadPoolExecutor', _SyncExecutor)
    item = journal(enriched_at=None)
    original = vi.fetch_enrichment

    def fetch_while_admin_decides(record, http, now):
        fresh = IndexedVenue.objects.get(id=record.id)
        scr.decide(fresh, decision='exclude', user_email='admin@x', criteria=['guaranteed_acceptance'],
                   evidence_urls=['https://example.org/promise'])
        record.crossref = {'registered': True, 'checked_at': now.isoformat()}
        record.enriched_at = now
        return record
    monkeypatch.setattr(vi, 'fetch_enrichment', fetch_while_admin_decides)
    run = VenueIndexRun.objects.create(mode='enrich', trigger='command')
    vi.run_index(run, http=object(), read_pages=False)
    item.refresh_from_db()
    assert item.excluded is True and item.screening_status == 'excluded', (item.excluded, item.screening_status)
