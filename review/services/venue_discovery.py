"""Daily venue discovery.

Finds journals, book publishers and conferences on the public web, verifies them
against their official author/submission pages, extracts their rules with the
configured AI provider, and stages them as DiscoveredVenue records. Nothing here
is visible to authors: only the admin's one-click "Add to Venue Agent" creates a
live Venue + VenueAgentConfig (see review/discovery_api.py).

Only public venue web pages are processed. No manuscript, author, credential or
other private data is ever sent to the search provider or the AI.
"""
import hashlib
import ipaddress
import json
import os
import re
import socket
import time
from datetime import timedelta
import urllib.robotparser
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from django.db import transaction
from django.utils import timezone

from ..models import DiscoveredVenue, VenueDiscoveryRun


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_bool(name, default=False):
    return os.getenv(name, 'true' if default else 'false').strip().lower() in {'1', 'true', 'yes', 'on'}


def _env_int(name, default, low=1, high=10_000):
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(value, high))


@dataclass
class DiscoveryConfig:
    enabled: bool
    mode: str
    provider: str
    api_key: str
    ai_provider: str
    max_queries_per_run: int
    max_results_per_query: int
    max_candidates_per_run: int
    max_pages_per_run: int
    max_pages_per_candidate: int
    max_rechecks_per_run: int
    recheck_after_hours: int
    http_timeout: float
    page_max_bytes: int
    max_text_chars_per_page: int
    max_ai_chars: int
    max_redirects: int
    per_domain_delay: float
    user_agent: str

    @classmethod
    def from_env(cls):
        return cls(
            enabled=_env_bool('VENUE_DISCOVERY_ENABLED', False),
            # claude_agent (default): Claude searches and reads pages with Anthropic's web tools.
            # search_api: fixed queries through a search API (Tavily) + our own safe fetcher.
            mode=os.getenv('VENUE_DISCOVERY_MODE', 'claude_agent').strip().lower(),
            provider=os.getenv('VENUE_SEARCH_PROVIDER', 'tavily').strip().lower(),
            api_key=os.getenv('VENUE_SEARCH_API_KEY', '').strip(),
            ai_provider=os.getenv('VENUE_DISCOVERY_AI_PROVIDER', '').strip().lower(),
            max_queries_per_run=_env_int('VENUE_DISCOVERY_MAX_QUERIES_PER_RUN', 16, 1, 100),
            max_results_per_query=_env_int('VENUE_DISCOVERY_MAX_RESULTS_PER_QUERY', 10, 1, 20),
            max_candidates_per_run=_env_int('VENUE_DISCOVERY_MAX_CANDIDATES_PER_RUN', 40, 1, 500),
            max_pages_per_run=_env_int('VENUE_DISCOVERY_MAX_PAGES_PER_RUN', 100, 1, 2000),
            max_pages_per_candidate=_env_int('VENUE_DISCOVERY_MAX_PAGES_PER_CANDIDATE', 3, 1, 10),
            max_rechecks_per_run=_env_int('VENUE_DISCOVERY_MAX_RECHECKS_PER_RUN', 25, 0, 500),
            recheck_after_hours=_env_int('VENUE_DISCOVERY_RECHECK_AFTER_HOURS', 20, 1, 24 * 60),
            http_timeout=float(_env_int('VENUE_DISCOVERY_HTTP_TIMEOUT_SECONDS', 15, 1, 120)),
            page_max_bytes=_env_int('VENUE_DISCOVERY_PAGE_MAX_BYTES', 2_000_000, 10_000, 20_000_000),
            max_text_chars_per_page=_env_int('VENUE_DISCOVERY_MAX_TEXT_CHARS_PER_PAGE', 12_000, 1_000, 100_000),
            max_ai_chars=_env_int('VENUE_DISCOVERY_MAX_AI_CHARS', 30_000, 2_000, 200_000),
            max_redirects=_env_int('VENUE_DISCOVERY_MAX_REDIRECTS', 4, 0, 10),
            per_domain_delay=float(os.getenv('VENUE_DISCOVERY_PER_DOMAIN_DELAY_SECONDS', '1.0') or 0),
            user_agent=os.getenv('VENUE_DISCOVERY_USER_AGENT', 'FlexeeVenueDiscovery/1.0 (+https://www.flexee.org)').strip(),
        )


class DiscoveryConfigError(Exception):
    """Discovery cannot run with the current configuration (disabled, missing key, ...)."""


class DiscoveryFetchError(Exception):
    """A page could not be fetched safely."""


class DiscoveryExtractionError(Exception):
    """The AI output could not be turned into a valid candidate."""


# ---------------------------------------------------------------------------
# Query bank (one place to change what we search for)
# ---------------------------------------------------------------------------

QUERY_BANK = {
    'journal': [
        'academic journal submit manuscript author guidelines',
        'journal accepting research articles submit manuscript',
        'research journal submissions open instructions for authors',
        'journal author guidelines submit manuscript information systems',
        'journal call for papers special issue submit manuscript',
        '"submit manuscript" journal management',
    ],
    'publisher': [
        'academic publisher submit book proposal',
        'submit textbook proposal publisher',
        'professional book proposal academic publisher guidelines',
        'publisher accepting book proposals business technology',
        '"submit a book proposal" academic press',
    ],
    'conference': [
        'conference call for papers submit paper information systems',
        'academic conference paper submission deadline',
        'conference accepting papers education technology',
    ],
}


def query_bank():
    queries = []
    for category in ('journal', 'publisher', 'conference'):
        for query in QUERY_BANK[category]:
            queries.append((category, query))
    return queries


# ---------------------------------------------------------------------------
# Search providers
# ---------------------------------------------------------------------------

@dataclass
class SearchResult:
    url: str
    title: str = ''
    snippet: str = ''


class VenueSearchProvider:
    name = 'base'

    def search(self, query, *, max_results=10):  # pragma: no cover - interface
        raise NotImplementedError


class TavilySearchProvider(VenueSearchProvider):
    name = 'tavily'
    endpoint = 'https://api.tavily.com/search'

    def __init__(self, api_key, *, timeout=15.0):
        if not api_key:
            raise DiscoveryConfigError('VENUE_SEARCH_API_KEY is not set, so the search provider cannot be called.')
        self.api_key = api_key
        self.timeout = timeout

    def search(self, query, *, max_results=10):
        response = httpx.post(
            self.endpoint,
            headers={'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json'},
            json={'query': query, 'max_results': max_results, 'search_depth': 'basic', 'include_answer': False},
            timeout=self.timeout,
        )
        if response.status_code == 429:
            raise DiscoveryFetchError('Search provider rate limit reached.')
        if response.status_code >= 400:
            raise DiscoveryFetchError(f'Search provider returned HTTP {response.status_code}.')
        payload = response.json()
        results = []
        for item in payload.get('results', [])[:max_results]:
            url = str(item.get('url', '')).strip()
            if url:
                results.append(SearchResult(url=url, title=str(item.get('title', ''))[:300],
                                            snippet=str(item.get('content', ''))[:600]))
        return results


SEARCH_PROVIDERS = {'tavily': TavilySearchProvider}


def get_search_provider(config):
    provider_class = SEARCH_PROVIDERS.get(config.provider)
    if not provider_class:
        raise DiscoveryConfigError(
            f'Unknown VENUE_SEARCH_PROVIDER {config.provider!r}. Supported: {", ".join(sorted(SEARCH_PROVIDERS))}.'
        )
    return provider_class(config.api_key, timeout=config.http_timeout)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

TRACKING_PARAMS = {'gclid', 'fbclid', 'mc_cid', 'mc_eid', 'ref', 'ref_src', 'igshid', 'msclkid', 'yclid'}
MULTI_PART_SUFFIXES = {
    'co.uk', 'ac.uk', 'org.uk', 'gov.uk', 'com.au', 'edu.au', 'org.au', 'co.in', 'ac.in', 'co.jp', 'ac.jp',
    'com.br', 'co.nz', 'ac.nz', 'com.cn', 'edu.cn', 'co.za', 'ac.za', 'com.sg', 'edu.sg',
}

# Aggregators, social networks and blogs are never treated as official sources.
THIRD_PARTY_DOMAINS = {
    'wikipedia.org', 'researchgate.net', 'scimagojr.com', 'medium.com', 'blogspot.com', 'wordpress.com',
    'linkedin.com', 'facebook.com', 'twitter.com', 'x.com', 'reddit.com', 'quora.com', 'youtube.com',
    'wikicfp.com', 'call4paper.com', 'conferenceindex.org', 'allconferencealert.com', 'scholar.google.com',
    'google.com', 'bing.com', 'substack.com', 'academia.edu',
}


def canonical_host(url):
    host = (urlsplit(url).hostname or '').lower().rstrip('.')
    return host[4:] if host.startswith('www.') else host


def registrable_domain(host):
    parts = [p for p in host.lower().split('.') if p]
    if len(parts) >= 3 and '.'.join(parts[-2:]) in MULTI_PART_SUFFIXES:
        return '.'.join(parts[-3:])
    return '.'.join(parts[-2:]) if len(parts) >= 2 else host


def is_third_party(url):
    domain = registrable_domain(canonical_host(url))
    return domain in THIRD_PARTY_DOMAINS


def canonical_url(url):
    """Lower-case host, drop www., fragments, tracking parameters and trailing slash."""
    if not url:
        return ''
    parts = urlsplit(url.strip())
    if parts.scheme not in {'http', 'https'}:
        return ''
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in TRACKING_PARAMS]
    path = re.sub(r'/+$', '', parts.path or '') or ''
    return urlunsplit(('https', canonical_host(url), path, urlencode(query), ''))


def normalize_name(value):
    text = re.sub(r'[^a-z0-9]+', ' ', str(value or '').lower()).strip()
    text = re.sub(r'^the\s+', '', text)
    return re.sub(r'\s+', ' ', text)[:300]


# ---------------------------------------------------------------------------
# Safe fetching (SSRF protection)
# ---------------------------------------------------------------------------

BLOCKED_HOSTNAMES = {'localhost', 'localhost.localdomain', 'metadata.google.internal', 'metadata'}
CGNAT = ipaddress.ip_network('100.64.0.0/10')
ALLOWED_CONTENT_TYPES = ('text/html', 'application/xhtml+xml', 'text/plain')


def _resolve(host):
    return {info[4][0] for info in socket.getaddrinfo(host, None)}


def _ip_is_public(ip_text):
    ip = ipaddress.ip_address(ip_text.split('%')[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
        or ip.is_unspecified or (isinstance(ip, ipaddress.IPv4Address) and ip in CGNAT)
    )


def validate_public_url(url):
    """Raise DiscoveryFetchError unless url is an http(s) URL on a public address."""
    parts = urlsplit(str(url or ''))
    if parts.scheme not in {'http', 'https'}:
        raise DiscoveryFetchError(f'Blocked non-web URL scheme: {parts.scheme or "none"}.')
    host = (parts.hostname or '').lower().rstrip('.')
    if not host:
        raise DiscoveryFetchError('Blocked URL without a host.')
    if host in BLOCKED_HOSTNAMES or host.endswith(('.localhost', '.local', '.internal', '.lan', '.home.arpa')):
        raise DiscoveryFetchError(f'Blocked internal host: {host}.')
    if parts.username or parts.password:
        raise DiscoveryFetchError('Blocked URL with embedded credentials.')
    try:
        addresses = {host} if _looks_like_ip(host) else _resolve(host)
    except socket.gaierror as exc:
        raise DiscoveryFetchError(f'Could not resolve {host}.') from exc
    for address in addresses:
        if not _ip_is_public(address):
            raise DiscoveryFetchError(f'Blocked private or internal address for {host}.')
    return url


def _looks_like_ip(host):
    try:
        ipaddress.ip_address(host.strip('[]'))
        return True
    except ValueError:
        return False


@dataclass
class FetchedPage:
    url: str
    title: str
    text: str
    links: list = field(default_factory=list)


class SafeFetcher:
    """Fetches public pages with redirect re-validation, size/time limits and robots.txt."""

    def __init__(self, config, client=None):
        self.config = config
        self.client = client or httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(config.http_timeout, connect=min(config.http_timeout, 10.0)),
            headers={'User-Agent': config.user_agent, 'Accept': 'text/html,application/xhtml+xml,text/plain;q=0.9'},
        )
        self._robots = {}
        self._last_hit = {}
        self.pages_fetched = 0

    def _throttle(self, host):
        if self.config.per_domain_delay <= 0:
            return
        wait = self._last_hit.get(host, 0) + self.config.per_domain_delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def _allowed_by_robots(self, url):
        parts = urlsplit(url)
        origin = f'{parts.scheme}://{parts.netloc}'
        if origin not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            try:
                body, _ = self._get(origin + '/robots.txt', accept_any=True, max_bytes=200_000)
                parser.parse(body.splitlines())
            except DiscoveryFetchError:
                parser.parse([])  # no robots.txt reachable: treat as allowed
            self._robots[origin] = parser
        return self._robots[origin].can_fetch(self.config.user_agent, url)

    def _get(self, url, *, accept_any=False, max_bytes=None):
        max_bytes = max_bytes or self.config.page_max_bytes
        current = url
        for _ in range(self.config.max_redirects + 1):
            validate_public_url(current)
            self._throttle(canonical_host(current))
            try:
                with self.client.stream('GET', current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get('location')
                        if not location:
                            raise DiscoveryFetchError('Redirect without a location.')
                        current = urljoin(current, location)
                        continue
                    if response.status_code >= 400:
                        raise DiscoveryFetchError(f'HTTP {response.status_code} for {current}.')
                    content_type = response.headers.get('content-type', '').split(';')[0].strip().lower()
                    if not accept_any and content_type not in ALLOWED_CONTENT_TYPES:
                        raise DiscoveryFetchError(f'Skipped non-HTML content ({content_type or "unknown"}).')
                    declared = response.headers.get('content-length')
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise DiscoveryFetchError('Page is larger than the configured limit.')
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > max_bytes:
                            raise DiscoveryFetchError('Page is larger than the configured limit.')
                        chunks.append(chunk)
                    encoding = response.encoding or 'utf-8'
                    return b''.join(chunks).decode(encoding, errors='replace'), current
            except httpx.TimeoutException as exc:
                raise DiscoveryFetchError(f'Timed out fetching {current}.') from exc
            except httpx.HTTPError as exc:
                raise DiscoveryFetchError(f'Could not fetch {current}.') from exc
        raise DiscoveryFetchError('Too many redirects.')

    def fetch(self, url):
        if self.pages_fetched >= self.config.max_pages_per_run:
            raise DiscoveryFetchError('Page budget for this run is used up.')
        validate_public_url(url)
        if not self._allowed_by_robots(url):
            raise DiscoveryFetchError(f'robots.txt does not allow fetching {url}.')
        body, final_url = self._get(url)
        self.pages_fetched += 1
        parsed = parse_html(body, final_url)
        parsed.text = parsed.text[: self.config.max_text_chars_per_page]
        return parsed


# ---------------------------------------------------------------------------
# HTML parsing (standard library only)
# ---------------------------------------------------------------------------

SKIP_TAGS = {'script', 'style', 'noscript', 'svg', 'template', 'nav', 'footer', 'header', 'form', 'iframe', 'canvas'}
BLOCK_TAGS = {'p', 'div', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'section', 'article', 'br', 'td', 'th', 'dd', 'dt'}


class _TextExtractor(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.skip_depth = 0
        self.in_title = False
        self.title_parts, self.text_parts, self.links = [], [], []
        self._link_href, self._link_text = None, []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip_depth += 1
        if tag == 'title':
            self.in_title = True
        if tag in BLOCK_TAGS:
            self.text_parts.append('\n')
        if tag == 'a' and not self.skip_depth:
            href = dict(attrs).get('href')
            if href:
                self._link_href, self._link_text = urljoin(self.base_url, href), []

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        if tag == 'title':
            self.in_title = False
        if tag == 'a' and self._link_href:
            self.links.append((self._link_href, ' '.join(self._link_text).strip()))
            self._link_href = None

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
            return
        if self.skip_depth:
            return
        self.text_parts.append(data)
        if self._link_href is not None:
            self._link_text.append(data.strip())


def parse_html(body, url):
    extractor = _TextExtractor(url)
    try:
        extractor.feed(body)
        extractor.close()
    except Exception:  # malformed markup: keep whatever was parsed
        pass
    text = ''.join(extractor.text_parts)
    text = re.sub(r'[ \t\r\f\v]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n', text).strip()
    title = re.sub(r'\s+', ' ', ''.join(extractor.title_parts)).strip()[:300]
    return FetchedPage(url=url, title=title, text=text, links=extractor.links[:400])


SUBMISSION_KEYWORDS = (
    'author guideline', 'guide for author', 'instructions for author', 'information for author', 'for authors',
    'submit', 'submission', 'call for paper', 'book proposal', 'publish with us', 'publish a book', 'write for us',
    'proposal guideline',
)


def looks_like_submission_page(url, title='', text=''):
    haystack = f'{url} {title} {text[:400]}'.lower()
    return any(keyword in haystack for keyword in SUBMISSION_KEYWORDS)


def find_submission_links(page, limit):
    """Same-site links whose text or URL suggests author or submission information."""
    site = registrable_domain(canonical_host(page.url))
    seen, picked = {canonical_url(page.url)}, []
    for href, text in page.links:
        if urlsplit(href).scheme not in {'http', 'https'}:
            continue
        if registrable_domain(canonical_host(href)) != site:
            continue
        key = canonical_url(href)
        if not key or key in seen:
            continue
        if looks_like_submission_page(href, text):
            seen.add(key)
            picked.append(href)
        if len(picked) >= limit:
            break
    return picked


# ---------------------------------------------------------------------------
# Type mapping
# ---------------------------------------------------------------------------

SUBMISSION_TYPES = ['research_article', 'practitioner_article', 'review_article', 'case_study', 'conference_paper', 'book', 'other']
TYPE_LABELS = {
    'research_article': 'Research article',
    'practitioner_article': 'Practitioner article',
    'review_article': 'Review article',
    'case_study': 'Case study',
    'conference_paper': 'Conference paper',
    'book': 'Book manuscript',
    'other': 'Other',
}
_TYPE_PATTERNS = [
    ('book', r'\b(book|monograph|textbook|edited volume|proposal)\b'),
    ('conference_paper', r'\b(conference|proceedings|symposium|workshop) paper|\bfull paper|\bshort paper'),
    ('review_article', r'\b(review|survey|meta.?analysis)\b'),
    ('case_study', r'\bcase (study|studies|report)\b|\bteaching case'),
    ('practitioner_article', r'\b(practitioner|practice|industry|professional|perspective|viewpoint|field note)'),
    ('research_article', r'\b(research|original|empirical|article|paper|study)\b'),
]


def normalize_submission_type(label):
    text = str(label or '').strip().lower().replace('-', ' ').replace('_', ' ')
    if not text:
        return None
    if text.replace(' ', '_') in SUBMISSION_TYPES:
        return text.replace(' ', '_')
    for type_key, pattern in _TYPE_PATTERNS:
        if re.search(pattern, text):
            return type_key
    return 'other'


# ---------------------------------------------------------------------------
# AI extraction
# ---------------------------------------------------------------------------

EXTRACTION_SCHEMA_HINT = {
    'name': 'string', 'organization_name': 'string', 'venue_type': 'journal | conference | publisher',
    'acceptance_status': 'accepting | unclear | closed', 'website_url': 'url', 'submission_url': 'url or empty',
    'submission_types': ['labels such as Research article, Review article, Case study, Book proposal'],
    'description': 'one or two sentences', 'aims_scope': 'string', 'article_types': ['string'],
    'accepted_methods': ['string'], 'quality_threshold': 'string', 'reviewer_criteria': ['string'],
    'policies': {'key': 'value'}, 'disclosures': ['string'], 'reporting_standards': ['string'],
    'desk_rejection_rules': ['string'],
    'structured_desk_rejection_rules': [{'field': 'word_count | reference_count | required_sections',
                                         'operator': '<= | >= | < | > | missing_any', 'value': 'number or list',
                                         'message': 'string'}],
    'required_submission_items': [{'label': 'string', 'type': 'text | textarea | url | checkbox | file',
                                   'required': 'true only if the source says it is required', 'help_text': 'string'}],
    'retention_days': 'integer or null', 'deadlines': {'name': 'YYYY-MM-DD'}, 'submission_capacity': {},
    'current_demand': {}, 'config_notes': 'string', 'conflicts': ['contradictions between official pages'],
    'source_evidence': [{'field': 'string', 'claim': 'string', 'url': 'one of the page URLs',
                         'source_title': 'string', 'evidence_text': 'short exact quote from that page'}],
}


def build_extraction_prompt(pages, config):
    budget = config.max_ai_chars
    blocks = []
    for page in pages:
        if budget <= 0:
            break
        text = page.text[: min(len(page.text), budget)]
        budget -= len(text)
        blocks.append(f'=== PAGE ===\nURL: {page.url}\nTITLE: {page.title}\n{text}')
    return (
        'You extract submission information about ONE academic venue (journal, book publisher or conference) '
        'from its official web pages. Use ONLY the page text below. Never guess.\n'
        'Rules:\n'
        '- If something is not stated, use "" for text, [] for lists, {} for objects and null for numbers.\n'
        '- acceptance_status is "accepting" only if a page shows an open way to submit now; "closed" if a page '
        'says submissions are closed or suspended; otherwise "unclear".\n'
        '- structured_desk_rejection_rules: only explicit, objective limits (e.g. maximum word count, required '
        'named sections). Never turn subjective wording into a rule.\n'
        '- required_submission_items: mark required=true only when the page says it is required.\n'
        '- For book publishers, include proposal details in policies, e.g. '
        '{"book_submission_stage": "proposal", "accepted_book_types": [...]}.\n'
        '- source_evidence: give evidence for acceptance status, accepted types, scope, required items, word '
        'limits, deadlines and desk-rejection conditions. evidence_text must be a short EXACT quote copied from '
        'the page whose URL you give.\n'
        'Return ONLY a JSON object with this shape:\n'
        + json.dumps(EXTRACTION_SCHEMA_HINT)
        + '\n\n' + '\n\n'.join(blocks)
    )


def extract_with_ai(pages, config):
    from .ai_provider import ai_chat_json
    prompt = build_extraction_prompt(pages, config)
    last_error = None
    for _attempt in range(2):
        try:
            _model, raw = ai_chat_json(
                prompt,
                max_tokens=2200,
                timeout=180,
                force_provider=config.ai_provider or None,
                operation='venue_discovery_extraction',
            )
            return parse_ai_json(raw)
        except DiscoveryExtractionError as exc:
            last_error = exc
    raise last_error or DiscoveryExtractionError('AI extraction failed.')


def parse_ai_json(raw):
    text = str(raw or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?|```$', '', text, flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        match = re.search(r'\{.*\}', text, flags=re.DOTALL)
        if not match:
            raise DiscoveryExtractionError('The AI did not return JSON.') from exc
        try:
            data = json.loads(match.group(0))
        except ValueError as inner:
            raise DiscoveryExtractionError('The AI returned malformed JSON.') from inner
    if not isinstance(data, dict):
        raise DiscoveryExtractionError('The AI returned JSON, but not an object.')
    return data


# ---------------------------------------------------------------------------
# Validation and normalisation
# ---------------------------------------------------------------------------

def _clean_text(value, limit=4000):
    return re.sub(r'\s+', ' ', str(value or '')).strip()[:limit]


def _clean_list(value, limit=30, item_limit=400):
    if isinstance(value, str):
        value = [part for part in re.split(r'[\n;]+', value)]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = _clean_text(item, item_limit)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _clean_dict(value, limit=20):
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, item in list(value.items())[:limit]:
        key = _clean_text(key, 80)
        if not key:
            continue
        if isinstance(item, (str, int, float, bool)) or item is None:
            out[key] = item if not isinstance(item, str) else _clean_text(item, 600)
        elif isinstance(item, list):
            out[key] = _clean_list(item, limit=20, item_limit=200)
        elif isinstance(item, dict):
            out[key] = {(_clean_text(k, 80)): _clean_text(v, 300) for k, v in list(item.items())[:10]}
    return out


def _squash(text):
    return re.sub(r'\s+', ' ', str(text or '')).strip().lower()


def _quote_found(quote, page_text):
    """True if a meaningful part of the quote really appears on the page."""
    quote = _squash(quote).strip('"“”\' ')
    if not quote:
        return False
    haystack = _squash(page_text)
    for part in re.split(r'\s*(?:\.\.\.|…)\s*', quote):
        part = part.strip(' .,;:')
        if len(part) >= 12 and part in haystack:
            return True
    return len(quote) >= 6 and quote in haystack


def _number_in_text(number, text):
    variants = {str(number), f'{number:,}', f'{number:,}'.replace(',', ' '), f'{number:,}'.replace(',', '.')}
    lowered = text.lower()
    return any(re.search(r'(?<![\d,.])' + re.escape(v) + r'(?![\d])', lowered) for v in variants)


def verify_evidence(raw_evidence, pages):
    by_url = {canonical_url(p.url): p for p in pages}
    checked_at = timezone.now().isoformat()
    verified = []
    for item in raw_evidence if isinstance(raw_evidence, list) else []:
        if not isinstance(item, dict):
            continue
        page = by_url.get(canonical_url(str(item.get('url', ''))))
        quote = str(item.get('evidence_text') or item.get('excerpt') or '')
        if not page or not _quote_found(quote, page.text):
            continue  # unverifiable evidence is dropped, never trusted
        verified.append({
            'field': _clean_text(item.get('field'), 80) or 'general',
            'claim': _clean_text(item.get('claim'), 300),
            'url': page.url,
            'source_title': _clean_text(item.get('source_title') or page.title, 200),
            'excerpt': _clean_text(quote, 300),
            'checked_at': checked_at,
        })
        if len(verified) >= 25:
            break
    return verified


def _verified_structured_rules(raw_rules, pages):
    from ..author_api import _normalise_structured_desk_rules
    corpus = '\n'.join(p.text for p in pages)
    kept = []
    for rule in raw_rules if isinstance(raw_rules, list) else []:
        if not isinstance(rule, dict) or rule.get('field') not in {'word_count', 'reference_count', 'required_sections'}:
            continue  # only objective rule types are allowed from discovery
        try:
            [normalised] = _normalise_structured_desk_rules([rule])
        except (ValueError, TypeError):
            continue
        if normalised['field'] in {'word_count', 'reference_count'}:
            if not _number_in_text(int(normalised['value']), corpus):
                continue
        if normalised['field'] == 'required_sections':
            if not all(_squash(section) in _squash(corpus) for section in normalised['value']):
                continue
        kept.append(normalised)
    return kept[:10]


def _verified_requirements(raw_items):
    from ..author_api import _normalise_required_submission_items
    kept, seen = [], set()
    for item in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(item, dict):
            continue
        candidate = dict(item)
        candidate['required'] = item.get('required') is True  # never required unless stated
        try:
            [normalised] = _normalise_required_submission_items([candidate])
        except (ValueError, TypeError):
            continue
        if normalised['key'] in seen:
            continue
        seen.add(normalised['key'])
        kept.append(normalised)
    return kept[:20]


def validate_extraction(raw, pages):
    """Turn raw AI output into a safe, normalised candidate dict (or raise)."""
    name = _clean_text(raw.get('name'), 300)
    if not name:
        raise DiscoveryExtractionError('The extraction did not include a venue name.')

    submission_types = []
    for label in _clean_list(raw.get('submission_types')) + _clean_list(raw.get('article_types')):
        mapped = normalize_submission_type(label)
        if mapped and mapped not in submission_types:
            submission_types.append(mapped)
    if 'other' in submission_types and len(submission_types) > 1:
        submission_types.remove('other')

    venue_type = str(raw.get('venue_type', '')).strip().lower()
    if venue_type not in {'journal', 'conference', 'publisher'}:
        if submission_types == ['book']:
            venue_type = 'publisher'
        elif 'conference_paper' in submission_types:
            venue_type = 'conference'
        else:
            venue_type = 'journal'

    # article_types use the labels the existing matcher understands, then any extras.
    article_types = [TYPE_LABELS[t] for t in submission_types if t != 'other']
    for extra in _clean_list(raw.get('article_types')):
        if extra not in article_types and len(article_types) < 30:
            article_types.append(extra)

    website_url = str(raw.get('website_url') or '').strip()
    submission_url = str(raw.get('submission_url') or '').strip()
    if website_url and not canonical_url(website_url):
        website_url = ''
    if submission_url and not canonical_url(submission_url):
        submission_url = ''

    evidence = verify_evidence(raw.get('source_evidence'), pages)
    official_domains = {registrable_domain(canonical_host(u)) for u in (website_url, submission_url) if u}
    official_pages = [p for p in pages if not is_third_party(p.url)
                      and (not official_domains or registrable_domain(canonical_host(p.url)) in official_domains)]

    acceptance = str(raw.get('acceptance_status', '')).strip().lower()
    if acceptance not in {'accepting', 'unclear', 'closed'}:
        acceptance = 'unclear'
    official_urls = {canonical_url(p.url) for p in official_pages}
    status_evidence = [e for e in evidence
                       if canonical_url(e['url']) in official_urls
                       and re.search(r'accept|status|submi|open|closed|suspend|proposal', e['field'] + ' ' + e['claim'], re.I)]
    if acceptance in {'accepting', 'closed'} and not status_evidence:
        acceptance = 'unclear'  # never claim a status the official pages don't support

    retention = raw.get('retention_days')
    try:
        retention = int(retention) if retention not in (None, '') else None
        if retention is not None and not 1 <= retention <= 3650:
            retention = None
    except (TypeError, ValueError):
        retention = None

    conflicts = _clean_list(raw.get('conflicts'), limit=5)
    candidate = {
        'name': name,
        'organization_name': _clean_text(raw.get('organization_name'), 300),
        'venue_type': venue_type,
        'description': _clean_text(raw.get('description'), 1000),
        'website_url': website_url[:1000],
        'submission_url': submission_url[:1000],
        'acceptance_status': acceptance,
        'submission_types': submission_types,
        'aims_scope': _clean_text(raw.get('aims_scope'), 4000),
        'article_types': article_types,
        'accepted_methods': _clean_list(raw.get('accepted_methods')),
        'quality_threshold': _clean_text(raw.get('quality_threshold'), 2000),
        'reviewer_criteria': _clean_list(raw.get('reviewer_criteria')),
        'policies': _clean_dict(raw.get('policies')),
        'disclosures': _clean_list(raw.get('disclosures')),
        'reporting_standards': _clean_list(raw.get('reporting_standards')),
        'desk_rejection_rules': _clean_list(raw.get('desk_rejection_rules')),
        'structured_desk_rejection_rules': _verified_structured_rules(raw.get('structured_desk_rejection_rules'), official_pages or pages),
        'required_submission_items': _verified_requirements(raw.get('required_submission_items')),
        'retention_days': retention,
        'deadlines': _clean_dict(raw.get('deadlines')),
        'submission_capacity': _clean_dict(raw.get('submission_capacity')),
        'current_demand': _clean_dict(raw.get('current_demand')),
        'config_notes': _clean_text(raw.get('config_notes'), 1500),
        'source_evidence': evidence,
        'source_urls': [p.url for p in pages][:10],
    }
    candidate['confidence'] = compute_confidence(candidate, pages, official_pages, status_evidence, conflicts)
    return candidate


def compute_confidence(candidate, pages, official_pages, status_evidence, conflicts):
    """Deterministic score from source quality; the AI never chooses the number."""
    if not official_pages:
        return min(25, 10 + 5 * len(candidate['source_evidence']))  # third-party or unverified only
    score = 25  # official venue/publisher page found
    if any(looks_like_submission_page(p.url, p.title) for p in official_pages):
        score += 20
    submission_url = candidate['submission_url']
    if submission_url and registrable_domain(canonical_host(submission_url)) in {
            registrable_domain(canonical_host(p.url)) for p in official_pages}:
        score += 10
    if candidate['submission_types']:
        score += 10
    if status_evidence:
        score += 15
    if len(official_pages) >= 2:
        score += 10
    score += min(10, 2 * len(candidate['source_evidence']))
    if candidate['acceptance_status'] == 'unclear':
        score -= 15
    if conflicts:
        score -= 15
    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# Deduplication and upsert
# ---------------------------------------------------------------------------

COMPARED_FIELDS = [
    'acceptance_status', 'submission_types', 'aims_scope', 'article_types', 'accepted_methods', 'quality_threshold',
    'reviewer_criteria', 'policies', 'disclosures', 'reporting_standards', 'desk_rejection_rules',
    'structured_desk_rejection_rules', 'required_submission_items', 'retention_days', 'deadlines',
    'submission_capacity', 'current_demand',
]
STAGED_FIELDS = COMPARED_FIELDS + [
    'name', 'organization_name', 'venue_type', 'description', 'website_url', 'submission_url', 'config_notes',
    'source_evidence', 'source_urls', 'confidence',
]


def content_fingerprint(pages):
    digest = hashlib.sha256()
    for page in sorted(pages, key=lambda p: canonical_url(p.url)):
        digest.update(canonical_url(page.url).encode())
        digest.update(_squash(page.text).encode())
    return digest.hexdigest()


def find_existing(candidate):
    sub = canonical_url(candidate.get('submission_url'))
    if sub:
        match = DiscoveredVenue.objects.filter(canonical_submission_url=sub).first()
        if match:
            return match
    name = normalize_name(candidate['name'])
    domain = registrable_domain(canonical_host(candidate.get('website_url') or candidate.get('submission_url') or ''))
    if domain:
        match = DiscoveredVenue.objects.filter(normalized_name=name, canonical_domain=domain).first()
        if match:
            return match
    org = normalize_name(candidate.get('organization_name'))
    if org:
        return DiscoveredVenue.objects.filter(normalized_name=name, normalized_organization_name=org).first()
    return None


def _describe_changes(old, new):
    changed = [f for f in COMPARED_FIELDS if getattr(old, f) != new.get(f)]
    labels = [f.replace('_', ' ') for f in changed]
    if not labels:
        return None
    return changed, 'Official sources changed: ' + ', '.join(labels[:6]) + ('…' if len(labels) > 6 else '') + '.'


def upsert_candidate(candidate, fingerprint):
    """Create or update one DiscoveredVenue. Returns (record, outcome)."""
    now = timezone.now()
    domain = registrable_domain(canonical_host(candidate.get('website_url') or candidate.get('submission_url') or ''))
    keys = {
        'normalized_name': normalize_name(candidate['name']),
        'normalized_organization_name': normalize_name(candidate.get('organization_name')),
        'canonical_domain': domain,
        'canonical_submission_url': canonical_url(candidate.get('submission_url')),
    }
    with transaction.atomic():
        existing = find_existing(candidate)
        if existing is None:
            record = DiscoveredVenue.objects.create(
                **{f: candidate[f] for f in STAGED_FIELDS}, **keys,
                content_fingerprint=fingerprint, first_discovered_at=now, last_checked_at=now,
                discovery_status='new', last_error='',
            )
            return record, 'created'

        record = DiscoveredVenue.objects.select_for_update().get(id=existing.id)
        diff = _describe_changes(record, candidate)
        for f in STAGED_FIELDS:
            setattr(record, f, candidate[f])
        for k, v in keys.items():
            setattr(record, k, v)
        record.content_fingerprint = fingerprint
        record.last_checked_at = now
        record.last_error = ''
        outcome = 'updated'
        if diff and record.discovery_status in {'added', 'changed'}:
            # Never touch the live VenueAgentConfig: flag the difference for the admin instead.
            record.discovery_status = 'changed'
            record.change_summary = diff[1] + ' The live Venue Agent was not changed.'
            outcome = 'changed'
            from ..models import AuditEvent
            AuditEvent.objects.create(
                actor_email='', actor_role='system', action='venue_discovery.candidate_changed',
                resource_type='discovered_venue', resource_id=str(record.id),
                venue_id=record.added_venue_id,
                organization_id=record.added_venue.organization_id if record.added_venue_id else None,
                detail={'discovered_venue_id': str(record.id), 'changed_fields': diff[0][:10],
                        'acceptance_status': candidate['acceptance_status'], 'confidence': candidate['confidence']},
            )
        elif record.discovery_status == 'error':
            record.discovery_status = 'new'
        record.save()
        return record, outcome


# ---------------------------------------------------------------------------
# Candidate processing and the daily run
# ---------------------------------------------------------------------------

def gather_pages(entry_url, fetcher, config):
    """Fetch the entry page and up to N same-site author/submission pages."""
    first = fetcher.fetch(entry_url)
    pages = [first]
    for link in find_submission_links(first, config.max_pages_per_candidate - 1):
        try:
            pages.append(fetcher.fetch(link))
        except DiscoveryFetchError:
            continue
    return pages


def process_candidate(entry_url, fetcher, config, *, extractor=None):
    extractor = extractor or extract_with_ai
    pages = gather_pages(entry_url, fetcher, config)
    if not any(page.text for page in pages):
        raise DiscoveryExtractionError('The page has no readable text (it may need JavaScript).')
    fingerprint = content_fingerprint(pages)

    # Cost control: unchanged content from a recent check reuses the stored extraction.
    previous = DiscoveredVenue.objects.filter(content_fingerprint=fingerprint).first()
    if previous:
        previous.last_checked_at = timezone.now()
        previous.save(update_fields=['last_checked_at', 'updated_at'])
        return previous, 'unchanged'

    candidate = validate_extraction(extractor(pages, config), pages)
    return upsert_candidate(candidate, fingerprint)


def _entry_key(url):
    parts = urlsplit(canonical_url(url))
    first_segment = (parts.path.strip('/').split('/') or [''])[0]
    return f'{parts.netloc}/{first_segment}'


def _record_error(run, url, stage, message):
    run.errors = (run.errors or []) + [{'url': url[:500], 'stage': stage, 'message': str(message)[:300]}]


def run_discovery(run, *, config=None, provider=None, fetcher=None, extractor=None, create_message=None):
    """Execute one discovery run and keep the run record up to date."""
    config = config or DiscoveryConfig.from_env()
    run.status, run.started_at = 'processing', timezone.now()
    run.save(update_fields=['status', 'started_at'])
    try:
        if not config.enabled:
            raise DiscoveryConfigError('Venue discovery is disabled (VENUE_DISCOVERY_ENABLED is not true).')
        if config.mode == 'claude_agent' and provider is None and fetcher is None:
            _run_claude_agent(run, config, create_message=create_message)
            run.status = 'completed'
            run.summary = (f'{run.candidates_created} new · {run.candidates_updated} updated · '
                           f'{run.candidates_changed} changed · {len(run.errors or [])} skipped')
            run.completed_at = timezone.now()
            run.save()
            return run
        if config.mode not in {'claude_agent', 'search_api'}:
            raise DiscoveryConfigError(f'Unknown VENUE_DISCOVERY_MODE {config.mode!r}. Use claude_agent or search_api.')
        provider = provider or get_search_provider(config)
        fetcher = fetcher or SafeFetcher(config)

        entries, seen_keys = [], set()
        for _category, query in query_bank()[: config.max_queries_per_run]:
            try:
                results = provider.search(query, max_results=config.max_results_per_query)
            except Exception as exc:  # one failing query must not stop the run
                _record_error(run, query, 'search', exc)
                continue
            run.queries_run += 1
            run.results_seen += len(results)
            for result in results:
                if not canonical_url(result.url) or is_third_party(result.url):
                    continue
                key = _entry_key(result.url)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                entries.append(result.url)

        for url in entries[: config.max_candidates_per_run]:
            _process_and_count(run, url, fetcher, config, extractor)

        _recheck_existing(run, fetcher, config, extractor, skip=set(seen_keys))

        run.official_pages_checked = fetcher.pages_fetched
        run.status = 'completed'
        run.summary = (f'{run.candidates_created} new · {run.candidates_updated} updated · '
                       f'{run.candidates_changed} changed · {len(run.errors or [])} skipped')
    except DiscoveryConfigError as exc:
        run.status = 'failed'
        run.summary = str(exc)
    except Exception as exc:  # unexpected: fail the run, keep the app healthy
        run.status = 'failed'
        run.summary = 'Discovery failed unexpectedly. See server logs for details.'
        _record_error(run, '', 'run', exc.__class__.__name__)
        try:
            from ..monitoring import capture_exception
            capture_exception(exc, component='venue_discovery', operation='venue_discovery_run')
        except Exception:
            pass
    run.completed_at = timezone.now()
    run.save()
    return run


def _process_and_count(run, url, fetcher, config, extractor):
    try:
        _record, outcome = process_candidate(url, fetcher, config, extractor=extractor)
    except (DiscoveryFetchError, DiscoveryExtractionError) as exc:
        _record_error(run, url, 'candidate', exc)
        return
    except Exception as exc:  # e.g. AI provider failure or database constraint
        _record_error(run, url, 'candidate', exc.__class__.__name__ + ': ' + str(exc)[:200])
        return
    if outcome == 'created':
        run.candidates_created += 1
    elif outcome == 'changed':
        run.candidates_changed += 1
    else:
        run.candidates_updated += 1


def _recheck_existing(run, fetcher, config, extractor, skip):
    if config.max_rechecks_per_run <= 0:
        return
    stale_before = timezone.now() - timedelta(hours=config.recheck_after_hours)
    due = (DiscoveredVenue.objects
           .exclude(discovery_status='ignored')
           .filter(last_checked_at__lt=stale_before)
           .order_by('last_checked_at')[: config.max_rechecks_per_run])
    for record in due:
        url = record.submission_url or record.website_url or (record.source_urls or [''])[0]
        if not url or _entry_key(url) in skip:
            continue
        before = record.discovery_status
        try:
            _updated, outcome = process_candidate(url, fetcher, config, extractor=extractor)
        except Exception as exc:
            # Keep the last good data; just note the failed check.
            record.last_error = str(exc)[:300]
            record.save(update_fields=['last_error', 'updated_at'])
            _record_error(run, url, 'recheck', exc)
            continue
        if outcome == 'changed' and before != 'changed':
            run.candidates_changed += 1
        elif outcome in {'updated', 'unchanged'}:
            run.candidates_updated += 1


def start_run(trigger='schedule', requested_by=''):
    """Create a run unless one is already queued/processing (from the last 3 hours)."""
    recent = timezone.now() - timedelta(hours=3)
    active = VenueDiscoveryRun.objects.filter(status__in=['queued', 'processing'], created_at__gte=recent).first()
    if active:
        return active, False
    return VenueDiscoveryRun.objects.create(status='queued', trigger=trigger, requested_by=requested_by[:254]), True


# ---------------------------------------------------------------------------
# Claude agent mode
# ---------------------------------------------------------------------------

def _ingest_agent_answer(run, final_text, pages, *, expected_records=None):
    """Validate each venue Claude reported against the pages it fetched, then upsert."""
    from .venue_discovery_agent import pages_for_venue, parse_venues
    try:
        venues = parse_venues(final_text)
    except DiscoveryExtractionError as exc:
        _record_error(run, '', 'agent', exc)
        return set()
    run.results_seen += len(venues)
    seen_ids = set()
    for raw in venues:
        label = str(raw.get('website_url') or raw.get('name') or '')[:300]
        try:
            venue_pages = pages_for_venue(raw, pages)
            candidate = validate_extraction(raw, venue_pages)
            fingerprint = (content_fingerprint(venue_pages) if venue_pages
                           else hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest())
            record, outcome = upsert_candidate(candidate, fingerprint)
        except (DiscoveryExtractionError, ValueError, TypeError) as exc:
            _record_error(run, label, 'candidate', exc)
            continue
        except Exception as exc:
            _record_error(run, label, 'candidate', exc.__class__.__name__ + ': ' + str(exc)[:200])
            continue
        seen_ids.add(record.id)
        if outcome == 'created':
            run.candidates_created += 1
        elif outcome == 'changed':
            run.candidates_changed += 1
        else:
            run.candidates_updated += 1
    return seen_ids


def _account(run, stats, pages):
    run.queries_run += stats['searches']
    run.official_pages_checked += len(pages)
    for message in stats['tool_errors'][:10]:
        _record_error(run, '', 'agent tool', message)


def _run_claude_agent(run, config, *, create_message=None):
    from ..ai_usage import AIBudgetExceeded
    from .venue_discovery_agent import agent_settings, discovery_prompt, recheck_prompt, run_agent

    settings = agent_settings()
    if not settings['api_key']:
        raise DiscoveryConfigError('ANTHROPIC_API_KEY is not set, so the Claude discovery agent cannot run.')

    known = list(DiscoveredVenue.objects.order_by('-last_checked_at').values_list('name', flat=True)[:200])
    for category in settings['categories']:
        try:
            final_text, pages, stats = run_agent(discovery_prompt(category, settings, known), settings,
                                                 create_message=create_message)
        except AIBudgetExceeded as exc:
            _record_error(run, category, 'budget', exc)
            return  # stop spending for this run
        except Exception as exc:
            _record_error(run, category, 'agent', exc.__class__.__name__ + ': ' + str(exc)[:200])
            continue
        _account(run, stats, pages)
        _ingest_agent_answer(run, final_text, pages)

    if config.max_rechecks_per_run <= 0:
        return
    stale_before = timezone.now() - timedelta(hours=config.recheck_after_hours)
    due = list(DiscoveredVenue.objects.exclude(discovery_status='ignored')
               .filter(last_checked_at__lt=stale_before)
               .exclude(website_url='', submission_url='')
               .order_by('last_checked_at')[: config.max_rechecks_per_run])
    size = settings['recheck_batch_size']
    for start in range(0, len(due), size):
        batch = due[start:start + size]
        try:
            final_text, pages, stats = run_agent(recheck_prompt(batch), settings, create_message=create_message,
                                                 operation='venue_discovery_recheck')
        except AIBudgetExceeded as exc:
            _record_error(run, '', 'budget', exc)
            return
        except Exception as exc:
            for record in batch:
                record.last_error = 'Re-check could not run.'
                record.save(update_fields=['last_error', 'updated_at'])
            _record_error(run, '', 'recheck', exc.__class__.__name__)
            continue
        _account(run, stats, pages)
        updated = _ingest_agent_answer(run, final_text, pages)
        for record in batch:
            if record.id not in updated:
                # Keep the last good data; just note that this check did not confirm it.
                record.last_error = 'The latest re-check did not return this venue.'
                record.save(update_fields=['last_error', 'updated_at'])
