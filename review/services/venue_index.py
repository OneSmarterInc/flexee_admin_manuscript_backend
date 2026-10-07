"""Venue index, layer 1: the spine (build plan step 2).

Builds and refreshes a catalogue of journals in the target fields from free, structured
sources, with no AI involved:

  * OpenAlex  - the journal list itself: title, ISSNs, publisher, country, open access,
                subject mix, output and citation counts, first/last publication year.
  * Crossref  - whether the journal registers DOIs, how many, and since which year.
  * DOAJ      - for open-access titles: listing, review type and time, APC, and the
                official guideline / scope / board pages (the worklist for layer 2).
  * ISSN      - checksum validation and agreement between catalogues. (The ISSN Portal
                itself is a paid service and disallows automated access.)

Records land as 'listed'. A record linked to a live Venue takes that venue's trust tier.
"""
import logging
import os
import re
import time
from datetime import timedelta

import httpx
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from ..models import IndexedVenue, Venue, VenueIndexRun
from .venue_discovery import normalize_name

logger = logging.getLogger(__name__)

OPENALEX_SOURCES = 'https://api.openalex.org/sources'
CROSSREF_JOURNALS = 'https://api.crossref.org/journals/'
DOAJ_JOURNALS = 'https://doaj.org/api/search/journals/'

# Build plan section 3.1: start narrow and deep. OpenAlex subfields (ASJC-based ids).
PROFILES = {
    'business-is': {
        'label': 'Business & management, information systems, supply chain & operations, '
                 'AI in organizations, education',
        'subfields': {
            '1400': 'General Business, Management and Accounting',
            '1403': 'Business and International Management',
            '1404': 'Management Information Systems',
            '1405': 'Management of Technology and Innovation',
            '1407': 'Organizational Behavior and Human Resource Management',
            '1408': 'Strategy and Management',
            '1800': 'General Decision Sciences',
            '1802': 'Information Systems and Management',
            '1803': 'Management Science and Operations Research',
            '1702': 'Artificial Intelligence',
            '1710': 'Information Systems',
            '2209': 'Industrial and Manufacturing Engineering',
            '3304': 'Education',
        },
        # Broad subjects that only count when a journal also connects to the core fields: education
        # in general is not business or simulation-based learning, and most AI journals are pure
        # computer science rather than AI in organizations.
        'bridging': {'1702', '3304', '2209'},
        # Used only if OpenAlex rejects the subfield filter: keyword searches, then the same scope check.
        'search_terms': ['management', 'business', 'information systems', 'operations management',
                         'supply chain', 'artificial intelligence', 'education', 'decision sciences'],
    },
}
DEFAULT_PROFILE = 'business-is'


class IndexSourceError(Exception):
    """A catalogue could not be reached or answered with an error."""


def _env_int(name, default, low, high):
    try:
        return max(low, min(int(os.getenv(name, str(default))), high))
    except ValueError:
        return default


def _env_float(name, default, low, high):
    try:
        return max(low, min(float(os.getenv(name, str(default))), high))
    except ValueError:
        return default


class IndexConfig:
    def __init__(self):
        self.profile = os.getenv('VENUE_INDEX_PROFILE', DEFAULT_PROFILE).strip() or DEFAULT_PROFILE
        # The index keeps the N most-published journals in the fields (build plan 3.1: ~1,500-2,500).
        self.max_records = _env_int('VENUE_INDEX_MAX_RECORDS', 2500, 1, 50000)
        self.max_pages = _env_int('VENUE_INDEX_MAX_PAGES', 400, 1, 5000)
        self.min_works = _env_int('VENUE_INDEX_MIN_WORKS', 30, 0, 100000)
        # A journal whose main subject is outside the fields needs this share of its output inside them.
        self.min_scope_share = _env_float('VENUE_INDEX_MIN_SCOPE_SHARE', 0.5, 0.0, 1.0)
        # Every journal not mainly in a core subject needs at least this share in core subjects
        # (for a bridging-subject journal, other bridging subjects count too).
        self.min_core_share = _env_float('VENUE_INDEX_MIN_CORE_SHARE', 0.10, 0.0, 1.0)
        self.max_inactive_years = _env_int('VENUE_INDEX_MAX_INACTIVE_YEARS', 3, 0, 50)
        self.enrich_days = _env_int('VENUE_INDEX_ENRICH_DAYS', 30, 1, 365)
        self.time_limit_seconds = _env_int('VENUE_INDEX_TIME_LIMIT_MINUTES', 25, 1, 28) * 60
        self.http_timeout = _env_float('VENUE_INDEX_HTTP_TIMEOUT_SECONDS', 20, 2, 120)
        self.delay_scale = _env_float('VENUE_INDEX_POLITE_DELAY', 1.0, 0.0, 10.0)
        self.openalex_api_key = os.getenv('OPENALEX_API_KEY', '').strip()
        # Crossref allows 3 parallel requests to callers who send a contact email, 1 otherwise.
        self.workers = _env_int('VENUE_INDEX_WORKERS', 3, 1, 3)
        self.contact_email = (os.getenv('VENUE_INDEX_CONTACT_EMAIL', '')
                              or os.getenv('VENUE_DISCOVERY_CONTACT_EMAIL', '')).strip()
        override = [x.strip() for x in os.getenv('VENUE_INDEX_SUBFIELDS', '').split(',') if x.strip()]
        base = PROFILES.get(self.profile, PROFILES[DEFAULT_PROFILE])
        self.subfields = {sid: base['subfields'].get(sid, f'Subfield {sid}') for sid in override} if override \
            else dict(base['subfields'])
        self.search_terms = list(base['search_terms'])
        bridging_env = os.getenv('VENUE_INDEX_BRIDGING_SUBFIELDS')
        bridging = ({x.strip() for x in bridging_env.split(',') if x.strip()} if bridging_env is not None
                    else set(base.get('bridging', set())))
        self.bridging = bridging & set(self.subfields)
        self.core = set(self.subfields) - self.bridging
        if not self.contact_email:
            self.workers = 1

    @property
    def polite(self):
        return bool(self.contact_email)

    @property
    def user_agent(self):
        contact = f' (mailto:{self.contact_email})' if self.contact_email else ''
        return f'FlexeeVenueIndex/1.0{contact}'


# ---------------------------------------------------------------------------
# HTTP: polite, retrying, injectable for tests
# ---------------------------------------------------------------------------

# Seconds between calls per catalogue. Crossref: 10/s for callers with a contact email, 5/s otherwise.
HOST_DELAY = {'openalex': 0.15, 'crossref': 0.1, 'crossref_public': 0.2, 'doaj': 0.6}


class IndexHttp:
    """Polite, retrying GETs. Safe to call from several threads: each catalogue is paced
    across threads, and Crossref gets at most `workers` requests in flight."""

    def __init__(self, config, *, get=None, sleep=None):
        import threading
        self.config = config
        self._get = get or httpx.get
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._next_at = {}
        self._slots = {
            'crossref': threading.BoundedSemaphore(config.workers),
            'doaj': threading.BoundedSemaphore(1),
            'openalex': threading.BoundedSemaphore(1),
        }

    def _delay(self, source):
        if source == 'crossref' and not self.config.polite:
            source = 'crossref_public'
        return HOST_DELAY.get(source, 0.2) * self.config.delay_scale

    def _pace(self, source):
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_at.get(source, 0))
            self._next_at[source] = start + self._delay(source)
        if start > now:
            self._sleep(start - now)

    def get_json(self, source, url, params=None):
        """Return (status, json_or_None). Retries on network errors, 429 and 5xx."""
        params = dict(params or {})
        if source == 'crossref' and self.config.contact_email:
            params.setdefault('mailto', self.config.contact_email)  # Crossref's polite pool
        last_error = ''
        slot = self._slots.get(source)
        for attempt in range(3):
            self._pace(source)
            if slot:
                slot.acquire()
            try:
                response = self._get(url, params=params, timeout=self.config.http_timeout,
                                     headers={'User-Agent': self.config.user_agent, 'Accept': 'application/json'},
                                     follow_redirects=True)
            except httpx.HTTPError as exc:
                last_error = f'could not reach {source} ({exc.__class__.__name__})'
            else:
                if response.status_code == 404:
                    return 404, None
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = f'{source} returned HTTP {response.status_code}'
                elif response.status_code >= 400:
                    return response.status_code, None
                else:
                    try:
                        return response.status_code, response.json()
                    except ValueError:
                        raise IndexSourceError(f'{source} did not return JSON')
            finally:
                if slot:
                    slot.release()
            self._sleep((1.5 * (attempt + 1)) * self.config.delay_scale)
        if source == 'openalex' and 'HTTP 429' in last_error:
            last_error = ('OpenAlex daily free allowance used up (HTTP 429). Add OPENALEX_API_KEY to .env '
                          '(free at openalex.org/settings/api), or run again after midnight UTC (5:30 AM IST).')
        raise IndexSourceError(last_error or f'{source} failed')


# ---------------------------------------------------------------------------
# ISSN
# ---------------------------------------------------------------------------

_ISSN_RE = re.compile(r'^(\d{4})-?(\d{3}[\dXx])$')


def normalize_issn(value):
    match = _ISSN_RE.match(str(value or '').strip())
    return f'{match.group(1)}-{match.group(2).upper()}' if match else ''


def issn_checksum_ok(value):
    issn = normalize_issn(value)
    if not issn:
        return False
    digits = issn.replace('-', '')
    total = sum(int(d) * w for d, w in zip(digits[:7], range(8, 1, -1)))
    check = (11 - total % 11) % 11
    return digits[7] == ('X' if check == 10 else str(check))


# ---------------------------------------------------------------------------
# OpenAlex catalogue
# ---------------------------------------------------------------------------

def _short_id(value, prefix=''):
    text = str(value or '').rstrip('/')
    tail = text.rsplit('/', 1)[-1]
    return tail[len(prefix):] if prefix and tail.startswith(prefix) else tail


def assess_scope(source, config, *, this_year):
    """Decide whether an OpenAlex source belongs in this field profile.

    A journal's topic list includes anything it ever published, so one stray paper must not
    pull it in: the in-scope share of its output, or its main topic, decides.
    Returns (keep, details) where details has share, primary_subfield, subfields, reason.
    """
    topics = [t for t in (source.get('topics') or []) if isinstance(t, dict)]
    total = sum(int(t.get('count') or 0) for t in topics) or 0
    by_subfield = {}
    for topic in topics:
        sub = topic.get('subfield') or {}
        sid = _short_id(sub.get('id'))
        if not sid:
            continue
        entry = by_subfield.setdefault(sid, {'id': sid, 'name': str(sub.get('display_name') or '')[:200], 'count': 0})
        entry['count'] += int(topic.get('count') or 0)
    ranked = sorted(by_subfield.values(), key=lambda e: e['count'], reverse=True)
    in_scope = sum(e['count'] for e in ranked if e['id'] in config.subfields)
    share = (in_scope / total) if total else 0.0
    primary = ranked[0] if ranked else None
    subfields = [{'id': e['id'], 'name': e['name'], 'share': round(e['count'] / total, 3) if total else 0}
                 for e in ranked[:6]]
    details = {'share': round(share, 3), 'subfields': subfields,
               'primary_subfield': primary['name'] if primary else ''}

    if str(source.get('type') or 'journal') != 'journal':
        return False, {**details, 'reason': 'not a journal'}
    if int(source.get('works_count') or 0) < config.min_works:
        return False, {**details, 'reason': 'too few works'}
    last_year = source.get('last_publication_year')
    if last_year and config.max_inactive_years and int(last_year) < this_year - config.max_inactive_years:
        return False, {**details, 'reason': 'no longer publishing'}
    main = primary['id'] if primary else ''
    if main in config.core:
        return True, {**details, 'reason': ''}

    def share_of(ids):
        return (sum(e['count'] for e in ranked if e['id'] in ids) / total) if total else 0.0

    if main in config.bridging:
        # e.g. an education journal: kept only if it also publishes business / IS / AI work.
        if share_of(set(config.subfields) - {main}) < config.min_core_share:
            return False, {**details, 'reason': f'{primary["name"]} journal with little business or IS content'}
        return True, {**details, 'reason': ''}
    if share < config.min_scope_share or share_of(config.core) < config.min_core_share:
        return False, {**details, 'reason': 'mostly outside the target fields'}
    return True, {**details, 'reason': ''}


class OpenAlexCatalogue:
    """Pages through OpenAlex journals for a field profile.

    First choice: filter by subfield. If OpenAlex rejects that filter (or it matches nothing),
    fall back to keyword searches. Every result goes through assess_scope either way.
    """

    def __init__(self, http, config):
        self.http = http
        self.config = config
        self.method = ''
        self.complete = False
        self.pages = 0
        self.progress = None

    # Only the fields the spine uses: pages are several times smaller and faster.
    SELECT = ('id,issn_l,issn,display_name,host_organization_name,country_code,type,is_oa,is_in_doaj,'
              'is_core,is_ojs,homepage_url,apc_usd,works_count,cited_by_count,summary_stats,'
              'first_publication_year,last_publication_year,topics')

    def _params(self, extra):
        params = {'per_page': 100, 'sort': 'works_count:desc', **extra}
        if self.config.openalex_api_key:
            params['api_key'] = self.config.openalex_api_key
        if self.config.contact_email:
            params['mailto'] = self.config.contact_email
        return params

    def _walk(self, base_filter, budget, *, search=None):
        """Pages one query, largest journals first. Stops by itself once journals fall below the
        minimum size (everything after is smaller), which counts as reaching the end."""
        variants = [  # if OpenAlex rejects an optional part, the next variant drops it
            {'filter': f'{base_filter},works_count:>{max(self.config.min_works - 1, 0)}', 'select': self.SELECT},
            {'filter': base_filter, 'select': self.SELECT},
            {'filter': base_filter},
        ]
        if search:
            for v in variants:
                v['search'] = search
        cursor = '*'
        while cursor and self.pages < self.config.max_pages and budget():
            status, payload = self.http.get_json('openalex', OPENALEX_SOURCES, self._params({**variants[0], 'cursor': cursor}))
            if status in (400, 403) and cursor == '*' and len(variants) > 1:
                variants.pop(0)
                continue
            if status != 200 or payload is None:
                raise IndexSourceError(f'OpenAlex returned HTTP {status}')
            self.pages += 1
            results = payload.get('results') or []
            for item in results:
                if int(item.get('works_count') or 0) < self.config.min_works:
                    return  # sorted by size: nothing further can qualify
                yield item
            if self.progress:
                self.progress('catalogue_page')
            cursor = (payload.get('meta') or {}).get('next_cursor')
            if not results:
                cursor = None
        if cursor:
            raise _Truncated()

    def sources(self, budget):
        ids = '|'.join(sorted(self.config.subfields))
        first_page_seen = False
        try:
            self.method = 'subfield_filter'
            for item in self._walk(f'type:journal,topics.subfield.id:{ids}', budget):
                first_page_seen = True
                yield item
            if first_page_seen:
                self.complete = True
                return
        except _Truncated:
            return
        except IndexSourceError as exc:
            if first_page_seen or 'HTTP 4' not in str(exc):
                raise
        # The subfield filter was rejected or matched nothing: keyword searches instead.
        self.method = 'keyword_search'
        seen = set()
        try:
            for term in self.config.search_terms:
                for item in self._walk('type:journal', budget, search=term):
                    key = item.get('id')
                    if key in seen:
                        continue
                    seen.add(key)
                    yield item
        except _Truncated:
            return
        # Keyword search can never prove a journal left the catalogue, so it is not 'complete'.


class _Truncated(Exception):
    """Stopped early (page cap or time limit): the catalogue pass is not complete."""


def clean_title(value):
    """Catalogue titles sometimes carry invisible or private-use characters (shown as boxes)."""
    import unicodedata
    text = ''.join(' ' if unicodedata.category(ch) in {'Cc', 'Cf', 'Co', 'Cs', 'Zl', 'Zp', 'Cn'} else ch
                   for ch in unicodedata.normalize('NFC', str(value or '')))
    return re.sub(r'\s+', ' ', text).strip()


def spine_fields(source, details):
    issns = sorted({normalize_issn(x) for x in (source.get('issn') or []) if normalize_issn(x)})
    stats = source.get('summary_stats') or {}
    apc = source.get('apc_usd')
    return {
        'title': clean_title(source.get('display_name'))[:500],
        'venue_type': 'journal',
        'issn_l': normalize_issn(source.get('issn_l')),
        'issns': issns,
        'publisher': clean_title(source.get('host_organization_name'))[:300],
        'country_code': str(source.get('country_code') or '')[:2].upper(),
        'homepage_url': str(source.get('homepage_url') or '')[:1000],
        'open_access': bool(source.get('is_oa')),
        'doaj_listed': bool(source.get('is_in_doaj')),
        'apc_usd': int(apc) if isinstance(apc, (int, float)) and apc >= 0 else None,
        'primary_subfield': details['primary_subfield'][:200],
        'subfields': details['subfields'],
        'scope_share': details['share'],
        'metrics': {k: v for k, v in {
            'works_count': source.get('works_count'),
            'cited_by_count': source.get('cited_by_count'),
            'h_index': stats.get('h_index'),
            'two_year_mean_citedness': round(stats['2yr_mean_citedness'], 3)
            if isinstance(stats.get('2yr_mean_citedness'), (int, float)) else None,
            'is_core': source.get('is_core'),
            'is_ojs': source.get('is_ojs'),
        }.items() if v is not None},
        'first_publication_year': source.get('first_publication_year') or None,
        'last_publication_year': source.get('last_publication_year') or None,
    }


# ---------------------------------------------------------------------------
# Crossref and DOAJ enrichment
# ---------------------------------------------------------------------------

def crossref_facts(http, issns, now):
    for issn in issns[:2]:
        status, payload = http.get_json('crossref', CROSSREF_JOURNALS + issn)
        if status == 404:
            continue
        if status != 200 or not payload:
            raise IndexSourceError(f'Crossref returned HTTP {status}')
        message = payload.get('message') or {}
        years = [int(pair[0]) for pair in ((message.get('breakdowns') or {}).get('dois-by-issued-year') or [])
                 if isinstance(pair, (list, tuple)) and pair and str(pair[0]).isdigit() and int(pair[1] or 0) > 0]
        return {
            'registered': True,
            'checked_issn': issn,
            'publisher': str(message.get('publisher') or '')[:300],
            'total_dois': int((message.get('counts') or {}).get('total-dois') or 0),
            'first_year': min(years) if years else None,
            'issns': sorted({normalize_issn(x) for x in (message.get('ISSN') or []) if normalize_issn(x)}),
            'checked_at': now.isoformat(),
        }
    return {'registered': False, 'checked_at': now.isoformat()}


def doaj_facts(http, issns, now):
    from urllib.parse import quote
    for issn in issns[:2]:
        status, payload = http.get_json('doaj', DOAJ_JOURNALS + quote(f'issn:{issn}', safe=''))
        if status != 200 or payload is None:
            if status == 404:
                continue
            raise IndexSourceError(f'DOAJ returned HTTP {status}')
        results = payload.get('results') or []
        if not results:
            continue
        item = results[0]
        bib = item.get('bibjson') or {}
        ref = bib.get('ref') or {}
        editorial = bib.get('editorial') or {}
        apc = bib.get('apc') or {}
        return {
            'listed': True,
            'id': str(item.get('id') or '')[:64],
            'guidelines_url': str(ref.get('author_instructions') or '')[:1000],
            'aims_scope_url': str(ref.get('aims_scope') or '')[:1000],
            'journal_url': str(ref.get('journal') or '')[:1000],
            'board_url': str(editorial.get('board_url') or '')[:1000],
            'review_url': str(editorial.get('review_url') or '')[:1000],
            'review_process': [str(x)[:80] for x in (editorial.get('review_process') or [])][:4],
            'publication_time_weeks': bib.get('publication_time_weeks'),
            'has_apc': apc.get('has_apc'),
            'apc_max': [{'price': p.get('price'), 'currency': p.get('currency')}
                        for p in (apc.get('max') or []) if isinstance(p, dict)][:3],
            'oa_start': bib.get('oa_start'),
            'plagiarism_detection': (bib.get('plagiarism') or {}).get('detection'),
            'last_updated': str(item.get('last_updated') or '')[:40],
            'checked_at': now.isoformat(),
        }
    return {'listed': False, 'checked_at': now.isoformat()}


def enrich_record(record, http, now):
    fetch_enrichment(record, http, now)
    record.save()


def fetch_enrichment(record, http, now):
    """Fill in the Crossref/DOAJ facts on the in-memory record (network only, no database),
    so several records can be fetched in parallel and saved by the caller."""
    issns = [x for x in [record.issn_l, *record.issns] if x]
    issns = list(dict.fromkeys(issns))
    record.issn_checks = {
        'valid_checksums': all(issn_checksum_ok(x) for x in issns) if issns else False,
        'has_issn': bool(issns),
    }
    checked = dict(record.checked or {})
    record.crossref = crossref_facts(http, issns, now) if issns else {'registered': False, 'checked_at': now.isoformat()}
    checked['crossref'] = now.isoformat()
    if record.crossref.get('registered'):
        record.issn_checks['crossref_agrees'] = bool(set(record.crossref.get('issns') or []) & set(issns))
    if issns and (record.open_access or record.doaj_listed):
        record.doaj = doaj_facts(http, issns, now)
        checked['doaj'] = now.isoformat()
        record.doaj_listed = bool(record.doaj.get('listed'))
        if record.doaj.get('id'):
            record.source_ids = {**(record.source_ids or {}), 'doaj': record.doaj['id']}
    record.checked = checked
    record.enriched_at = now
    record.last_error = ''
    return record


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def close_stale_runs(now=None):
    now = now or timezone.now()
    VenueIndexRun.objects.filter(status__in=['queued', 'processing'], created_at__lt=now - timedelta(hours=1)).update(
        status='failed', completed_at=now,
        summary='Interrupted (worker restarted or time limit reached). Records saved before that were kept.')


def start_index_run(*, mode='full', trigger='schedule', requested_by=''):
    """Create a run unless one is already queued or processing."""
    close_stale_runs()
    active = VenueIndexRun.objects.filter(status__in=['queued', 'processing']).first()
    if active:
        return active, False
    config = IndexConfig()
    return VenueIndexRun.objects.create(mode=mode if mode in {'full', 'enrich', 'screen'} else 'full', trigger=trigger,
                                        requested_by=requested_by[:254], field_profile=config.profile), True


def pending_enrichment(config, now):
    """Records never checked against Crossref/DOAJ, or last checked longer ago than the refresh window."""
    stale = now - timedelta(days=config.enrich_days)
    return (IndexedVenue.objects.filter(field_profile=config.profile, missing_since__isnull=True)
            .filter(Q(enriched_at__isnull=True) | Q(enriched_at__lt=stale)))


def _record_error(run, source, detail):
    run.errors = (run.errors or [])[-49:] + [{'source': source, 'detail': str(detail)[:300],
                                               'at': timezone.now().isoformat()}]


def link_to_venues(config):
    """Link index records to live venues with the same normalized name. Returns how many were linked."""
    by_name = {}
    for venue in Venue.objects.only('id', 'name'):
        by_name.setdefault(normalize_name(venue.name), venue.id)
    linked = 0
    for record in IndexedVenue.objects.filter(field_profile=config.profile, venue__isnull=True).only('id', 'normalized_title'):
        venue_id = by_name.get(record.normalized_title)
        if venue_id:
            IndexedVenue.objects.filter(id=record.id).update(venue_id=venue_id)
            linked += 1
    return linked


def run_index(run, *, http=None, now=None, clock=None, progress=None, time_limit_seconds=None, page_fetcher=None,
              read_pages=True):
    """Run one import. Saves as it goes, so a stopped run keeps what it found.

    progress, if given, is called with short status lines (the management command prints them).
    """
    config = IndexConfig()
    clock = clock or time.monotonic
    deadline = clock() + (time_limit_seconds or config.time_limit_seconds)
    budget = lambda: clock() < deadline  # noqa: E731
    http = http or IndexHttp(config)
    say = progress or (lambda message: None)
    now = now or timezone.now()
    run.status = 'processing'
    run.started_at = timezone.now()
    run.field_profile = config.profile
    run.save(update_fields=['status', 'started_at', 'field_profile'])
    try:
        if run.mode == 'full':
            _catalogue_pass(run, config, http, now, budget, say)
            run.linked_count = link_to_venues(config)
        if run.mode != 'screen':
            _enrichment_pass(run, config, http, now, budget, say)
        _screening_pass(run, config, budget, say, page_fetcher=page_fetcher, read_pages=read_pages)
        run.pending_after = pending_enrichment(config, now).count()
        run.status = 'completed'
        run.summary = _summary(run)
    except Exception as exc:  # keep what was saved; report clearly
        logger.exception('Venue index run failed')
        _record_error(run, 'run', exc)
        run.status = 'failed'
        run.summary = f'Stopped: {exc}'[:500]
    run.completed_at = timezone.now()
    run.save()
    return run


def _catalogue_pass(run, config, http, now, budget, say=lambda m: None):
    catalogue = OpenAlexCatalogue(http, config)
    this_year = now.year
    seen_ids = set()
    kept = 0
    cutoff = None

    def page_done(_event):
        run.pages_fetched = catalogue.pages
        run.save()
        say(f'OpenAlex page {catalogue.pages}: {run.records_seen:,} journals read, {kept:,} in your fields')
    catalogue.progress = page_done

    say('Reading the journal catalogue from OpenAlex (largest journals first)…')
    try:
        for source in catalogue.sources(budget):
            run.records_seen += 1
            keep, details = assess_scope(source, config, this_year=this_year)
            if not keep:
                run.out_of_scope += 1
                # A journal already in the index that no longer qualifies (e.g. the scope was narrowed)
                # is taken out, unless an admin linked it to a live venue.
                run.removed_count += IndexedVenue.objects.filter(
                    openalex_id=_short_id(source.get('id')), venue__isnull=True, excluded=False).delete()[0]
                continue
            openalex_id = _short_id(source.get('id'))
            fields = spine_fields(source, details)
            if not openalex_id or not fields['title']:
                continue
            seen_ids.add(openalex_id)
            _upsert(run, config, openalex_id, fields, now)
            kept += 1
            if kept >= config.max_records:
                # Results come largest first, so the cap is a size cutoff: the index is the N
                # most-published journals in the fields, and everything smaller is outside it.
                cutoff = int(source.get('works_count') or 0)
                catalogue.complete = False
                say(f'Reached {config.max_records:,} journals (VENUE_INDEX_MAX_RECORDS): keeping the '
                    f'{config.max_records:,} most-published journals in your fields (cutoff {cutoff:,} works).')
                break
    except IndexSourceError as exc:
        _record_error(run, 'openalex', exc)
        say(f'OpenAlex problem: {exc}')
        catalogue.complete = False
    run.pages_fetched = catalogue.pages
    run.catalogue_method = catalogue.method
    reached_cutoff = cutoff is not None and catalogue.method == 'subfield_filter' and not run.errors
    run.catalogue_complete = (catalogue.complete or reached_cutoff) and budget()
    run.size_cutoff = cutoff if reached_cutoff else None
    if run.catalogue_complete:
        unseen = IndexedVenue.objects.filter(field_profile=config.profile).exclude(openalex_id__in=seen_ids)
        if reached_cutoff:
            # Smaller than the cutoff: no longer among the N largest, so out of the index (unless linked).
            small = [r.id for r in unseen.only('id', 'metrics')
                     if int((r.metrics or {}).get('works_count') or 0) <= cutoff]
            run.removed_count += IndexedVenue.objects.filter(id__in=small, venue__isnull=True, excluded=False).delete()[0]
            unseen = unseen.exclude(id__in=small)  # a linked small journal stays, and is not 'missing'
        # Only a complete pass can say a journal left the catalogue. Keep it, flag it.
        run.flagged_missing = unseen.filter(missing_since__isnull=True).update(missing_since=now)
    run.save()
    method = ' (keyword search: OpenAlex did not accept the subject filter)' if catalogue.method == 'keyword_search' else ''
    removed = f', {run.removed_count:,} no longer in scope removed' if run.removed_count else ''
    say(f'Catalogue done{method}: {run.created_count:,} new, {run.updated_count:,} refreshed, '
        f'{run.out_of_scope:,} outside your fields skipped{removed}.')


def _upsert(run, config, openalex_id, fields, now):
    with transaction.atomic():
        record = IndexedVenue.objects.select_for_update().filter(openalex_id=openalex_id).first()
        if record is None:
            IndexedVenue.objects.create(openalex_id=openalex_id, field_profile=config.profile,
                                        normalized_title=normalize_name(fields['title']), first_imported_at=now,
                                        last_refreshed_at=now, checked={'openalex': now.isoformat()}, **fields)
            run.created_count += 1
            return
        for key, value in fields.items():
            if key == 'doaj_listed' and record.doaj.get('listed') is not None:
                continue  # DOAJ's own answer outranks OpenAlex's copy of it
            setattr(record, key, value)
        record.normalized_title = normalize_name(fields['title'])
        record.field_profile = config.profile
        record.last_refreshed_at = now
        record.missing_since = None
        record.checked = {**(record.checked or {}), 'openalex': now.isoformat()}
        record.save()
        run.updated_count += 1


def _enrichment_pass(run, config, http, now, budget, say=lambda m: None):
    """Crossref/DOAJ checks. Network calls run in parallel (up to config.workers);
    every database write happens here, on the calling thread."""
    from concurrent.futures import ThreadPoolExecutor

    due = list(pending_enrichment(config, now).order_by('enriched_at', 'title').values_list('id', flat=True))
    if not due:
        return
    pool = 'parallel, polite pool' if config.polite else 'one at a time; set VENUE_INDEX_CONTACT_EMAIL to go 3x faster'
    say(f'Checking {len(due):,} journals against Crossref and DOAJ ({pool})…')

    def fetch(record):
        try:
            return record, fetch_enrichment(record, http, timezone.now()), None
        except IndexSourceError as exc:
            return record, None, exc

    failures_in_a_row = 0
    batch_size = max(1, config.workers) * 8
    with ThreadPoolExecutor(max_workers=max(1, config.workers)) as executor:
        for start in range(0, len(due), batch_size):
            if not budget():
                say('Time limit reached; the remaining checks continue in the next run.')
                break
            records = list(IndexedVenue.objects.filter(id__in=due[start:start + batch_size]).order_by('enriched_at', 'title'))
            stop = False
            for record, done, exc in executor.map(fetch, records):
                if done is not None:
                    done.save()
                    run.enriched_count += 1
                    failures_in_a_row = 0
                    continue
                failures_in_a_row += 1
                IndexedVenue.objects.filter(id=record.id).update(last_error=str(exc)[:300])
                _record_error(run, 'enrichment', f'{record.title}: {exc}')
                if failures_in_a_row >= 5:
                    _record_error(run, 'enrichment', 'Five lookups failed in a row; stopping enrichment for this run.')
                    say('Five lookups failed in a row; stopping checks for now (they are retried next run).')
                    stop = True
                    break
            run.save()
            say(f'Checked {min(start + batch_size, len(due)):,} of {len(due):,}')
            if stop:
                break


def _screening_pass(run, config, budget, say, *, page_fetcher=None, read_pages=True):
    """Step 3: flag journals for human review (catalogue signals, then evidence from their own pages)."""
    from .index_screening import gather_page_evidence, review_queue, screen_all
    run.screened_count, _ = screen_all(config.profile, say=say)
    if read_pages and os.getenv('VENUE_INDEX_READ_PAGES', 'true').strip().lower() not in {'0', 'false', 'no', 'off'}:
        gather_page_evidence(run, config.profile, budget, fetcher=page_fetcher, say=say)
    run.flagged_count = review_queue(config.profile).count()
    run.save()


def _summary(run):
    parts = []
    if run.mode == 'full':
        parts.append(f'{run.created_count} new and {run.updated_count} refreshed journals '
                     f'({run.out_of_scope} outside the target fields skipped)')
        if run.removed_count:
            parts.append(f'{run.removed_count} no longer in scope removed')
        if run.size_cutoff is not None:
            parts.append(f'index holds the most-published journals down to {run.size_cutoff:,} works')
        if not run.catalogue_complete:
            parts.append('catalogue pass stopped early, so nothing was flagged as missing')
        elif run.flagged_missing:
            parts.append(f'{run.flagged_missing} no longer in the catalogue (kept, flagged)')
        if run.linked_count:
            parts.append(f'{run.linked_count} linked to live venues')
    if run.mode != 'screen':
        parts.append(f'{run.enriched_count} checked against Crossref/DOAJ')
    if run.screened_count:
        parts.append(f'{run.flagged_count} need review'
                     + (f' ({run.pages_checked} journals\' pages read for evidence)' if run.pages_checked else ''))
    if run.pending_after:
        parts.append(f'{run.pending_after} still to check (continues next run)')
    return '; '.join(parts) + '.'


def coverage_counts(profile):
    qs = IndexedVenue.objects.filter(field_profile=profile)
    totals = qs.aggregate(
        total=Count('id'),
        open_access=Count('id', filter=Q(open_access=True)),
        doaj=Count('id', filter=Q(doaj_listed=True)),
        crossref=Count('id', filter=Q(crossref__registered=True)),
        linked=Count('id', filter=Q(venue__isnull=False)),
        missing=Count('id', filter=Q(missing_since__isnull=False)),
        not_enriched=Count('id', filter=Q(enriched_at__isnull=True)),
    )
    by_field = list(qs.exclude(primary_subfield='').values('primary_subfield')
                    .annotate(count=Count('id')).order_by('-count')[:20])
    return {**totals, 'by_field': [{'name': r['primary_subfield'], 'count': r['count']} for r in by_field]}
