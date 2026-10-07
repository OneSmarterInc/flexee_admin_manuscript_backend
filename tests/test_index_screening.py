"""Build plan step 3: exclusion screening, evidence, the admin review queue and reversible decisions."""
import json

import pytest
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from review.auth import COOKIE_NAME, issue_session
from review.models import (
    BlockedPublisher, DiscoveredVenue, EditorUser, IndexedVenue, IndexReviewDecision, Organization, Venue,
    VenueIndexRun,
)
from review.services import index_screening as scr
from review.services import venue_index as vi
from review.services.venue_discovery import FetchedPage, DiscoveryFetchError


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'screen-secret')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')
    monkeypatch.setenv('VENUE_INDEX_POLITE_DELAY', '0')


NOW = timezone.now()


def record(title='Journal of Information Systems Practice', **extra):
    base = dict(
        title=title, normalized_title=title.lower(), field_profile='business-is', issn_l='0048-7333',
        issns=['0048-7333'], publisher='Meridian Academic', homepage_url='https://jisp.example',
        first_publication_year=1995, subfields=[{'id': '1404', 'name': 'Management Information Systems', 'share': 0.8},
                                                {'id': '1408', 'name': 'Strategy and Management', 'share': 0.2}],
        metrics={'works_count': 900, 'is_core': True},
        crossref={'registered': True, 'total_dois': 900, 'first_year': 1996, 'checked_at': NOW.isoformat()},
        issn_checks={'valid_checksums': True, 'has_issn': True, 'crossref_agrees': True},
        openalex_id=f'S{abs(hash(title)) % 10**8}', enriched_at=NOW,
    )
    base.update(extra)
    return IndexedVenue.objects.create(**base)


def codes(item, kind='negative'):
    return {f['code'] for f in item.screening_flags if f['kind'] == kind}


def admin_client(superuser=True, email='root@example.com'):
    user = EditorUser.objects.create(email=email, password_hash='x', platform_superuser=superuser)
    client = Client()
    client.cookies[COOKIE_NAME] = issue_session(user.email)[0]
    return client


def post(client, url, data):
    return client.post(url, data=json.dumps(data), content_type='application/json')


# ---------------------------------------------------------------------------
# Catalogue signals
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_healthy_journal_is_clear_with_positive_signals():
    item = scr.screen_record(record(doaj_listed=True, doaj={'listed': True, 'journal_url': 'https://jisp.example'}), set())
    assert item.screening_status == 'clear' and item.screening_points == 0
    assert codes(item, 'positive') == {'doaj_listed', 'crossref_history', 'core_source', 'issn_confirmed'}


@pytest.mark.django_db
def test_no_crossref_and_unknown_publisher_go_to_review():
    item = scr.screen_record(record(publisher='', crossref={'registered': False, 'checked_at': NOW.isoformat()},
                                    metrics={}), set())
    assert codes(item) == {'no_crossref', 'publisher_unknown'} and item.screening_points == 3
    assert item.screening_status == 'flagged'


@pytest.mark.django_db
def test_no_crossref_alone_is_not_enough_to_flag():
    item = scr.screen_record(record(crossref={'registered': False, 'checked_at': NOW.isoformat()}), set())
    assert codes(item) == {'no_crossref'} and item.screening_status == 'clear'


@pytest.mark.django_db
@pytest.mark.parametrize('extra,code', [
    ({'issn_checks': {'valid_checksums': False, 'has_issn': True}}, 'issn_invalid'),
    ({'doaj': {'listed': True, 'publication_time_weeks': 2, 'review_url': 'https://x.example/review'}}, 'rapid_review'),
    ({'subfields': [{'id': '1404', 'name': 'MIS', 'share': 0.25}, {'id': '2705', 'name': 'Cardiology', 'share': 0.25},
                    {'id': '1203', 'name': 'Linguistics', 'share': 0.25}, {'id': '2205', 'name': 'Civil', 'share': 0.25}]},
     'overbroad_scope'),
    ({'first_publication_year': NOW.year, 'crossref': {'registered': True, 'first_year': NOW.year,
                                                       'checked_at': NOW.isoformat()}}, 'short_history'),
    ({'open_access': True, 'doaj_listed': False, 'apc_usd': 300}, 'apc_not_in_doaj'),
])
def test_each_catalogue_signal(extra, code):
    assert code in codes(scr.screen_record(record(**extra), set()))


@pytest.mark.django_db
def test_blocked_publisher_is_strong_evidence():
    BlockedPublisher.objects.create(name='Rapid Press', normalized_name='rapid press', added_by='root@example.com')
    item = scr.screen_record(record(publisher='Rapid Press'), scr.blocked_publisher_names())
    assert 'blocked_publisher' in codes(item) and item.screening_status == 'flagged'


# ---------------------------------------------------------------------------
# Evidence from the journal's own pages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('text,code', [
    ('We offer guaranteed acceptance for all quality papers.', 'guaranteed_acceptance'),
    ('Our journal: 100% publication for registered authors', 'guaranteed_acceptance'),
    ('Paper publication within 48 hours of payment!', 'rapid_review_promise'),
    ('Fast track publication in 7 days.', 'rapid_review_promise'),
    ('SJIF 2025: 7.21 | Global Impact Factor 1.4', 'invented_metrics'),
    ('Indexed with Index Copernicus Value (ICV): 92.4', 'invented_metrics'),
])
def test_page_phrases_are_found_with_exact_quotes(text, code):
    flags = scr.scan_page_text('https://bad.example/', 'Welcome. ' + text + ' Submit now.')
    assert [f['code'] for f in flags] == [code]
    assert flags[0]['evidence_url'] == 'https://bad.example/' and flags[0]['quote'] and flags[0]['weight'] == 3


@pytest.mark.parametrize('text', [
    'First decision within 14 days on average.',              # a decision is not acceptance
    'Journal Impact Factor 5.2 (Clarivate Journal Citation Reports).',
    'Accepted papers are published online first; the issue appears in 6 months.',
    'Indexed in Scopus, Web of Science and DOAJ.',
])
def test_ordinary_journal_wording_is_not_flagged(text):
    assert scr.scan_page_text('https://good.example/', text) == []


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.fetched = []

    def fetch(self, url):
        self.fetched.append(url)
        if url not in self.pages:
            raise DiscoveryFetchError(f'HTTP 404 for {url}.')
        body = self.pages[url]
        links = [('https://bad.example/author-guidelines', 'Author guidelines')] if url == 'https://bad.example/' else []
        return FetchedPage(url=url, title='', text=body, links=links)


@pytest.mark.django_db
def test_page_evidence_reads_home_and_author_pages():
    item = record(homepage_url='https://bad.example/')
    fetcher = FakeFetcher({'https://bad.example/': 'Welcome to our journal.',
                           'https://bad.example/author-guidelines': 'Publication within 3 days. SJIF 6.1'})
    flags, problem = scr.page_evidence(item, fetcher)
    assert problem == '' and {f['code'] for f in flags} == {'rapid_review_promise', 'invented_metrics'}
    assert all(f['evidence_url'] == 'https://bad.example/author-guidelines' for f in flags)


@pytest.mark.django_db
def test_gather_page_evidence_only_reads_candidates_and_flags_with_evidence():
    concern = record('Open Fee Journal', openalex_id='S1', homepage_url='https://bad.example/', open_access=True,
                     doaj_listed=False, apc_usd=200, screening_points=1)
    healthy = record('Healthy Journal', openalex_id='S2', homepage_url='https://good.example/')
    fetcher = FakeFetcher({'https://bad.example/': 'Guaranteed acceptance within the week.'})
    run = VenueIndexRun.objects.create(mode='screen')
    scr.gather_page_evidence(run, 'business-is', lambda: True, fetcher=fetcher)
    concern.refresh_from_db(); healthy.refresh_from_db()
    assert fetcher.fetched[0] == 'https://bad.example/' and 'https://good.example/' not in fetcher.fetched
    assert concern.screening_status == 'flagged' and 'guaranteed_acceptance' in codes(concern)
    assert concern.pages_checked_at and healthy.pages_checked_at is None and run.pages_checked == 1


@pytest.mark.django_db
def test_unreadable_site_is_noted_not_flagged():
    item = record('Quiet Journal', homepage_url='https://down.example/', open_access=True, apc_usd=100)
    run = VenueIndexRun.objects.create(mode='screen')
    scr.gather_page_evidence(run, 'business-is', lambda: True, fetcher=FakeFetcher({}))
    item.refresh_from_db()
    assert item.page_flags == [] and item.last_error.startswith('Pages not read') and item.pages_checked_at


# ---------------------------------------------------------------------------
# Decisions: a person decides, with criteria and evidence; reversible
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_exclusion_needs_criteria_and_evidence():
    item = record()
    with pytest.raises(scr.DecisionError, match='criterion'):
        scr.decide(item, decision='exclude', user_email='r@x', evidence_urls=['https://x.example'])
    with pytest.raises(scr.DecisionError, match='evidence'):
        scr.decide(item, decision='exclude', user_email='r@x', criteria=['invented_metrics'])
    with pytest.raises(scr.DecisionError, match='evidence'):
        scr.decide(item, decision='exclude', user_email='r@x', criteria=['invented_metrics'], evidence_urls=['javascript:x'])


@pytest.mark.django_db
def test_exclude_hides_the_linked_live_venue_and_keeps_a_record():
    org = Organization.objects.create(name='Org')
    venue = Venue.objects.create(organization=org, name='J', slug='j', venue_type='journal')
    item = record(venue=venue)
    scr.decide(item, decision='exclude', user_email='root@x', criteria=['invented_metrics', 'bogus'],
               evidence_urls=['https://bad.example/about'], note='SJIF badge on the homepage')
    item.refresh_from_db(); venue.refresh_from_db()
    assert item.excluded and item.screening_status == 'excluded'
    assert item.exclusion_reason['criteria'] == ['invented_metrics'] and item.exclusion_reason['decided_by'] == 'root@x'
    assert venue.excluded and venue.exclusion_reason['evidence_urls'] == ['https://bad.example/about']
    decision = IndexReviewDecision.objects.get()
    assert decision.decision == 'exclude' and decision.note == 'SJIF badge on the homepage'

    scr.decide(item, decision='restore', user_email='root@x', note='Badge removed')
    item.refresh_from_db(); venue.refresh_from_db()
    assert not item.excluded and item.screening_status == 'kept' and not venue.excluded
    assert IndexReviewDecision.objects.count() == 2  # the trail is never deleted


@pytest.mark.django_db
def test_kept_journal_returns_to_the_queue_only_for_new_concerns():
    item = scr.screen_record(record(publisher='', crossref={'registered': False, 'checked_at': NOW.isoformat()}), set())
    assert item.screening_status == 'flagged'
    scr.decide(item, decision='keep', user_email='r@x', note='Society journal, DOIs via JSTOR')
    item.refresh_from_db()
    assert scr.screen_record(item, set()).screening_status == 'kept'  # same concerns: stays kept
    item.page_flags = scr.scan_page_text('https://x.example/', 'Guaranteed acceptance!')
    item.save()
    assert scr.screen_record(item, set()).screening_status == 'flagged'  # something new


@pytest.mark.django_db
def test_exclusion_is_reversible_when_the_problem_disappears():
    item = record(page_flags=scr.scan_page_text('https://bad.example/', 'Guaranteed acceptance!'))
    scr.screen_record(item, set())
    scr.decide(item, decision='exclude', user_email='r@x', criteria=['guaranteed_acceptance'],
               evidence_urls=['https://bad.example/'])
    item.refresh_from_db()
    assert not scr.screen_record(item, set()).rereview_suggested  # still detected
    item.page_flags = []  # the site no longer makes the promise
    item.save()
    item = scr.screen_record(item, set())
    assert item.excluded and item.rereview_suggested
    assert scr.review_queue('business-is').filter(id=item.id).exists()


@pytest.mark.django_db
def test_blocking_a_publisher_sends_its_other_titles_to_review():
    one = record('Alpha Journal', openalex_id='S1', publisher='Rapid Press')
    two = record('Beta Journal', openalex_id='S2', publisher='Rapid Press')
    scr.screen_record(two, set())
    assert two.screening_status == 'clear'
    scr.decide(one, decision='exclude', user_email='r@x', criteria=['templated_site'],
               evidence_urls=['https://rapid.example/'], block_publisher=True)
    two.refresh_from_db()
    assert BlockedPublisher.objects.get().name == 'Rapid Press'
    assert two.screening_status == 'flagged' and not two.excluded  # flagged for a person, never auto-excluded


# ---------------------------------------------------------------------------
# Exclusions are never lost, and never quietly re-added
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_excluded_journals_survive_scope_changes(monkeypatch):
    from tests.test_venue_index import FakeHttp, source, topic
    monkeypatch.setenv('VENUE_INDEX_READ_PAGES', 'false')
    out_of_scope = source('S77', 'Ling Journal', issn_l='0000-0051', topics=[topic('1203', 'Language and Linguistics', 90)])
    item = record('Ling Journal', openalex_id='S77')
    scr.decide(item, decision='exclude', user_email='r@x', criteria=['not_peer_reviewed'], evidence_urls=['https://l.example'])
    run, _ = vi.start_index_run(trigger='command')
    vi.run_index(run, http=FakeHttp([[out_of_scope]]))
    assert IndexedVenue.objects.filter(openalex_id='S77', excluded=True).exists()


@pytest.mark.django_db
def test_discovery_cannot_re_add_an_excluded_journal_or_blocked_publisher():
    client = admin_client()
    item = record('Rapid Results Journal')
    scr.decide(item, decision='exclude', user_email='r@x', criteria=['guaranteed_acceptance'],
               evidence_urls=['https://rr.example'], block_publisher=True)
    same = DiscoveredVenue.objects.create(name='Rapid Results Journal', normalized_name='rapid results journal',
                                          venue_type='journal', acceptance_status='accepting')
    sibling = DiscoveredVenue.objects.create(name='Another Title', normalized_name='another title', venue_type='journal',
                                             organization_name='Meridian Academic', acceptance_status='accepting')
    for discovered in (same, sibling):
        response = client.post(f'/api/admin/venue-discovery/{discovered.id}/add-to-venue-agent/')
        assert response.status_code == 409 and response.json()['code'] == 'excluded_from_index'
    assert not Venue.objects.exists()


# ---------------------------------------------------------------------------
# Runs and the command
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_screen_run_and_command(monkeypatch, capsys):
    record('Fine Journal', openalex_id='S1')
    record('Odd Journal', openalex_id='S2', publisher='', crossref={'registered': False, 'checked_at': NOW.isoformat()})
    call_command('import_venue_index', '--screen-only', '--no-pages')
    out = capsys.readouterr().out
    assert 'Screened 2 journals: 1 need review' in out and '1 journals need review' in out
    run = VenueIndexRun.objects.get()
    assert run.mode == 'screen' and run.screened_count == 2 and run.flagged_count == 1 and run.enriched_count == 0


def test_openalex_daily_limit_has_a_clear_message():
    class R:
        status_code = 429
    http = vi.IndexHttp(vi.IndexConfig(), get=lambda url, **kw: R(), sleep=lambda s: None)
    with pytest.raises(vi.IndexSourceError, match='OPENALEX_API_KEY'):
        http.get_json('openalex', 'https://api.openalex.org/sources')


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_review_queue_api_and_decisions():
    flagged = scr.screen_record(record('Odd Journal', openalex_id='S2', publisher='',
                                       crossref={'registered': False, 'checked_at': NOW.isoformat()},
                                       page_flags=scr.scan_page_text('https://odd.example/', 'SJIF 2025: 7.21')), set())
    scr.screen_record(record('Fine Journal', openalex_id='S1'), set())
    client = admin_client()
    body = client.get('/api/admin/venue-index/?filter=review').json()
    assert body['filter_counts']['review'] == 1 and [i['title'] for i in body['items']] == ['Odd Journal']
    assert body['items'][0]['screening']['status'] == 'flagged' and 'Publisher not stated' in body['items'][0]['screening']['concerns']
    assert 'invented_metrics' in body['criteria']

    detail = client.get(f'/api/admin/venue-index/{flagged.id}/').json()['item']
    assert detail['suggested_criteria'] == ['invented_metrics']
    assert detail['suggested_evidence'][0] == 'https://odd.example/'

    bad = post(client, f'/api/admin/venue-index/{flagged.id}/decision/', {'decision': 'exclude', 'criteria': []})
    assert bad.status_code == 400 and 'criterion' in bad.json()['detail']
    ok = post(client, f'/api/admin/venue-index/{flagged.id}/decision/',
              {'decision': 'exclude', 'criteria': detail['suggested_criteria'], 'evidence_urls': detail['suggested_evidence'],
               'note': 'Fake metric on homepage', 'block_publisher': False})
    assert ok.status_code == 200 and ok.json()['item']['excluded'] and ok.json()['item']['decisions'][0]['decision'] == 'exclude'
    counts = client.get('/api/admin/venue-index/').json()['filter_counts']
    assert counts['excluded'] == 1 and counts['review'] == 0

    assert post(admin_client(superuser=False, email='ed@x.org'), f'/api/admin/venue-index/{flagged.id}/decision/',
                {'decision': 'restore'}).status_code == 403


@pytest.mark.django_db
def test_unblocking_a_publisher_clears_that_concern():
    item = record('Alpha Journal', publisher='Rapid Press')
    block = BlockedPublisher.objects.create(name='Rapid Press', normalized_name='rapid press', added_by='r@x')
    scr.screen_record(item, scr.blocked_publisher_names())
    assert item.screening_status == 'flagged'
    client = admin_client()
    assert client.get('/api/admin/venue-index/').json()['blocked_publishers'][0]['name'] == 'Rapid Press'
    assert client.delete(f'/api/admin/venue-index/blocked-publishers/{block.id}/').status_code == 200
    item.refresh_from_db()
    assert item.screening_status == 'clear' and 'blocked_publisher' not in codes(item)


@pytest.mark.django_db
def test_run_now_accepts_screen_mode():
    from unittest import mock
    client = admin_client()
    with mock.patch('django_q.tasks.async_task') as queued:
        response = post(client, '/api/admin/venue-index/run/', {'mode': 'screen'})
    assert response.status_code == 202 and response.json()['run']['mode'] == 'screen' and queued.called


# ---------------------------------------------------------------------------
# Parallel page reading: different websites at once, never two requests to one site
# ---------------------------------------------------------------------------

class SlowFetcher:
    """Each fetch takes 0.2 s; records how many requests run at once, per site and overall."""

    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.active = {}
        self.max_per_site = 0
        self.max_overall = 0

    def fetch(self, url):
        import time as _time
        from urllib.parse import urlsplit
        site = urlsplit(url).hostname
        with self.lock:
            self.active[site] = self.active.get(site, 0) + 1
            self.max_per_site = max(self.max_per_site, self.active[site])
            self.max_overall = max(self.max_overall, sum(self.active.values()))
        _time.sleep(0.2)
        with self.lock:
            self.active[site] -= 1
        return FetchedPage(url=url, title='', text='Nothing unusual here.', links=[])


@pytest.mark.django_db
def test_pages_are_read_in_parallel_but_one_request_per_site(monkeypatch):
    import time as _time
    monkeypatch.setenv('VENUE_INDEX_PAGE_WORKERS', '6')
    monkeypatch.setenv('VENUE_INDEX_PAGE_DELAY_SECONDS', '0')
    for i in range(12):  # 12 journals on 12 different sites
        record(f'Site Journal {i}', openalex_id=f'S{i}', homepage_url=f'https://site{i}.example/', screening_points=1)
    for i in range(4):   # 4 journals on one publisher's site
        record(f'Publisher Journal {i}', openalex_id=f'P{i}', homepage_url=f'https://www.bigpress.example/j{i}',
               screening_points=1)
    fetcher = SlowFetcher()
    run = VenueIndexRun.objects.create(mode='screen')
    started = _time.monotonic()
    scr.gather_page_evidence(run, 'business-is', lambda: True, fetcher=fetcher)
    elapsed = _time.monotonic() - started
    assert run.pages_checked == 16 and IndexedVenue.objects.filter(pages_checked_at__isnull=False).count() == 16
    assert fetcher.max_per_site == 1           # polite: never two at once on one site
    assert fetcher.max_overall >= 4            # but different sites in parallel
    assert elapsed < 16 * 0.2 * 0.6            # well under the one-at-a-time time (3.2 s)


@pytest.mark.django_db
def test_time_limit_stops_taking_new_sites(monkeypatch):
    monkeypatch.setenv('VENUE_INDEX_PAGE_WORKERS', '2')
    for i in range(10):
        record(f'Site Journal {i}', openalex_id=f'S{i}', homepage_url=f'https://site{i}.example/', screening_points=1)
    calls = iter([True] * 3 + [False] * 1000)
    run = VenueIndexRun.objects.create(mode='screen')
    scr.gather_page_evidence(run, 'business-is', lambda: next(calls), fetcher=SlowFetcher())
    assert 1 <= run.pages_checked < 10  # what was started finishes and is saved; the rest waits for the next run
    assert IndexedVenue.objects.filter(pages_checked_at__isnull=True).count() == 10 - run.pages_checked
