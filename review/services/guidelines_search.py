"""Find a journal's author-guidelines page when the index does not know its address.

The index reads rules from the DOAJ guidelines URL when it has one, and otherwise from the homepage
OpenAlex gives it. For a commercially published journal that homepage is the article-delivery host,
and Elsevier, SAGE, Wiley and INFORMS all answer 403 to our crawler. 26 October: 91 ABDC A* and A
journals read that way, none produced rules.

The pages themselves are public, and for a great many journals they are not on the publisher's site
at all. Journal of Management Information Systems keeps its rules on jmis-web.org, The Accounting
Review on aaahq.org, RAND Journal of Economics on rje.org, Strategic Management Journal on
strategicmanagement.net. All three of those hosts answered our own crawler with 200 when tested on
27 October. We fail on them only because we never ask.

A search fills that gap. The provider returns the page text along with the result, so the fetch
happens on its infrastructure rather than ours, which also covers journals whose rules are only on
a publisher host that refuses us.

Off unless TAVILY_API_KEY is set. When off, find_guidelines_pages returns nothing and the reading
path behaves exactly as before.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

SEARCH_URL = 'https://api.tavily.com/search'
# Words that mark a result as the guidelines page rather than the journal's front page or a
# third-party summary of it. A result carrying none of them is not worth the model's time.
MARKERS = ('author guidelines', 'guide for authors', 'submission guidelines', 'instructions for authors',
           'information for contributors', 'author instructions', 'manuscript preparation',
           'submission requirements')
# Publisher sites carry commerce and marketing pages that mention manuscripts in passing. California
# Management Review's held homepage redirected to sagepub.com/shop/subscribe-to-a-journal/..., which
# read cleanly and carried no rules. A result on one of these paths is not the guidelines page.
REJECT_PATHS = ('/shop/', '/subscribe', '/pricing', '/cart', '/basket', '/checkout', '/order',
                '/librarians', '/advertis', '/permissions', '/reprints')
MIN_TEXT = 400


def search_enabled():
    return bool(os.getenv('TAVILY_API_KEY', '').strip())


def _timeout():
    try:
        return max(5.0, float(os.getenv('GUIDELINES_SEARCH_TIMEOUT_SECONDS', '30')))
    except ValueError:
        return 30.0


def _max_results():
    try:
        return max(1, min(int(os.getenv('GUIDELINES_SEARCH_RESULTS', '4')), 10))
    except ValueError:
        return 4


def query_for(record):
    """What to ask. The publisher disambiguates titles that several journals share."""
    title = str(getattr(record, 'title', '') or '').strip()
    publisher = str(getattr(record, 'publisher', '') or '').strip()
    if not title:
        return ''
    return f'"{title}" {publisher} author guidelines submission requirements'.strip()


def _looks_like_guidelines(url, title, text):
    address = url.casefold()
    if any(path in address for path in REJECT_PATHS):
        return False
    haystack = f'{url}\n{title}\n{text[:4000]}'.casefold()
    return any(marker in haystack for marker in MARKERS)


def raw_search(query, *, api_key, timeout, max_results):
    """One provider call. Returns the provider's result list, or [] when it cannot be reached."""
    payload = {'api_key': api_key, 'query': query, 'max_results': max_results,
               'include_raw_content': True, 'search_depth': 'basic'}
    try:
        response = httpx.post(SEARCH_URL, json=payload, timeout=timeout)
        response.raise_for_status()
        body = response.json()
    except Exception as exc:  # a search failure must never stop the run
        logger.warning('Guidelines search failed for %r: %s', query[:80], exc)
        return []
    results = body.get('results')
    return results if isinstance(results, list) else []


def find_guidelines_pages(record, *, searcher=None, limit=None):
    """Pages that look like this journal's author guidelines, as venue_discovery FetchedPage objects.

    Returns [] when search is switched off, the query cannot be built, the provider cannot be
    reached, or nothing in the results reads like a guidelines page. The caller treats an empty
    list as "no new pages" and carries on.
    """
    from .venue_discovery import FetchedPage
    query = query_for(record)
    if not query:
        return []
    search = searcher
    if search is None:
        key = os.getenv('TAVILY_API_KEY', '').strip()
        if not key:
            return []
        max_results = limit or _max_results()
        timeout = _timeout()
        search = lambda q: raw_search(q, api_key=key, timeout=timeout, max_results=max_results)
    pages = []
    for result in search(query) or []:
        if not isinstance(result, dict):
            continue
        url = str(result.get('url') or '').strip()
        text = str(result.get('raw_content') or result.get('content') or '').strip()
        title = str(result.get('title') or '').strip()
        if not url or len(text) < MIN_TEXT:
            continue
        if not _looks_like_guidelines(url, title, text):
            continue
        pages.append(FetchedPage(url=url, title=title or record.title, text=text, links=[]))
    return pages
