"""Review 4b: AI work and background jobs. Every test here asserts the CORRECT behaviour and fails on
main at aad7783 because of the bug it names."""
import json
from datetime import timedelta

import pytest
from django.utils import timezone

from review.models import DiscoveredVenue, VenueDiscoveryRun
from review.services import venue_discovery as vd
from tests.test_venue_discovery import (  # noqa: F401  (env: autouse fixture)
    FakeClaude, FakeProvider, JOURNAL_PAGES, agent_answer, agent_response, env, journal_extraction, make_fetcher,
)


def _staged(name, url, checked_days_ago):
    record = DiscoveredVenue.objects.create(
        name=name, normalized_name=vd.normalize_name(name), venue_type='journal', website_url=url,
        canonical_domain=vd.registrable_domain(vd.canonical_host(url)), source_urls=[url])
    DiscoveredVenue.objects.filter(id=record.id).update(
        last_checked_at=timezone.now() - timedelta(days=checked_days_ago))
    record.refresh_from_db()
    return record


# Y1: a venue whose re-check fails is re-checked again in every run, and every other stale venue waits -----

@pytest.mark.django_db
def test_y1_search_api_recheck_moves_on_after_a_failed_venue(monkeypatch):
    """_recheck_existing takes the max_rechecks_per_run venues with the oldest last_checked_at. A venue
    whose site is dead keeps that timestamp (only last_error is written), so it is picked again by the
    next run, and the next, and the stale venues behind it are never re-checked."""
    dead = _staged('Dead Journal', 'https://dead.example/journal', checked_days_ago=5)
    alive = _staged('Journal of Applied AI in Organizations', 'https://www.meridian-academic.example/jaaio',
                    checked_days_ago=3)
    before = alive.last_checked_at
    fetcher, config = make_fetcher(JOURNAL_PAGES)
    config.max_rechecks_per_run = 1
    for _run in range(2):
        run = VenueDiscoveryRun.objects.create()
        vd.run_discovery(run, config=config, provider=FakeProvider(), fetcher=fetcher, extractor=journal_extraction)
        assert run.status == 'completed'
    dead.refresh_from_db(); alive.refresh_from_db()
    assert 'HTTP 404' in dead.last_error
    assert alive.last_checked_at > before, \
        'two runs with one re-check each both went to the dead venue; the live stale venue was never re-checked'


@pytest.mark.django_db
def test_y1_claude_recheck_does_not_pay_for_the_same_missing_venue_every_run(monkeypatch):
    """Claude mode: a venue the agent did not return keeps its old last_checked_at, so each daily run sends
    the same venue to the paid agent again, while other stale venues are never re-checked."""
    monkeypatch.setenv('VENUE_DISCOVERY_CATEGORIES', 'none')      # rechecks only
    monkeypatch.setenv('VENUE_DISCOVERY_RECHECK_BATCH_SIZE', '1')
    monkeypatch.setenv('VENUE_DISCOVERY_MAX_RECHECKS_PER_RUN', '1')
    _staged('Dead Journal', 'https://dead.example/journal', checked_days_ago=5)
    _staged('Quiet Journal', 'https://quiet.example/journal', checked_days_ago=3)
    prompts = []
    for _run in range(2):
        claude = FakeClaude([agent_response(agent_answer([]))])   # the agent finds nothing on the dead site
        run = VenueDiscoveryRun.objects.create()
        vd.run_discovery(run, create_message=claude)
        assert run.status == 'completed'
        prompts.append(claude.calls[0]['messages'][0]['content'])
    assert any('quiet.example' in p for p in prompts), \
        'both paid re-check calls were spent on the dead venue; the other stale venue was never re-checked'


# Y2: the anonymization check cannot fit a default-sized local context -------------------------------------

_BRIEF = json.dumps({
    'editor_summary': 'Fine.',
    'outlet_fit': {'summary': 'Ok', 'venue_fields': [], 'manuscript_evidence_ids': []},
    'policy_compliance': {'summary': 'Ok', 'venue_fields': [], 'manuscript_evidence_ids': []},
    'contribution': {'summary': 'Ok', 'venue_fields': [], 'manuscript_evidence_ids': []},
    'methods': {'summary': 'Ok', 'venue_fields': [], 'manuscript_evidence_ids': []},
    'citation_integrity': {'summary': 'Ok', 'venue_fields': [], 'manuscript_evidence_ids': []},
    'unresolved_risks': [], 'reviewer_expertise': ['applied AI'],
})


class _Reply:
    def __init__(self, content):
        self.status_code, self.text = 200, ''
        self._content = content

    def json(self):
        return {'message': {'content': self._content}, 'prompt_eval_count': 10, 'eval_count': 10}


@pytest.mark.django_db
def test_y2_blind_review_assessment_fits_the_local_context_it_is_sent_to(monkeypatch):
    """_anonymization_prompt sends 12,000 characters of manuscript plus the instructions (~3,160 tokens)
    with max_tokens=1000. With the code's own default context (DEFAULT_OLLAMA_NUM_CTX=4096, also the value
    in .env.example, and the floor the shared Qwen worker applies when OLLAMA_NUM_CTX is not larger) only
    3,096 prompt tokens fit, so assert_prompt_fits_context raises for every manuscript of 12k characters
    or more. The call is not guarded like the brief call is, so the whole assessment job fails with
    "prompt is too large" and the author of a paper for a blind-review venue can never get a packet."""
    import httpx
    from django.core.files.uploadedfile import SimpleUploadedFile
    from review.models import Manuscript, Organization, Venue, VenueAgentConfig, VenueSubmission
    from review.services.author_agents import run_venue_assessment
    monkeypatch.setenv('AI_PROVIDER', 'ollama')
    monkeypatch.delenv('OLLAMA_NUM_CTX', raising=False)      # the code default, 4096
    monkeypatch.delenv('OLLAMA_NUM_PREDICT', raising=False)
    prompts = []

    def fake_post(url, *, headers=None, json=None, timeout=None):
        prompts.append(json['messages'][0]['content'])
        if 'double-blind' in json['messages'][0]['content']:
            return _Reply('{"status": "passed", "issues": []}')
        return _Reply(_BRIEF)
    monkeypatch.setattr(httpx, 'post', fake_post)
    monkeypatch.setattr(httpx, 'get', lambda *a, **k: _Reply(''))   # ai_available(): Ollama answers

    org = Organization.objects.create(name='Org')
    venue = Venue.objects.create(name='Blind Venue', slug='blind-venue', organization=org)
    config = VenueAgentConfig.objects.create(venue=venue, policies={'blind_review': True})
    manuscript = Manuscript.objects.create(title='Paper', manuscript_filename='paper.md',
                                           parsed_profile={'semantic': {'summary': 'A study of something.'}})
    body = ('This paragraph of the manuscript discusses the method and the findings in some detail. ' * 160).encode()
    assert len(body) > 12000
    manuscript.manuscript_file.save('paper.md', SimpleUploadedFile('paper.md', body))
    submission = VenueSubmission.objects.create(manuscript=manuscript, venue=venue, venue_config=config, status='draft')

    submission = run_venue_assessment(submission)   # RuntimeError('... prompt is too large ...') on main
    assert submission.status == 'packet_ready' and submission.packet['anonymization']['status'] == 'passed'


# Y3: the production readiness check lets Claude-agent discovery go live with no Anthropic pricing -------------

@pytest.mark.django_db
def test_y3_readiness_check_requires_anthropic_pricing_when_the_discovery_agent_will_use_anthropic():
    """verify_production_readiness only requires Anthropic pricing when AI_PROVIDER=anthropic or
    ENABLE_CLOUD_FALLBACK=true. VENUE_DISCOVERY_MODE=claude_agent (the default mode) also bills Anthropic,
    through reserve_ai_call, which with cost enforcement on (required in production) raises
    AIUsageConfigurationError for an unpriced model. The check passes, and every scheduled discovery run
    then fails each category before the first call."""
    from review.ai_usage import AIUsageConfigurationError
    from review.services.venue_discovery_agent import agent_settings, run_agent
    from tests.test_readiness_and_review_model import SHARED_QWEN, line_for, run_checker
    env = {**SHARED_QWEN, 'VENUE_DISCOVERY_ENABLED': 'true', 'VENUE_DISCOVERY_MODE': 'claude_agent',
           'ANTHROPIC_API_KEY': 'sk-ant-test-key', 'ANTHROPIC_MODEL': 'claude-sonnet-5-5'}
    out = run_checker(env)
    pricing = line_for(out, 'Anthropic input and output pricing')
    with pytest.MonkeyPatch.context() as mp:
        for key, value in env.items():
            mp.setenv(key, value)
        with pytest.raises(AIUsageConfigurationError):   # what the daily run hits with exactly this env
            run_agent('find journals', agent_settings(), create_message=lambda s, m: pytest.fail('not reached'))
    assert pricing.startswith('[FAIL]'), f'readiness passed the pricing check: {pricing!r}'


# Y4: the run's "removed" count includes the rows deleted with each journal -------------------------------------

@pytest.mark.django_db
def test_y4_removed_count_counts_journals_not_their_cascaded_rows(monkeypatch):
    """_catalogue_pass adds delete()[0] to run.removed_count. QuerySet.delete() returns the total number of
    rows deleted INCLUDING cascades (RulesAttempt, IndexReviewDecision, VenueClaim), so a journal whose
    rules were read twice counts as three removals in the run summary and the admin page."""
    import uuid
    from review.models import IndexedVenue, RulesAttempt
    from tests.test_venue_index import FakeHttp, MIS, env as _index_env, run_full, source, topic  # noqa: F401
    monkeypatch.setenv('VENUE_INDEX_POLITE_DELAY', '0')
    monkeypatch.setenv('VENUE_INDEX_READ_PAGES', 'false')
    lang_heavy = source('S7', 'Language Teaching Review', issn_l='0000-0051',
                        topics=[topic('1203', 'Language and Linguistics', 60),
                                topic('1404', 'Management Information Systems', 40)])
    monkeypatch.setenv('VENUE_INDEX_MIN_SCOPE_SHARE', '0.25')
    run_full(FakeHttp([[MIS, lang_heavy]]))
    record = IndexedVenue.objects.get(openalex_id='S7')
    chain = uuid.uuid4()
    for attempt, stage in ((1, 'local'), (2, 'local_retry')):
        RulesAttempt.objects.create(chain=chain, record=record, attempt=attempt, stage=stage, provider='ollama',
                                    outcome='inadequate')
    monkeypatch.delenv('VENUE_INDEX_MIN_SCOPE_SHARE')
    run = run_full(FakeHttp([[MIS, lang_heavy]]))
    assert not IndexedVenue.objects.filter(openalex_id='S7').exists()
    assert run.removed_count == 1, f'one journal left the index but the run says {run.removed_count} were removed'


# Y5: the production E2E report still counts shared Qwen calls as unpriced cloud calls ------------------------

@pytest.mark.django_db
def test_y5_e2e_cost_report_does_not_count_shared_qwen_as_unpriced_cloud_calls():
    """docs/e2e-reports/README.md says the 'unpriced_cloud_calls: 4' in the shared Qwen reports was a
    counting error fixed by review 2 (X14). X14 fixed ai_usage_snapshot; production_e2e._ai_cost_report
    still excludes only ollama and mock, so the next shared Qwen E2E report repeats the error."""
    from review.ai_usage import complete_ai_call
    from review.management.commands.production_e2e import Command
    started = timezone.now() - timedelta(minutes=1)
    complete_ai_call(None, provider='shared_qwen', model='qwen2.5-shared', operation='semantic_readiness',
                     input_tokens=1000, output_tokens=200)
    report = Command()._ai_cost_report(started)
    assert report['completed_calls'] == 1
    assert report['unpriced_cloud_calls'] == 0, report


# Y6: one chunk the model answers badly discards every other chunk's judgment ----------------------------------

def test_y6_chunked_review_keeps_the_chunks_that_parsed_when_one_answer_is_not_json(monkeypatch):
    """_judge_chunked skips a chunk whose JSON is not an object (M4 fails only when none is usable), but a
    chunk whose answer is not JSON at all raises json.JSONDecodeError out of _parse_model_json, so the
    whole review job fails and every other chunk's AI call is thrown away. The shared Qwen worker has no
    JSON mode and a 2,000-token output cap, so a cut-off or prose answer on one chunk of a book is normal."""
    from review.services import review_engine as re_
    monkeypatch.delenv('MOCK_AI_REVIEW', raising=False)
    monkeypatch.setenv('ENABLE_CLOUD_FALLBACK', 'false')
    monkeypatch.setenv('OLLAMA_NUM_CTX', '4096')
    monkeypatch.setenv('OLLAMA_NUM_PREDICT', '2000')
    text = '\n\n'.join(['Words here. ' * 200] * 8)
    good = json.dumps({'items': [{'id': i['id'], 'verdict': 'pass', 'evidence': 'ok', 'gap': ''}
                                 for i in re_.ARTICLE_JUDGMENT]})
    answers = iter([good, 'I am sorry, I cannot produce the JSON for this part of the manuscript', good, good])

    def model(prompt, **kw):
        return 'qwen', next(answers)
    monkeypatch.setattr(re_, 'ai_chat_json', model)
    measured = {'total_words': 2000, 'chapters': [], 'figures': 0, 'checks': []}
    _m, items, decision, _s, _l, meta = re_.judge_with_local_model(
        text, '', re_.ARTICLE_JUDGMENT, 'article', '', measured)   # json.JSONDecodeError on main
    assert meta['mode'] == 'chunked' and meta['chunk_count'] >= 3
    assert decision == re_.DECISION_PASS


# Y7: an Anthropic answer that is billed but unusable is recorded at zero cost ----------------------------------

@pytest.mark.django_db
def test_y7_billed_anthropic_tokens_count_against_the_ceiling_even_when_the_answer_is_empty(monkeypatch):
    """_anthropic_chat_json raises when the response holds no text (all thinking, cut at max_tokens) or is a
    refusal. _tracked_call then calls fail_ai_call, which records actual_cost_usd = 0, although Anthropic
    billed the input and the (thinking) output tokens the response reports. With ANTHROPIC_THINKING=on the
    comment in the code says this is exactly what happens on the small JSON budgets; each such call spends
    up to the full max_tokens of thinking and none of it is counted towards the daily ceiling."""
    from types import SimpleNamespace
    from decimal import Decimal
    from review.ai_usage import _committed_cost_since
    from review.services import ai_provider
    from tests.test_anthropic_thinking import install_fake_sdk
    monkeypatch.setenv('ANTHROPIC_THINKING', 'on')
    monkeypatch.setenv('AI_COST_ENFORCEMENT_ENABLED', 'true')
    monkeypatch.setenv('AI_DAILY_COST_LIMIT_USD', '10')
    monkeypatch.setenv('AI_ANTHROPIC_INPUT_USD_PER_MILLION', '3')
    monkeypatch.setenv('AI_ANTHROPIC_OUTPUT_USD_PER_MILLION', '15')
    billed = SimpleNamespace(content=[SimpleNamespace(type='thinking', thinking='...')], stop_reason='max_tokens',
                             usage=SimpleNamespace(input_tokens=20_000, output_tokens=4_000))
    install_fake_sdk(monkeypatch, billed)
    with pytest.raises(RuntimeError):
        ai_provider.ai_chat_json('prompt', max_tokens=650, force_provider='anthropic', operation='manuscript_review')
    now = timezone.now()
    spent = _committed_cost_since(now.replace(hour=0, minute=0, second=0, microsecond=0), now)
    assert spent >= Decimal('0.12'), f'Anthropic billed 20k in + 4k out (USD 0.12) but USD {spent} was counted'


# Y8: the monthly refresh deletes a listed journal together with the editor's pending claim on it ---------------

@pytest.mark.django_db
def test_y8_catalogue_refresh_keeps_a_journal_an_editor_has_claimed(monkeypatch):
    """_catalogue_pass deletes a journal that no longer qualifies (or falls below the size cutoff) unless it
    is linked to a live venue, excluded or from a worklist. VenueClaim.indexed is CASCADE, so a claim that
    is waiting for review (the editor already confirmed the email) is deleted with it: nobody is told, the
    admin never sees it, and the editor waits for ever. The same cascade removes the journal's
    RulesAttempt rows and IndexReviewDecision rows ("Never deleted", models.py)."""
    from review.models import IndexedVenue, VenueClaim
    from tests.test_venue_index import FakeHttp, MIS, run_full, source, topic
    monkeypatch.setenv('VENUE_INDEX_POLITE_DELAY', '0')
    monkeypatch.setenv('VENUE_INDEX_READ_PAGES', 'false')
    lang_heavy = source('S7', 'Language Teaching Review', issn_l='0000-0051',
                        topics=[topic('1203', 'Language and Linguistics', 60),
                                topic('1404', 'Management Information Systems', 40)])
    monkeypatch.setenv('VENUE_INDEX_MIN_SCOPE_SHARE', '0.25')
    run_full(FakeHttp([[MIS, lang_heavy]]))
    record = IndexedVenue.objects.get(openalex_id='S7')
    claim = VenueClaim.objects.create(indexed=record, journal_name=record.title, name='Dr Editor',
                                      email='editor@ltr.example', role_title='Editor-in-chief',
                                      status='pending_review', email_verified_at=timezone.now())
    monkeypatch.delenv('VENUE_INDEX_MIN_SCOPE_SHARE')   # the rules were narrowed back to the default
    run_full(FakeHttp([[MIS, lang_heavy]]))
    assert VenueClaim.objects.filter(id=claim.id, status='pending_review').exists(), \
        "the editor's confirmed claim was deleted with the index record"


# Y9: a deadline the model made up is stored, published and shown to authors ------------------------------------

@pytest.mark.django_db
def test_y9_index_reading_drops_a_deadline_that_is_nowhere_on_the_pages():
    """The index read keeps only quoted rules (keep_quoted_rules) for scope, article types and required
    items, and checks limits against the page text. The model's `deadlines` dict (and desk_rejection_rules,
    disclosures, reporting_standards, retention_days) is stored as returned: a date the model invented is
    published with the venue and author_config_view shows it to authors as a live, dated deadline."""
    from review.services import index_rules as ir
    from review.services.freshness import author_config_view
    from tests.test_index_rules import env as _rules_env, journal, run_rules  # noqa: F401
    from tests.test_rules_escalation import journal_extraction as quoted_extraction

    def inventive(pages=None, config=None):
        data = quoted_extraction(pages, config)
        data['deadlines'] = {'Manuscript deadline': '2027-06-01'}   # not on any fetched page
        return data
    item = journal()
    fetcher, _ = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher, extractor=inventive)
    item.refresh_from_db()
    assert item.rules_status == 'ready'
    stored = item.discovered.deadlines
    shown = author_config_view(type('V', (), {'trust_tier': 'verified_index', 'calls_checked_at': None,
                                              'open_calls': [], 'last_verified_at': None})(), None,
                               {'deadlines': stored, 'current_demand': {}})['deadlines']
    assert 'Manuscript deadline' not in stored and 'Manuscript deadline' not in shown, (stored, shown)


# Y10: one cloud escalation can make two paid Anthropic calls -----------------------------------------------------

@pytest.mark.django_db
def test_y10_the_cloud_escalation_ceiling_counts_paid_calls(monkeypatch):
    """extract_with_ai retries once when the answer is not JSON. For the cloud stage that is a second billed
    Anthropic call for one escalation: VENUE_INDEX_ESCALATIONS_PER_RUN=10 allows 20 paid calls, and the
    run's rules_escalated / the admin's cloud-call count disagree."""
    from review.services import ai_provider
    from review.services import rules_escalation as esc
    from tests.test_index_rules import env as _rules_env  # noqa: F401
    monkeypatch.setenv('VENUE_INDEX_ESCALATE', 'anthropic')
    monkeypatch.setenv('VENUE_INDEX_ESCALATIONS_PER_RUN', '1')
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-test')
    paid = []

    def fake_chat(prompt, **kw):
        paid.append(kw.get('force_provider'))
        return 'claude-sonnet-5-5', 'Sorry, here is my reading in prose rather than JSON.'
    monkeypatch.setattr(ai_provider, 'ai_chat_json', fake_chat)
    config = vd.DiscoveryConfig.from_env()
    escalation = esc.Escalation.from_env()
    pages = [vd.FetchedPage(url='https://one.example/', title='One', text='Welcome to One.')]
    with pytest.raises(vd.DiscoveryExtractionError):
        esc._call('cloud', pages, config, extractor=None, feedback='', escalation=escalation)
    assert paid == ['anthropic'], f'{len(paid)} paid Anthropic calls for one escalation'


# Y11: a match row removed while the AI was explaining it fails the whole semantic matching job --------------

@pytest.mark.django_db
def test_y11_semantic_matching_survives_a_match_deleted_while_the_ai_was_answering(monkeypatch):
    """author_generate_matches is not blocked while a semantic_matches job runs (only edits and uploads are),
    and a re-run deletes matches that left the topical shortlist. run_semantic_matching then calls
    match.save(update_fields=...) on a row that is gone: Django raises DatabaseError, the job is marked
    failed, and the AI explanations of every later match in the list are never written."""
    from review.models import Manuscript, Organization, Venue, VenueAgentConfig, VenueMatch
    from review.services import author_agents as aa
    org = Organization.objects.create(name='Org')
    manuscript = Manuscript.objects.create(title='Paper', manuscript_filename='paper.md',
                                           parsed_profile={'semantic': {'summary': 'A study.', 'evidence_points': []}})
    matches = []
    for slug in ('alpha', 'beta'):
        venue = Venue.objects.create(name=slug.title(), slug=slug, organization=org)
        config = VenueAgentConfig.objects.create(venue=venue, active=True, version=1, aims_scope='AI in organizations')
        matches.append(VenueMatch.objects.create(manuscript=manuscript, venue=venue, venue_config=config,
                                                 eligibility='eligible', reasons=['gate ok']))

    def answer(prompt, **kw):
        if 'Name: Alpha' in prompt:   # the author re-ran the gate meanwhile; Alpha left the shortlist
            VenueMatch.objects.filter(id=matches[0].id).delete()
        return 'mock', {'fit_summary': 'Fits well.', 'reasons': [{'text': 'Topic overlap', 'venue_fields': ['aims_scope']}],
                        'gaps': []}
    monkeypatch.setattr(aa, '_agent_json', answer)
    manuscript.refresh_from_db()   # the first match row gave the manuscript its version 1
    result = aa.run_semantic_matching(manuscript)   # DatabaseError on main
    beta = VenueMatch.objects.get(id=matches[1].id)
    assert beta.fit_summary == 'Fits well.' and 'Topic overlap' in beta.reasons
    assert not result['errors']
