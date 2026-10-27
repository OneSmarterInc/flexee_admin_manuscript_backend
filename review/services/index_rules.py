"""Venue index, layer 2: read each journal's rules from its own pages (build plan step 4).

For journals in the first target field, the existing Venue Discovery pipeline reads the official
author-guidelines page (from DOAJ) or the homepage, asks the AI (local Ollama by default) to extract
the rules a VenueAgentConfig needs, and keeps only rules whose quotes appear on the fetched pages.

Reading never publishes anything. A journal whose rules were read is "ready"; an admin approves it
("Publish"), which creates the live venue labelled "Checked from official pages" (verified_index).
"""
import logging
import os
import re
from dataclasses import replace
from datetime import timedelta
from urllib.parse import urlsplit

from django.db.models import Case, F, IntegerField, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from ..models import ABDCJournal, IndexedVenue

logger = logging.getLogger(__name__)

# Build plan 3.1: start with the field Vikram can judge directly. OpenAlex subfield ids.
FIELDS = {
    'information-systems': {'label': 'Information Systems and MIS', 'subfields': {'1404', '1710', '1802'}},
    'strategy-management': {'label': 'Strategy & Management and OB/HRM', 'subfields': {'1408', '1407', '1403', '1400'}},
    'operations': {'label': 'Operations research and supply chain', 'subfields': {'1803', '1800', '2209'}},
}
DEFAULT_FIELD = 'information-systems'
RULE_FIELDS = ('article_types', 'structured_desk_rejection_rules', 'required_submission_items', 'aims_scope',
               'submission_types')
RETRY_AFTER_DAYS = 30
BLOCKED_RETRY_DAYS = 90   # publishers that refuse automated reading rarely change their mind quickly
STATUS_TEXT = {'ready': 'rules ready for approval', 'incomplete': 'rules not found on the pages',
               'failed': 'pages could not be read', 'blocked': 'site blocks automated reading'}


class RulesModelUnavailable(Exception):
    """The AI model cannot be reached at all: stop the run instead of failing every journal."""


class RulesTimeUp(Exception):
    """The run's time limit leaves too little time to read this journal; it is left for the next run."""


def _env_int(name, default, low, high):
    try:
        return max(low, min(int(os.getenv(name, str(default))), high))
    except ValueError:
        return default


def rules_field():
    key = os.getenv('VENUE_INDEX_RULES_FIELD', DEFAULT_FIELD).strip() or DEFAULT_FIELD
    field = FIELDS.get(key, FIELDS[DEFAULT_FIELD])
    override = {x.strip() for x in os.getenv('VENUE_INDEX_RULES_SUBFIELDS', '').split(',') if x.strip()}
    return key, field['label'], (override or field['subfields'])


def rules_candidates(profile, now=None):
    """Journals in the first field whose rules should be read now: not excluded, not waiting for an
    exclusion decision, and either not yet live (never read, a failed read older than 30 days, a
    blocking site after 90 days) or live and due for the quarterly re-read (build plan step 7)."""
    from .freshness import rules_refresh_days
    now = now or timezone.now()
    _key, _label, subfields = rules_field()
    retry = now - timedelta(days=RETRY_AFTER_DAYS)
    retry_blocked = now - timedelta(days=BLOCKED_RETRY_DAYS)
    refresh = now - timedelta(days=rules_refresh_days())
    not_live = Q(venue__isnull=True) & (
        Q(rules_status='not_read') | Q(rules_status__in=['failed', 'incomplete'], rules_read_at__lt=retry)
        | Q(rules_status='blocked', rules_read_at__lt=retry_blocked))
    live_due = Q(venue__isnull=False, rules_read_at__lt=refresh)
    return (IndexedVenue.objects.filter(field_profile=profile, excluded=False, missing_since__isnull=True,
                                        screening_status__in=['clear', 'kept'], subfields__0__id__in=list(subfields))
            .exclude(Q(homepage_url='') & (Q(doaj__guidelines_url__isnull=True) | Q(doaj__guidelines_url='')))
            .filter(not_live | live_due))


ABDC_RANK = {'A*': 0, 'A': 1, 'B': 2, 'C': 3}
UNRATED_RANK = len(ABDC_RANK)


def abdc_first_enabled():
    return os.getenv('VENUE_INDEX_RULES_ABDC_FIRST', 'true').strip().lower() not in {'0', 'false', 'no', 'off'}


def _abdc_rank_subquery():
    """Best ABDC rating for a journal in the newest imported edition, as a sort rank.

    A journal can hold rows from several editions. Only the newest is used, so a title downgraded
    in a later list is not still sorted on an older, better rating. With no ABDC data imported at
    all there is no edition, the subquery yields nothing, and every journal ranks equal: the order
    below then falls back to exactly what it was before ABDC existed.
    """
    edition = ABDCJournal.objects.order_by('-edition').values_list('edition', flat=True).first()
    if edition is None:
        return None
    best = (ABDCJournal.objects
            .filter(indexed_venue_id=OuterRef('pk'), edition=edition)
            .annotate(_rank=Case(*[When(rating=value, then=Value(rank)) for value, rank in ABDC_RANK.items()],
                                 default=Value(UNRATED_RANK), output_field=IntegerField()))
            .order_by('_rank').values('_rank')[:1])
    return Coalesce(Subquery(best, output_field=IntegerField()), Value(UNRATED_RANK))


def reading_order(queryset):
    """ABDC rating first, then never-read journals, then journals with a DOAJ author-guidelines page
    (usually readable), then the most-published.

    The DOAJ-first tiebreaker favours journals that are easy to read, and open-access directories
    carry few of the journals authors in this field target: of 542 Information Systems records in the
    October index only 29 held an ABDC rating. Left on its own that ordering reads the cheapest
    journals rather than the ones a submission plan needs, so rating leads and readability breaks
    ties inside each rating band. Set VENUE_INDEX_RULES_ABDC_FIRST=false to restore the old order.
    """
    has_guidelines = Q(doaj__guidelines_url__isnull=False) & ~Q(doaj__guidelines_url='')
    queryset = queryset.annotate(_guidelines_first=Case(When(has_guidelines, then=Value(0)), default=Value(1),
                                                        output_field=IntegerField()))
    sort = [F('rules_read_at').asc(nulls_first=True), '_guidelines_first', '-metrics__works_count', 'title']
    rank = _abdc_rank_subquery() if abdc_first_enabled() else None
    if rank is None:
        return queryset.order_by(*sort)
    # Re-reads stay behind first reads: a quarterly refresh of an A* journal must not displace a
    # journal whose rules have never been read at all.
    return (queryset.annotate(_abdc_rank=rank)
            .order_by(F('rules_read_at').asc(nulls_first=True), '_abdc_rank', *sort[1:]))


def entry_url(record):
    return (record.doaj or {}).get('guidelines_url') or record.homepage_url


def entry_urls(record):
    """The official URLs to try, in order.

    DOAJ's author-guidelines page first, then any publisher route that is known to answer us, then
    the homepage. The middle group exists because SAGE and INFORMS refuse our crawler on the host
    OpenAlex gives us while serving the same guidelines from a host that answers; the code needed to
    build those addresses is read out of the homepage we already hold. See publisher_routes.
    """
    from .publisher_routes import candidates_for
    urls = []
    for url in ((record.doaj or {}).get('guidelines_url'), *candidates_for(record), record.homepage_url):
        if url and url not in urls:
            urls.append(url)
    return urls


def site_of(url):
    host = (urlsplit(url or '').hostname or '').lower()
    return host[4:] if host.startswith('www.') else host


def _is_page_signal(evidence):
    """Evidence found by fixed phrases on the page (a status line, a type mentioned), not read by the
    model. It is a real quote, but it is not the journal's rules."""
    claim = str(evidence.get('claim') or '')
    return evidence.get('origin') == 'page_signal' or claim.startswith('The official page ')


# Which quoted evidence supports which rule field (the model names the field in its own words).
QUOTE_FIELDS = {
    'aims_scope': ('scope', 'aim'),
    'article_types': ('type',),
    'required_submission_items': ('item', 'require', 'checklist', 'declaration', 'statement'),
    # Review 4, Y9: a deadline is a dated claim an author plans around, and author_config_view shows it as
    # a live one. It is published only when the model quoted the page it came from.
    # Review 5, R5-11: a call for papers is where a real deadline is quoted from, so it counts as evidence
    # for one; without it, verified call deadlines were wiped. 'date' is dropped — as a substring it also
    # matched 'last_updated', which quotes nothing about a deadline.
    'deadlines': ('deadline', 'due', 'closing', 'closes', 'submission window', 'call for papers',
                  'call_for_papers'),
}


def _get(item, name):
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _names_field(evidence_field, word):
    """Does the model's own name for this evidence start a word with `word`?

    Review 5, R5-11: matched as a bare substring, 'date' also sat inside 'last_updated' and 'type' inside
    'prototype', so evidence about one thing counted as a quote for another. Matching from the start of a
    word keeps the stems the rules rely on — 'item' still matches 'required_submission_items', 'require'
    matches 'required' — while 'date' no longer matches 'updated'. Underscores and hyphens read as spaces,
    so 'call for papers' matches a field named 'call_for_papers'.
    """
    haystack = re.sub(r'[^a-z0-9]+', ' ', evidence_field.lower()).strip()
    needle = re.sub(r'[^a-z0-9]+', ' ', word.lower()).strip()
    if not haystack or not needle:
        return False
    return re.search(r'\b' + re.escape(needle).replace(r'\ ', ' '), haystack) is not None


def quoted_fields(item):
    """The rule fields backed by their own quote on the official pages.

    Word and reference limits and required sections are checked against the page text when they are
    extracted (number with its unit, each section name), so they count when present. Scope, article
    types and required items count only when the model quoted the page for that field."""
    model_evidence = [e for e in (_get(item, 'source_evidence') or [])
                      if isinstance(e, dict) and not _is_page_signal(e)]
    quoted = set()
    if _get(item, 'structured_desk_rejection_rules'):
        quoted.add('structured_desk_rejection_rules')
    for field, words in QUOTE_FIELDS.items():
        if not _get(item, field):
            continue
        if any(any(_names_field(str(e.get('field') or ''), w) for w in words) for e in model_evidence):
            quoted.add(field)
    return quoted, model_evidence


def keep_quoted_rules(candidate):
    """Index reading: drop any rule field without its own quote, so nothing unverified is published.
    Article types found only by fixed phrases on the page keep just those types."""
    quoted, _model = quoted_fields(candidate)
    out = dict(candidate)
    if 'aims_scope' not in quoted:
        out['aims_scope'] = ''
    if 'required_submission_items' not in quoted:
        out['required_submission_items'] = []
    if 'deadlines' not in quoted:
        out['deadlines'] = {}
    if 'article_types' not in quoted:
        from .venue_discovery import TYPE_LABELS
        signal_types = []
        for e in out.get('source_evidence') or []:
            key = e.get('type_key') if isinstance(e, dict) and _is_page_signal(e) else None
            if key in TYPE_LABELS and key not in signal_types:
                signal_types.append(key)
        out['submission_types'] = signal_types
        out['article_types'] = [TYPE_LABELS[k] for k in signal_types]
    return out


def rules_found(item, min_confidence):
    """Did the read find real rules? Official pages, a quote the model took from them, and at least
    one rule a manuscript can be checked against that is backed by its own quote (audit item 4).
    Phrases the system spotted itself (page signals) never count as rules found."""
    if item is None:
        return False, 'Nothing was extracted.'
    quoted, model_evidence = quoted_fields(item)
    if not model_evidence:
        return False, 'No rule could be confirmed by a quote on the official pages.'
    if (_get(item, 'confidence') or 0) < min_confidence:
        return False, f'Confidence {_get(item, "confidence")} is below {min_confidence} (official pages not clear enough).'
    if not quoted:
        return False, 'No article types, limits, required items or scope were backed by a quote on the pages.'
    return True, ''


def own_discovered(record):
    """The record this journal's rules are kept in, unless another journal shares it (audit item 5:
    journals on one publisher-wide page were merged). A shared record is not reused."""
    if not record.discovered_id:
        return None
    if IndexedVenue.objects.filter(discovered_id=record.discovered_id).exclude(id=record.id).exists():
        return None
    return record.discovered


def _match_for(record, candidate):
    """An existing staged record for this same journal (e.g. found by Venue Discovery), never one that
    belongs to another journal in the index."""
    from . import venue_discovery as vd
    found = vd.find_existing(candidate)
    if found is None:
        return None
    if IndexedVenue.objects.filter(discovered=found).exclude(id=record.id).exists():
        return None
    if found.added_venue_id and IndexedVenue.objects.filter(venue_id=found.added_venue_id).exclude(id=record.id).exists():
        return None
    if vd.normalize_name(found.name) != vd.normalize_name(record.title):
        return None
    return found


def _item_from_pages(record, pages, config, hints, *, extractor, min_confidence, escalation, run,
                     time_left, now, errors):
    """Turn one set of fetched pages into a stored candidate, or None when nothing could be read.

    Shared by the addresses the index holds and by the pages a search finds, so a searched page is
    held to exactly the same quote validation as any other.
    """
    from . import venue_discovery as vd
    from .rules_escalation import extract_with_escalation
    fingerprint = vd.content_fingerprint(pages)
    own = own_discovered(record)
    # Reuse only this journal's own earlier reading: another journal on the same page is not this one.
    previous = own if own is not None and own.content_fingerprint == fingerprint else None
    if previous and rules_found(previous, min_confidence)[0]:
        # Unchanged pages that were read well before: reuse, no model call.
        previous.last_checked_at = now
        previous.save(update_fields=['last_checked_at', 'updated_at'])
        vd.reconfirm_live_venue(previous, now)
        return previous
    candidate, _attempts = extract_with_escalation(record, pages, config, hints, escalation=escalation,
                                                   min_confidence=min_confidence, extractor=extractor, run=run,
                                                   time_left=time_left)
    if candidate is None:
        errors.append('The AI could not extract rules from the pages.')
        return None
    # This read is about this journal, whatever name a shared page shows.
    candidate = keep_quoted_rules(dict(candidate, name=record.title[:300] or candidate['name']))
    item, _outcome = vd.upsert_candidate(candidate, fingerprint,
                                         existing=own if own is not None else _match_for(record, candidate))
    return item


def read_rules_for(record, fetcher, config, *, extractor=None, min_confidence=40, now=None, blocked_sites=None,
                   escalation=None, run=None, time_left=None):
    """Read one journal's rules. Updates the record; returns its new rules_status.

    Tries each official URL (guidelines page, then homepage). A site that refuses automated reading
    (HTTP 401/403/429, robots.txt) is added to `blocked_sites`, and is not requested again in this
    run; we respect the refusal and never try to get around it.

    Pages are fetched once; the extraction then goes local model -> local retry -> cloud model,
    stopping at the first result the page validator accepts (build plan step 6)."""
    from . import venue_discovery as vd
    from .rules_escalation import Escalation, extract_with_escalation
    now = now or timezone.now()
    blocked_sites = blocked_sites if blocked_sites is not None else set()
    escalation = escalation or Escalation.from_env()
    hints = {'name': record.title, 'organization_name': record.publisher, 'website_url': record.homepage_url,
             'venue_type': 'journal'}
    item, errors, blocked = None, [], []
    for url in entry_urls(record):
        site = site_of(url)
        if site in blocked_sites:
            blocked.append(f'{site} blocked automated reading earlier in this run')
            continue
        try:
            pages = vd.gather_pages(url, fetcher, config)
        except vd.DiscoveryFetchError as exc:
            if vd.is_blocked_failure(exc):
                blocked_sites.add(site)
                blocked.append(f'{site} blocks automated reading ({exc})')
            else:
                errors.append(f'Pages could not be read: {exc}')
            continue
        if not any(page.text for page in pages):
            errors.append('The page has no readable text (it may need JavaScript).')
            continue
        item = _item_from_pages(record, pages, config, hints, extractor=extractor, min_confidence=min_confidence,
                                escalation=escalation, run=run, time_left=time_left, now=now, errors=errors)
        break  # the pages were read; another URL on the same journal would not help the model

    # A page that was fetched is not the same as the right page. California Management Review's
    # held homepage redirected to sagepub.com/shop/subscribe-to-a-journal/..., which was read
    # successfully and of course carried no submission rules. Searching only on a failed fetch
    # would never have fired there, so the search also runs when a page was read and produced
    # nothing usable, which is the common case rather than the exception.
    if item is None or not rules_found(item, min_confidence)[0]:
        # The addresses we hold are often the wrong ones: OpenAlex gives the article-delivery or
        # marketing host, and Elsevier, SAGE, Wiley and INFORMS refuse our crawler there, while
        # the guidelines sit on a society site that answers us normally. Ask a search provider
        # where the page is; it returns the text with the result, so the fetch happens on its side.
        # Off unless a key is configured, in which case this is a no-op.
        from .guidelines_search import find_guidelines_pages
        searched = find_guidelines_pages(record)
        if searched:
            found = _item_from_pages(record, searched, config, hints, extractor=extractor,
                                     min_confidence=min_confidence, escalation=escalation, run=run,
                                     time_left=time_left, now=now, errors=errors)
            # Only replace what we have when the search actually produced something; a worse
            # reading must not overwrite a better one.
            if found is not None and (item is None or rules_found(found, min_confidence)[0]):
                item = found

    if item is not None:
        if item.origin != 'index' and not item.added_venue_id and item.discovery_status != 'ignored':
            item.origin = 'index'  # approved from Venue Index, not Venue Discovery
            item.save(update_fields=['origin', 'updated_at'])
        record.discovered = item
        ok, reason = rules_found(item, min_confidence)
        record.rules_status = 'ready' if ok else 'incomplete'
        record.rules_error = '' if ok else reason[:300]
    elif blocked and not errors:
        record.rules_status = 'blocked'
        record.rules_error = ('; '.join(blocked) + '.')[:300]
    else:
        record.rules_status = 'failed'
        record.rules_error = ('; '.join(errors + blocked) or 'No official page to read.')[:300]
    record.rules_read_at = now
    record.save(update_fields=['discovered', 'rules_status', 'rules_error', 'rules_read_at', 'updated_at'])
    return record.rules_status


def _only_blocked_sites(record, blocked_sites):
    urls = entry_urls(record)
    return bool(urls) and all(site_of(u) in blocked_sites for u in urls)


def run_rules(run, profile, budget, *, say=lambda m: None, fetcher=None, extractor=None, limit=None):
    """Read rules for up to `limit` journals (the per-run ceiling) within the time budget."""
    from .venue_discovery import DiscoveryConfig, SafeFetcher
    key, label, _subfields = rules_field()
    limit = limit or _env_int('VENUE_INDEX_RULES_PER_RUN', 40, 1, 1000)
    min_confidence = _env_int('VENUE_INDEX_RULES_MIN_CONFIDENCE', 40, 0, 100)
    # Journals skipped because their site already blocked us in this run do not use up the ceiling,
    # so look further down the list than `limit`.
    due = list(reading_order(rules_candidates(profile)).values_list('id', flat=True)[:limit * 4])
    total_due = rules_candidates(profile).count()
    if not due:
        say(f'No journals in {label} are waiting for their rules to be read.')
        return
    config = replace(DiscoveryConfig.from_env(),
                     ai_provider=os.getenv('VENUE_INDEX_RULES_AI_PROVIDER', 'ollama').strip().lower() or 'ollama',
                     max_pages_per_run=len(due) * 6 + 10)
    from .rules_escalation import LOCAL_PROVIDERS
    if extractor is None and config.ai_provider not in LOCAL_PROVIDERS:
        # Rules are read on our own hardware only. A typo or 'auto' must never fall through to Anthropic.
        from .venue_index import _record_error
        exc = RulesModelUnavailable(
            f'VENUE_INDEX_RULES_AI_PROVIDER={config.ai_provider!r} is not a local model; '
            f'use one of {", ".join(sorted(LOCAL_PROVIDERS))}.')
        _record_error(run, 'ai', exc)
        run.save()
        say(f'Stopped: {exc}')
        return
    if config.ai_provider == 'shared_qwen':
        # The shared Qwen worker has a small context (about 2,000 prompt tokens): fit the page text to it.
        config = replace(config, max_ai_chars=min(
            config.max_ai_chars, _env_int('VENUE_INDEX_SHARED_QWEN_MAX_AI_CHARS', 5000, 1000, 30000)))
    fetcher = fetcher or SafeFetcher(config)
    from .rules_escalation import Escalation
    escalation = Escalation.from_env()
    provider = {'ollama': 'local Ollama', 'shared_qwen': 'shared Qwen worker'}.get(config.ai_provider, config.ai_provider)
    say(f'Reading the rules of up to {min(limit, total_due)} of {total_due} {label} journals from their official '
        f'pages ({provider}, {escalation.describe()}; every rule must be quoted on the page)…')
    blocked_sites = set()
    read = skipped = 0
    for record_id in due:
        if read >= limit:
            break
        if not budget():
            say('Time limit reached; the remaining journals are read in the next run.')
            break
        record = IndexedVenue.objects.filter(id=record_id).first()
        if record is None:
            continue
        if _only_blocked_sites(record, blocked_sites):
            # Same publisher site as one that refused us a moment ago: record it, no request made.
            read_rules_for(record, fetcher, config, extractor=extractor, blocked_sites=blocked_sites,
                           escalation=escalation, run=run)
            skipped += 1
            continue
        read += 1
        run.rules_attempted += 1
        retried, escalated = escalation.retried, escalation.escalated
        try:
            status = read_rules_for(record, fetcher, config, extractor=extractor, min_confidence=min_confidence,
                                    blocked_sites=blocked_sites, escalation=escalation, run=run,
                                    time_left=getattr(budget, 'remaining', None))
        except RulesTimeUp:
            # Audit item 35: not enough time left for this journal. It is not marked failed; the next run
            # reads it first.
            run.rules_attempted -= 1
            say('Time limit reached; the remaining journals are read in the next run.')
            break
        except RulesModelUnavailable as exc:
            from .venue_index import _record_error
            _record_error(run, 'ai', exc)
            run.rules_attempted -= 1
            say(f'Stopped: {exc}')
            break
        except Exception as exc:  # one journal must not stop the run
            logger.exception('Reading rules failed for %s', record.title)
            IndexedVenue.objects.filter(id=record.id).update(rules_status='failed', rules_error=str(exc)[:300],
                                                             rules_read_at=timezone.now())
            status = 'failed'
        if status == 'ready':
            run.rules_ready += 1
        else:
            run.rules_failed += 1
        run.rules_retried += escalation.retried - retried
        run.rules_escalated += escalation.escalated - escalated
        run.save()
        how = (' (after escalation to Anthropic)' if escalation.escalated > escalated
               else ' (after a local retry)' if escalation.retried > retried else '')
        say(f'[{read}/{min(limit, total_due)}] {record.title}: {STATUS_TEXT.get(status, status)}{how}')
    for note in escalation.notes:
        say(note)
    if escalation.cloud_blocked:
        say(f'Escalation to the cloud stopped: {escalation.cloud_blocked}')
    if skipped:
        say(f'Skipped {skipped} journals on sites that block automated reading '
            f'({", ".join(sorted(blocked_sites))}); they are tried again after {BLOCKED_RETRY_DAYS} days.')


def rules_summary(item):
    """What the AI read, for the admin's approval screen (only page-verified evidence is shown)."""
    if item is None:
        return None
    rules = []
    for rule in item.structured_desk_rejection_rules or []:
        if isinstance(rule, dict) and rule.get('message'):
            rules.append(rule['message'])
    return {
        'discovered_id': str(item.id),
        'confidence': item.confidence,
        'acceptance_status': item.acceptance_status,
        'aims_scope': item.aims_scope,
        'article_types': item.article_types,
        'submission_types': item.submission_types,
        'limits': rules,
        'required_items': [r.get('label') or r.get('name') or str(r) for r in (item.required_submission_items or [])
                           if r][:12],
        'policies': item.policies,
        'submission_url': item.submission_url,
        'evidence': [{'field': e.get('field'), 'claim': e.get('claim'), 'url': e.get('url'),
                      'quote': e.get('excerpt') or e.get('evidence_text') or ''}
                     for e in (item.source_evidence or [])][:12],
        'source_urls': item.source_urls,
        'checked_at': item.last_checked_at.isoformat() if item.last_checked_at else None,
        'published_venue_id': str(item.added_venue_id) if item.added_venue_id else None,
    }
