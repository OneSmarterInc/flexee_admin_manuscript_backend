"""Exclusion screening for the venue index (build plan step 3).

Automated screening only ever *flags* a journal for review; a person decides, with the evidence
in front of them. Every exclusion is a record with criteria, evidence and a date, and every
exclusion is reversible. Wording is about criteria a venue failed, never a label: nothing here
calls a journal "predatory", and excluded journals are never listed publicly.

Two layers, no AI:
  1. Catalogue signals already in the index (Crossref, DOAJ, ISSN, history, subject mix, blocklist).
  2. Evidence from the journal's own pages: exact quotes of guaranteed-acceptance promises,
     publication-in-days promises and metrics from unrecognised ranking bodies.
"""
import logging
import os
import re
import time
from dataclasses import replace
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ..models import BlockedPublisher, IndexedVenue, IndexReviewDecision, Venue
from .venue_discovery import normalize_name

logger = logging.getLogger(__name__)

# Exclusion criteria a reviewer can record (build plan 4.2). Codes are stable; labels are for the admin.
CRITERIA = {
    'guaranteed_acceptance': 'Guaranteed or stated rapid acceptance',
    'rapid_review_promise': 'Review or publication promised within days',
    'undisclosed_fees': 'Fees not disclosed before submission',
    'unverifiable_board': 'Editorial board without verifiable affiliations',
    'overbroad_scope': 'Scope covers unrelated disciplines',
    'unresolvable_address': 'Publisher address does not resolve',
    'templated_site': 'Copied or templated site shared across many titles',
    'invented_metrics': 'Metrics from unrecognised ranking bodies',
    'internal_blocklist': 'Publisher is on the internal blocklist',
    'not_peer_reviewed': 'Not a peer-reviewed journal (magazine, newsletter or trade title)',
}

# Which automated flag points at which criterion (used to pre-fill the reviewer's form).
FLAG_TO_CRITERION = {
    'guaranteed_acceptance': 'guaranteed_acceptance',
    'rapid_review_promise': 'rapid_review_promise',
    'rapid_review': 'rapid_review_promise',
    'invented_metrics': 'invented_metrics',
    'overbroad_scope': 'overbroad_scope',
    'blocked_publisher': 'internal_blocklist',
}

STRONG, MODERATE, WEAK = 3, 2, 1
FLAG_THRESHOLD = 3          # catalogue points that put a journal in the review queue
EVIDENCE_CODES = {'guaranteed_acceptance', 'rapid_review_promise', 'invented_metrics', 'blocked_publisher'}
PAGE_RECHECK_DAYS = 30


def _flag(code, label, weight, detail='', *, kind='negative', evidence_url='', quote='', source='catalogue'):
    return {'code': code, 'label': label, 'kind': kind, 'weight': weight, 'detail': detail,
            'evidence_url': evidence_url, 'quote': quote, 'source': source}


def blocked_publisher_names():
    return set(BlockedPublisher.objects.values_list('normalized_name', flat=True))


def catalogue_flags(record, blocked, *, this_year):
    """Signals from data already in the index. Returns (negatives, positives)."""
    neg, pos = [], []
    crossref = record.crossref or {}
    doaj = record.doaj or {}
    checks = record.issn_checks or {}
    metrics = record.metrics or {}

    if crossref.get('checked_at') and not crossref.get('registered'):
        neg.append(_flag('no_crossref', 'No DOIs registered with Crossref', MODERATE,
                         'Scholarly journals almost always register DOIs; magazines and some '
                         'questionable titles do not.'))
    if not (record.issn_l or record.issns):
        neg.append(_flag('no_issn', 'No ISSN', MODERATE, 'No ISSN is recorded in any catalogue.'))
    elif checks and checks.get('valid_checksums') is False:
        neg.append(_flag('issn_invalid', 'ISSN fails its check digit', STRONG,
                         f'ISSN {record.issn_l or ", ".join(record.issns)} is not a valid ISSN.'))
    if not record.publisher.strip():
        neg.append(_flag('publisher_unknown', 'Publisher not stated', WEAK, 'No catalogue names a publisher.'))

    years = [y for y in (record.first_publication_year, crossref.get('first_year')) if y]
    first_year = min(years) if years else None
    if first_year and first_year > this_year - 2:
        neg.append(_flag('short_history', 'Publishing for less than two years', WEAK, f'First publication {first_year}.'))

    weeks = doaj.get('publication_time_weeks')
    if isinstance(weeks, (int, float)) and 0 < weeks <= 3:
        neg.append(_flag('rapid_review', 'Very fast submission-to-publication time', MODERATE,
                         f'DOAJ lists {int(weeks)} week{"s" if weeks != 1 else ""} from submission to publication.',
                         evidence_url=doaj.get('review_url') or doaj.get('journal_url') or ''))

    fields = {str(s.get('id', ''))[:2] for s in (record.subfields or []) if (s.get('share') or 0) >= 0.05}
    top_share = max([s.get('share') or 0 for s in (record.subfields or [])] or [0])
    if len(fields) >= 4 and top_share < 0.3:
        names = ', '.join(s.get('name', '') for s in (record.subfields or [])[:4])
        neg.append(_flag('overbroad_scope', 'Scope spans unrelated disciplines', MODERATE,
                         f'Output spread across {len(fields)} unrelated fields ({names}).'))

    if record.open_access and not record.doaj_listed and (record.apc_usd or 0) > 0:
        neg.append(_flag('apc_not_in_doaj', 'Charges authors but is not in DOAJ', WEAK,
                         f'Article charge about USD {record.apc_usd}; not listed in the Directory of Open Access Journals.'))

    if record.publisher and normalize_name(record.publisher) in blocked:
        neg.append(_flag('blocked_publisher', 'Publisher is on the internal blocklist', STRONG,
                         f'{record.publisher} was blocked after an earlier review.', source='blocklist'))

    # Positive signals: shown to the reviewer, never used to exclude.
    if record.doaj_listed:
        pos.append(_flag('doaj_listed', 'Listed in DOAJ', 0, kind='positive',
                         evidence_url=doaj.get('journal_url', '')))
    if crossref.get('registered') and (crossref.get('total_dois') or 0) >= 100 and first_year and first_year <= this_year - 5:
        pos.append(_flag('crossref_history', 'Long Crossref history', 0,
                         f'{crossref.get("total_dois"):,} DOIs since {crossref.get("first_year") or first_year}.', kind='positive'))
    if metrics.get('is_core'):
        pos.append(_flag('core_source', 'Core source in major indexes (CWTS)', 0, kind='positive'))
    if checks.get('crossref_agrees'):
        pos.append(_flag('issn_confirmed', 'ISSN confirmed by Crossref', 0, kind='positive'))
    return neg, pos


# ---------------------------------------------------------------------------
# Evidence from the journal's own pages (exact quotes, no AI)
# ---------------------------------------------------------------------------

_TIME = r'(?:within|in|under)\s+(?:(?:24|48|72|96)\s*(?:hours?|hrs?)|(?:[1-9]|1[0-4])\s*(?:working\s+)?days?|(?:one|two|three|four|five|seven|ten)\s+days?)'
PAGE_PATTERNS = [
    ('guaranteed_acceptance', 'Guaranteed or stated rapid acceptance', [
        r'\b(?:guarantee[ds]?|assured|sure[- ]shot)\s+(?:of\s+)?(?:paper\s+)?(?:acceptance|publication)\b',
        r'\b100\s*%\s*(?:acceptance|publication)\b',
        r'\bacceptance\s+(?:is\s+)?guaranteed\b',
    ]),
    ('rapid_review_promise', 'Publication or acceptance promised within days', [
        r'\b(?:publication|publish(?:ed)?|acceptance|accepted|accept)\b[^.\n]{0,40}?\b' + _TIME,
        r'\b(?:fast|rapid|quick)[- ]track\s+publication\b[^.\n]{0,40}?\b' + _TIME,
    ]),
    ('invented_metrics', 'Metrics from unrecognised ranking bodies', [
        r'\b(?:Global|Universal|General|Scholarly|Cosmos|ISRA)\s+Impact\s+Factor\b',
        r'\bScientific\s+Journal\s+Impact\s+Factor\b', r'\bSJIF\b',
        r'\bIndex\s+Copernicus\s+Value\b', r'\bICV\s*[:=]?\s*\d',
        r'\bInternational\s+Scientific\s+Indexing\b', r'\bCitefactor\b', r'\bAdvanced\s+Sciences\s+Index\b',
        r'\bDirectory\s+of\s+Indexing\s+and\s+Impact\s+Factor\b', r'\bInfoBase\s+Index\b', r'\bI2OR\b', r'\bIIFS\b',
    ]),
]
_COMPILED = [(code, label, [re.compile(p, re.IGNORECASE) for p in patterns]) for code, label, patterns in PAGE_PATTERNS]


def _quote(text, start, end, width=90):
    left = max(0, start - width)
    right = min(len(text), end + width)
    snippet = re.sub(r'\s+', ' ', text[left:right]).strip()
    return ('…' if left else '') + snippet + ('…' if right < len(text) else '')


def scan_page_text(url, text):
    """Exact-quote evidence from one page's visible text. At most one flag per code."""
    found = []
    for code, label, patterns in _COMPILED:
        for pattern in patterns:
            match = pattern.search(text or '')
            if match:
                found.append(_flag(code, label, STRONG, f'Found on the journal\'s own page: "{match.group(0)}".',
                                   evidence_url=url, quote=_quote(text, match.start(), match.end()), source='page'))
                break
    return found


def page_evidence(record, fetcher):
    """Fetch the journal's homepage plus up to two author/fee pages and scan them. Returns flags."""
    from .venue_discovery import DiscoveryFetchError, find_submission_links
    urls = [u for u in [record.homepage_url, (record.doaj or {}).get('guidelines_url')] if u]
    if not urls:
        return [], 'No homepage on record.'
    flags, seen_codes, pages, problem = [], set(), [], ''
    try:
        first = fetcher.fetch(urls[0])
        pages.append(first)
        extra = [u for u in urls[1:]] + list(find_submission_links(first, 2))
    except DiscoveryFetchError as exc:
        problem, extra = str(exc), urls[1:]
    for url in extra[:2]:
        try:
            pages.append(fetcher.fetch(url))
        except DiscoveryFetchError:
            continue
    for page in pages:
        for flag in scan_page_text(page.url, page.text):
            if flag['code'] not in seen_codes:
                seen_codes.add(flag['code'])
                flags.append(flag)
    return flags, ('' if pages else problem or 'The pages could not be read.')


# ---------------------------------------------------------------------------
# Screening a record
# ---------------------------------------------------------------------------

def screen_record(record, blocked, *, now=None, save=True):
    """Recompute flags and the record's place in the review queue. Never excludes anything."""
    now = now or timezone.now()
    neg, pos = catalogue_flags(record, blocked, this_year=now.year)
    neg += [dict(f) for f in (record.page_flags or [])]
    points = sum(f['weight'] for f in neg)
    codes = {f['code'] for f in neg}
    has_evidence = bool(codes & EVIDENCE_CODES)

    if record.excluded:
        status = 'excluded'
        # Reversible: if the criteria that caused the exclusion are no longer detected, suggest a re-review.
        decided = set((record.exclusion_reason or {}).get('criteria') or [])
        detectable = {c for c in FLAG_TO_CRITERION.values()}
        watched = decided & detectable
        detected = {FLAG_TO_CRITERION[c] for c in codes if c in FLAG_TO_CRITERION}
        record.rereview_suggested = bool(watched) and not (watched & detected)
    elif record.screening_status == 'kept' and codes <= set(record.kept_flag_codes or []):
        status = 'kept'  # nothing new since a reviewer kept it
        record.rereview_suggested = False
    else:
        status = 'flagged' if (points >= FLAG_THRESHOLD or has_evidence) else 'clear'
        record.rereview_suggested = False

    record.screening_flags = neg + pos
    record.screening_points = min(points, 32767)
    record.screening_status = status
    record.screened_at = now
    if save:
        record.save(update_fields=['screening_flags', 'screening_points', 'screening_status', 'screened_at',
                                   'rereview_suggested', 'updated_at'])
    return record


def screen_all(profile, *, now=None, say=lambda m: None):
    """Catalogue screening of every record in a profile (database only, takes seconds)."""
    now = now or timezone.now()
    blocked = blocked_publisher_names()
    screened = flagged = 0
    for record in IndexedVenue.objects.filter(field_profile=profile).iterator():
        before = record.screening_status
        screen_record(record, blocked, now=now)
        screened += 1
        if record.screening_status == 'flagged' and before != 'flagged':
            flagged += 1
    say(f'Screened {screened:,} journals: {review_queue(profile).count():,} need review '
        f'({flagged:,} newly flagged), {IndexedVenue.objects.filter(field_profile=profile, excluded=True).count():,} excluded.')
    return screened, flagged


def review_queue(profile):
    return IndexedVenue.objects.filter(field_profile=profile).filter(
        Q(screening_status='flagged') | Q(rereview_suggested=True))


def page_candidates(profile, now):
    """Journals worth reading pages for: any catalogue concern, or charging authors outside DOAJ.
    Pages are re-read after PAGE_RECHECK_DAYS."""
    stale = now - timedelta(days=PAGE_RECHECK_DAYS)
    return (IndexedVenue.objects.filter(field_profile=profile, missing_since__isnull=True)
            .exclude(homepage_url='')
            .filter(Q(screening_points__gte=1) | Q(open_access=True, doaj_listed=False))
            .filter(Q(pages_checked_at__isnull=True) | Q(pages_checked_at__lt=stale)))


def gather_page_evidence(run, profile, budget, *, fetcher=None, say=lambda m: None, now=None):
    """Read the official pages of candidate journals and record exact-quote evidence."""
    from .venue_discovery import DiscoveryConfig, SafeFetcher
    now = now or timezone.now()
    due = list(page_candidates(profile, now).order_by('pages_checked_at', '-screening_points', 'title')
               .values_list('id', flat=True))
    if not due:
        return
    if fetcher is None:
        config = DiscoveryConfig.from_env()
        config = replace(config, max_pages_per_run=len(due) * 4 + 10,
                         per_domain_delay=float(os.getenv('VENUE_INDEX_PAGE_DELAY_SECONDS', '1.0') or 0))
        fetcher = SafeFetcher(config)
    say(f'Reading the official pages of {len(due):,} journals for evidence (exact quotes only)…')
    blocked = blocked_publisher_names()
    for index, record_id in enumerate(due, 1):
        if not budget():
            say('Time limit reached; the remaining pages are read in the next run.')
            break
        record = IndexedVenue.objects.filter(id=record_id).first()
        if record is None:
            continue
        try:
            flags, problem = page_evidence(record, fetcher)
        except Exception as exc:  # one bad site must not stop the run
            logger.warning('Page evidence failed for %s: %s', record.title, exc)
            flags, problem = [], str(exc)[:200]
        record.page_flags = flags
        record.pages_checked_at = timezone.now()
        if problem and not flags:
            record.last_error = f'Pages not read: {problem}'[:300]
        record.save(update_fields=['page_flags', 'pages_checked_at', 'last_error', 'updated_at'])
        screen_record(record, blocked)
        run.pages_checked += 1
        if index % 10 == 0 or index == len(due):
            run.save()
            say(f'Pages read for {index:,} of {len(due):,} journals')
        time.sleep(0)  # cooperative point; pacing is per domain inside the fetcher


# ---------------------------------------------------------------------------
# Decisions (a person, never the screening)
# ---------------------------------------------------------------------------

class DecisionError(ValueError):
    pass


def decide(record, *, decision, user_email, criteria=None, evidence_urls=None, note='', block_publisher=False):
    criteria = [c for c in (criteria or []) if c in CRITERIA]
    evidence_urls = [str(u).strip()[:1000] for u in (evidence_urls or []) if str(u).strip().startswith(('http://', 'https://'))][:10]
    note = str(note or '').strip()[:2000]
    now = timezone.now()
    if decision not in {'exclude', 'keep', 'restore'}:
        raise DecisionError('decision must be exclude, keep or restore')
    if decision == 'exclude':
        if not criteria:
            raise DecisionError('Choose at least one criterion the journal fails.')
        if not evidence_urls:
            raise DecisionError('Add at least one evidence link (the page that shows the problem).')
    if decision == 'restore' and not record.excluded:
        raise DecisionError('This journal is not excluded.')
    if decision == 'keep' and record.excluded:
        raise DecisionError('Restore an excluded journal instead of keeping it.')

    with transaction.atomic():
        record = IndexedVenue.objects.select_for_update().get(id=record.id)
        flags_snapshot = list(record.screening_flags or [])
        if decision == 'exclude':
            record.excluded = True
            record.exclusion_reason = {'criteria': criteria, 'evidence_urls': evidence_urls,
                                       'decided_at': now.isoformat(), 'decided_by': user_email, 'note': note}
            record.screening_status = 'excluded'
        elif decision == 'restore':
            record.excluded = False
            record.exclusion_reason = {}
            record.screening_status = 'kept'
            record.kept_flag_codes = sorted({f['code'] for f in flags_snapshot if f.get('kind') == 'negative'})
        else:  # keep
            record.screening_status = 'kept'
            record.kept_flag_codes = sorted({f['code'] for f in flags_snapshot if f.get('kind') == 'negative'})
        record.rereview_suggested = False
        record.save()
        if record.venue_id:  # a live venue follows the index decision, so authors never see it
            Venue.objects.filter(id=record.venue_id).update(
                excluded=record.excluded, exclusion_reason=record.exclusion_reason, updated_at=now)
        IndexReviewDecision.objects.create(record=record, decision=decision, criteria=criteria,
                                           evidence_urls=evidence_urls, note=note, flags_snapshot=flags_snapshot,
                                           decided_by=user_email, decided_at=now)
        blocked_now = None
        if decision == 'exclude' and block_publisher and record.publisher.strip():
            blocked_now, _ = BlockedPublisher.objects.get_or_create(
                normalized_name=normalize_name(record.publisher),
                defaults={'name': record.publisher[:300], 'note': f'Blocked when excluding {record.title}.',
                          'added_by': user_email})
    if blocked_now:
        # Other titles from the same publisher go to the review queue (they are not excluded automatically).
        names = blocked_publisher_names()
        for other in IndexedVenue.objects.filter(field_profile=record.field_profile, excluded=False).exclude(id=record.id):
            if other.publisher and normalize_name(other.publisher) == blocked_now.normalized_name:
                screen_record(other, names)
    return record


def excluded_match(name, organization=''):
    """For Venue Discovery: an excluded index record (or blocked publisher) matching this venue, so a
    later crawl can never quietly re-add it. Returns a short reason or ''."""
    normalized = normalize_name(name)
    if normalized and IndexedVenue.objects.filter(normalized_title=normalized, excluded=True).exists():
        return 'This journal was excluded from the venue index after review. Restore it in Venue Index first.'
    if organization and BlockedPublisher.objects.filter(normalized_name=normalize_name(organization)).exists():
        return 'This publisher is on the internal blocklist. Remove it from the blocklist in Venue Index first.'
    return ''
