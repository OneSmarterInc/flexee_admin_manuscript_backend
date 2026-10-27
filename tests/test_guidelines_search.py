"""Finding the guidelines page when the index holds the wrong address (27 October).

The index reads the DOAJ guidelines URL or the OpenAlex homepage. For a commercially published
journal that homepage is the article host, and Elsevier, SAGE, Wiley and INFORMS answer 403 there:
91 A* and A journals were read that way on 26 October and none produced rules. The same journals'
guidelines often sit on a society site that answers our own crawler with 200, and a search finds
them. The provider returns the page text with the result, so the fetch happens on its side.
"""
import pytest

from review.models import DiscoveredVenue, IndexedVenue, Venue
from review.services import guidelines_search as gs
from review.services import index_rules as ir
from review.services import venue_discovery as vd
from tests.test_index_rules import HOME, journal, run_rules
from tests.test_venue_discovery import JOURNAL_PAGES, journal_extraction, make_fetcher


@pytest.fixture(autouse=True)
def env(monkeypatch):
    """Same isolation as the rules tests, plus a guarantee that no real key is ever used here."""
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'rules-secret')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')
    monkeypatch.setenv('VENUE_DISCOVERY_PER_DOMAIN_DELAY_SECONDS', '0')
    monkeypatch.setenv('VENUE_DISCOVERY_RETRY_DELAY_SECONDS', '0')
    monkeypatch.setattr(vd, '_resolve', lambda host: {'93.184.216.34'})
    monkeypatch.delenv('TAVILY_API_KEY', raising=False)


class Rec:
    def __init__(self, title='A Journal', publisher='A Publisher'):
        self.title, self.publisher = title, publisher


def test_search_is_off_without_a_key(monkeypatch):
    monkeypatch.delenv('TAVILY_API_KEY', raising=False)
    assert gs.search_enabled() is False
    assert gs.find_guidelines_pages(Rec()) == []


def test_the_query_names_the_journal_and_its_publisher():
    q = gs.query_for(Rec('Journal of Management Information Systems', 'Taylor & Francis'))
    assert '"Journal of Management Information Systems"' in q
    assert 'Taylor & Francis' in q and 'author guidelines' in q


def test_a_journal_with_no_title_is_not_searched_for():
    assert gs.query_for(Rec('', 'Elsevier')) == ''
    assert gs.find_guidelines_pages(Rec('', 'Elsevier'), searcher=lambda q: [], limit=3) == []


def test_only_results_that_read_like_guidelines_are_kept():
    """A journal's front page and a third-party summary are not worth the model's time."""
    results = [
        {'url': 'https://example.org/journal/home', 'title': 'A Journal', 'raw_content': 'Latest issue. ' * 60},
        {'url': 'https://example.org/journal/authors', 'title': 'Author Guidelines',
         'raw_content': 'Author guidelines. Research articles up to 8,000 words. ' * 20},
    ]
    pages = gs.find_guidelines_pages(Rec(), searcher=lambda q: results)
    assert [p.url for p in pages] == ['https://example.org/journal/authors']


def test_a_result_with_almost_no_text_is_dropped():
    results = [{'url': 'https://example.org/authors', 'title': 'Author Guidelines', 'raw_content': 'Guidelines.'}]
    assert gs.find_guidelines_pages(Rec(), searcher=lambda q: results) == []


def test_raw_content_is_preferred_over_the_snippet():
    long_snippet = 'submission guidelines summary. ' * 20
    full = 'Author guidelines in full. Manuscripts up to 30 pages. ' * 20
    results = [{'url': 'https://example.org/authors', 'title': 'Guide for authors',
                'content': long_snippet, 'raw_content': full}]
    page = gs.find_guidelines_pages(Rec(), searcher=lambda q: results)[0]
    assert page.text.startswith('Author guidelines in full')


def test_a_provider_that_errors_does_not_stop_the_caller(monkeypatch):
    def boom(url, **kwargs):
        raise RuntimeError('provider down')
    monkeypatch.setattr(gs.httpx, 'post', boom)
    assert gs.raw_search('anything', api_key='k', timeout=5, max_results=3) == []


def test_a_malformed_provider_response_is_ignored(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {'results': 'not a list'}
    monkeypatch.setattr(gs.httpx, 'post', lambda url, **kwargs: Response())
    assert gs.raw_search('anything', api_key='k', timeout=5, max_results=3) == []


@pytest.mark.django_db
def test_a_searched_page_is_read_when_the_held_address_fails(monkeypatch):
    """The journal's own address refuses us; the search finds the society page and the rules land."""
    record = journal('Journal of Applied AI in Organizations', openalex_id='S1',
                     homepage='https://blocked.example/journal')

    fetcher, config = make_fetcher(JOURNAL_PAGES)
    # The same pages the normal path would have read, but delivered by the search instead.
    found = vd.gather_pages(HOME, fetcher, config)

    def refuse(url, fetcher, config):
        raise vd.DiscoveryFetchError('HTTP 403 for https://blocked.example/journal')
    monkeypatch.setattr(vd, 'gather_pages', refuse)
    monkeypatch.setattr('review.services.guidelines_search.find_guidelines_pages', lambda rec, **kw: found)
    run_rules(fetcher)
    record.refresh_from_db()
    assert record.rules_status == 'ready'
    assert record.discovered is not None


@pytest.mark.django_db
def test_nothing_changes_when_the_search_finds_nothing(monkeypatch):
    record = journal('Journal of Applied AI in Organizations', openalex_id='S1',
                     homepage='https://blocked.example/journal')

    def refuse(url, fetcher, config):
        raise vd.DiscoveryFetchError('HTTP 403 for https://blocked.example/journal')
    monkeypatch.setattr(vd, 'gather_pages', refuse)
    monkeypatch.setattr('review.services.guidelines_search.find_guidelines_pages', lambda rec, **kw: [])
    fetcher, _config = make_fetcher(JOURNAL_PAGES)
    run_rules(fetcher)
    record.refresh_from_db()
    assert record.rules_status == 'blocked'
    assert 'blocked.example' in record.rules_error


def test_a_shop_or_subscription_page_is_never_the_guidelines():
    """California Management Review's held homepage redirected to a subscribe page.

    It fetched cleanly, carried no rules, and the model returned nothing. A result on a commerce
    path is rejected before the model is asked to look at it, however it is titled.
    """
    results = [{'url': 'https://www.sagepub.com/shop/subscribe-to-a-journal/california-management-review-202706',
                'title': 'Subscribe: California Management Review',
                'raw_content': 'Submission guidelines for authors. Manuscript preparation. ' * 20}]
    assert gs.find_guidelines_pages(Rec(), searcher=lambda q: results) == []


@pytest.mark.django_db
def test_a_page_that_reads_cleanly_but_holds_no_rules_still_triggers_a_search(monkeypatch):
    """The common failure is the wrong page, not a failed fetch.

    Searching only when the fetch fails would never have fired for California Management Review,
    whose subscribe page was read successfully and produced nothing.
    """
    record = journal('Journal of Applied AI in Organizations', openalex_id='S1',
                     homepage='https://marketing.example/journal')
    fetcher, config = make_fetcher(JOURNAL_PAGES)
    good = vd.gather_pages(HOME, fetcher, config)
    empty = [vd.FetchedPage(url='https://marketing.example/journal', title='Subscribe',
                            text='Subscribe to this journal today. ' * 40, links=[])]

    monkeypatch.setattr(vd, 'gather_pages', lambda url, f, c: empty)
    calls = []

    def search(rec, **kw):
        calls.append(rec.title)
        return good
    monkeypatch.setattr('review.services.guidelines_search.find_guidelines_pages', search)
    run_rules(fetcher)
    record.refresh_from_db()
    assert calls, 'the search should have run after a readable page produced no rules'
    assert record.rules_status == 'ready'


@pytest.mark.django_db
def test_a_search_that_finds_nothing_leaves_the_original_reading_alone(monkeypatch):
    record = journal('Journal of Applied AI in Organizations', openalex_id='S1')
    fetcher, _config = make_fetcher(JOURNAL_PAGES)
    monkeypatch.setattr('review.services.guidelines_search.find_guidelines_pages', lambda rec, **kw: [])
    run_rules(fetcher)
    record.refresh_from_db()
    assert record.rules_status == 'ready'  # the held address worked; the search changed nothing
