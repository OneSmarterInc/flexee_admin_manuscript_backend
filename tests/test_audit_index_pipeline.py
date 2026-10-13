"""12 October audit, Venue Index pipeline (items 33 to 38).

33 Venue Discovery never re-checks or overwrites a journal the Venue Index keeps.
34 Matching inside a web request spends only a few seconds on embeddings; a broken embedding reply
   is "unavailable", never a crash.
35 Each AI call gets only the time left in the run; a journal that cannot start is left for the next
   run, not marked failed.
36 The open-call deadline is the date next to the deadline words, not the first future date.
37 --force closes only runs left over from a restart, and a closed run is not reopened by its worker.
38 HTTP 429 is "busy, try later", not "blocks automated reading"; an unreadable robots.txt stops the
   fetch for now.
"""
from datetime import date, timedelta

import httpx
import pytest
from django.utils import timezone

from review.models import DiscoveredVenue, VenueIndexRun
from review.services import venue_discovery as vd
from review.services import venue_index as vi
from review.services import venue_shortlist as vs
from tests.test_index_rules import env, journal, run_rules  # noqa: F401  (env: autouse fixture)
from tests.test_venue_discovery import JOURNAL_PAGES, journal_extraction, make_fetcher


# ---------------------------------------------------------------------------
# 33
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_discovery_does_not_overwrite_an_index_record():
    item = journal()
    fetcher, _ = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher)
    item.refresh_from_db()
    kept = DiscoveredVenue.objects.get(id=item.discovered_id)
    before = (kept.aims_scope, kept.article_types, kept.source_evidence)
    candidate = vd.candidate_from_raw(dict(journal_extraction(), aims_scope='Something else entirely.'),
                                      [vd.FetchedPage(url=u, title='t', text='x') for u in JOURNAL_PAGES])
    record, outcome = vd.upsert_candidate(candidate, 'other-fingerprint')
    kept.refresh_from_db()
    assert record.id == kept.id and outcome == 'unchanged'
    assert (kept.aims_scope, kept.article_types, kept.source_evidence) == before


@pytest.mark.django_db
def test_discovery_rechecks_skip_index_records(monkeypatch):
    item = journal()
    fetcher, _ = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher)
    item.refresh_from_db()
    DiscoveredVenue.objects.filter(id=item.discovered_id).update(last_checked_at=timezone.now() - timedelta(days=60))
    called = []
    monkeypatch.setattr(vd, 'process_candidate', lambda *a, **k: called.append(a) or (None, 'unchanged'))
    run = vd.VenueDiscoveryRun.objects.create(status='processing')
    config = vd.DiscoveryConfig.from_env()
    config.max_rechecks_per_run, config.recheck_after_hours = 10, 1
    vd._recheck_existing(run, fetcher, config, None, set())
    assert called == []


# ---------------------------------------------------------------------------
# 34
# ---------------------------------------------------------------------------

def test_a_broken_embedding_reply_is_unavailable_not_a_crash():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text='<html>oops</html>')))
    with pytest.raises(vs.EmbeddingUnavailable):
        vs.ollama_embed(['text'], model='m', client=client)
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={'embeddings': [['a', 'b']]})))
    with pytest.raises(vs.EmbeddingUnavailable):
        vs.ollama_embed(['text'], model='m', client=client)


@pytest.mark.django_db
def test_matching_in_a_request_does_not_wait_for_venue_embeddings(monkeypatch):
    from tests.test_author_dashboard_actions import make_manuscript, make_venue
    from review.models import Author
    author = Author.objects.create(email='p@x.org', name='P', password_hash='x')
    manuscript = make_manuscript(author)
    venues = [make_venue('Journal One', 'one'), make_venue('Journal Two', 'two')]
    sent = []

    def embed(texts, model=None):
        sent.append(len(texts))
        return [[1.0, 0.0]] * len(texts)
    _kept, info = vs.build_shortlist(manuscript, venues, embed=embed, time_limit=0)
    assert info['method'] == 'keywords'          # fell back instead of embedding every venue in the request
    assert sum(sent) <= 1                         # at most the manuscript itself; no venue was embedded


def test_the_request_limit_is_short():
    assert vs.request_time_limit() <= 60


# ---------------------------------------------------------------------------
# 35
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_journal_is_left_for_the_next_run_when_too_little_time_is_left():
    item = journal()
    fetcher, _ = make_fetcher(JOURNAL_PAGES)
    run = run_rules(fetcher, time_limit_seconds=20)   # inside the limit, but under 30 s for a model call
    item.refresh_from_db()
    assert item.rules_status == 'not_read' and item.rules_read_at is None
    assert run.rules_attempted == 0 and run.status == 'completed'


@pytest.mark.django_db
def test_each_model_call_waits_at_most_the_time_left(monkeypatch):
    item = journal()
    fetcher, _ = make_fetcher(JOURNAL_PAGES)
    waits = []

    def fake_extract(pages, config, *, operation, feedback='', model=None, timeout=None):
        waits.append(timeout)
        return journal_extraction()
    monkeypatch.setattr(vd, 'extract_with_ai', fake_extract)
    run_rules(fetcher, extractor=None, time_limit_seconds=300)
    item.refresh_from_db()
    assert item.rules_status == 'ready'
    assert waits and all(w is not None and w <= 300 for w in waits)


def test_extract_with_ai_never_waits_longer_than_given(monkeypatch):
    seen = []

    def fake_chat(prompt, *, max_tokens, timeout, force_provider, operation):
        seen.append(timeout)
        return 'm', '{"name": "J"}'
    monkeypatch.setattr('review.services.ai_provider.ai_chat_json', fake_chat)
    config = vd.DiscoveryConfig.from_env()
    config.ai_provider = 'shared_qwen'
    vd.extract_with_ai([vd.FetchedPage(url='https://j.example', title='J', text='x')], config, timeout=45)
    assert seen and seen[0] <= 45


# ---------------------------------------------------------------------------
# 36
# ---------------------------------------------------------------------------

def test_the_deadline_is_the_date_next_to_the_deadline_words():
    today = date(2026, 10, 13)
    text = ('Special issue on AI governance\n'
            'Expected publication: 1 December 2027. Submission deadline: 15 March 2027.')
    page = vd.FetchedPage(url='https://j.example/cfp', title='Call for papers', text=text)
    calls = vd.detect_calls_for_papers([page], today=today)
    assert calls and calls[0]['deadline'] == '2027-03-15'


def test_a_single_future_date_is_still_found():
    today = date(2026, 10, 13)
    page = vd.FetchedPage(url='https://j.example/cfp', title='Call for papers',
                          text='Special issue on supply chains\nSubmissions close on 30 January 2027.')
    assert vd.detect_calls_for_papers([page], today=today)[0]['deadline'] == '2027-01-30'


# ---------------------------------------------------------------------------
# 37
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_force_never_closes_a_run_that_may_still_be_working():
    live = VenueIndexRun.objects.create(mode='rules', status='processing', trigger='manual')
    assert vi.force_close_runs() == 0
    live.refresh_from_db()
    assert live.status == 'processing'


@pytest.mark.django_db
def test_a_closed_run_is_not_reopened_by_its_worker():
    fetcher, _ = make_fetcher(JOURNAL_PAGES)
    journal()
    run, _created = vi.start_index_run(mode='rules', trigger='command')

    def close_it_meanwhile(*_a, **_k):
        VenueIndexRun.objects.filter(id=run.id).update(status='failed', summary='Closed with --force.')
        return journal_extraction()
    finished = vi.run_index(run, rules_fetcher=fetcher, rules_extractor=close_it_meanwhile)
    stored = VenueIndexRun.objects.get(id=run.id)
    assert stored.status == 'failed' and finished.status == 'failed'
    assert stored.rules_attempted == 1   # what it did is still saved


# ---------------------------------------------------------------------------
# 38
# ---------------------------------------------------------------------------

def test_429_is_busy_not_blocked():
    exc = vd.DiscoveryFetchError('HTTP 429 for https://j.example/.')
    assert vd.is_temporary_failure(exc) and not vd.is_blocked_failure(exc)
    assert vd.is_blocked_failure(vd.DiscoveryFetchError('HTTP 403 for https://j.example/.'))


@pytest.mark.parametrize('status', [429, 503])
def test_an_unreadable_robots_txt_stops_the_fetch_for_now(monkeypatch, status):
    def handler(request):
        if request.url.path == '/robots.txt':
            return httpx.Response(status, text='busy')
        return httpx.Response(200, text='<html><body>page</body></html>', headers={'content-type': 'text/html'})
    monkeypatch.setattr(vd, '_resolve', lambda host: {'93.184.216.34'})
    monkeypatch.setenv('VENUE_DISCOVERY_PER_DOMAIN_DELAY_SECONDS', '0')
    monkeypatch.setenv('VENUE_DISCOVERY_RETRY_DELAY_SECONDS', '0')
    config = vd.DiscoveryConfig.from_env()
    fetcher = vd.SafeFetcher(config, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(vd.DiscoveryFetchError) as caught:
        fetcher.fetch('https://j.example/guide')
    assert 'robots.txt could not be read' in str(caught.value)
    assert not vd.is_blocked_failure(caught.value)


def test_a_missing_robots_txt_still_allows_reading(monkeypatch):
    def handler(request):
        if request.url.path == '/robots.txt':
            return httpx.Response(404, text='none')
        return httpx.Response(200, text='<html><body>page text</body></html>', headers={'content-type': 'text/html'})
    monkeypatch.setattr(vd, '_resolve', lambda host: {'93.184.216.34'})
    monkeypatch.setenv('VENUE_DISCOVERY_PER_DOMAIN_DELAY_SECONDS', '0')
    fetcher = vd.SafeFetcher(vd.DiscoveryConfig.from_env(),
                             client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert 'page text' in fetcher.fetch('https://j.example/guide').text
