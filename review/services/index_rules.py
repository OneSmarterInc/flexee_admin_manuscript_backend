"""Venue index, layer 2: read each journal's rules from its own pages (build plan step 4).

For journals in the first target field, the existing Venue Discovery pipeline reads the official
author-guidelines page (from DOAJ) or the homepage, asks the AI (local Ollama by default) to extract
the rules a VenueAgentConfig needs, and keeps only rules whose quotes appear on the fetched pages.

Reading never publishes anything. A journal whose rules were read is "ready"; an admin approves it
("Publish"), which creates the live venue labelled "Checked from official pages" (verified_index).
"""
import logging
import os
from dataclasses import replace
from datetime import timedelta
from urllib.parse import urlsplit

from django.db.models import Case, F, IntegerField, Q, Value, When
from django.utils import timezone

from ..models import IndexedVenue

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


def reading_order(queryset):
    """Never-read journals first; within those, journals with a DOAJ author-guidelines page (usually
    readable) before the rest, then the most-published."""
    has_guidelines = Q(doaj__guidelines_url__isnull=False) & ~Q(doaj__guidelines_url='')
    return (queryset.annotate(_guidelines_first=Case(When(has_guidelines, then=Value(0)), default=Value(1),
                                                     output_field=IntegerField()))
            .order_by(F('rules_read_at').asc(nulls_first=True), '_guidelines_first', '-metrics__works_count', 'title'))


def entry_url(record):
    return (record.doaj or {}).get('guidelines_url') or record.homepage_url


def entry_urls(record):
    """The official URLs to try, in order: DOAJ's author-guidelines page, then the homepage."""
    urls = []
    for url in ((record.doaj or {}).get('guidelines_url'), record.homepage_url):
        if url and url not in urls:
            urls.append(url)
    return urls


def site_of(url):
    host = (urlsplit(url or '').hostname or '').lower()
    return host[4:] if host.startswith('www.') else host


def rules_found(item, min_confidence):
    """Did the read find real rules? Official pages, at least one quote verified on them, and at
    least one rule a manuscript can be checked against."""
    if item is None:
        return False, 'Nothing was extracted.'
    get = item.get if isinstance(item, dict) else (lambda name: getattr(item, name, None))
    if not get('source_evidence'):
        return False, 'No rule could be confirmed by a quote on the official pages.'
    if (get('confidence') or 0) < min_confidence:
        return False, f'Confidence {get("confidence")} is below {min_confidence} (official pages not clear enough).'
    if not any(get(f) for f in RULE_FIELDS):
        return False, 'The pages did not state article types, limits, required items or scope.'
    return True, ''


def read_rules_for(record, fetcher, config, *, extractor=None, min_confidence=40, now=None, blocked_sites=None,
                   escalation=None, run=None):
    """Read one journal's rules. Updates the record; returns its new rules_status.

    Tries each official URL (guidelines page, then homepage). A site that refuses automated reading
    (HTTP 401/403/429, robots.txt) is added to `blocked_sites`, and is not requested again in this
    run; we respect the refusal and never try to get around it.

    Pages are fetched once; the extraction then goes local model -> local retry -> cloud model,
    stopping at the first result the page validator accepts (build plan step 6)."""
    from . import venue_discovery as vd
    from ..models import DiscoveredVenue
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
        fingerprint = vd.content_fingerprint(pages)
        previous = DiscoveredVenue.objects.filter(content_fingerprint=fingerprint).first()
        if previous and rules_found(previous, min_confidence)[0]:
            # Unchanged pages that were read well before: reuse, no model call.
            previous.last_checked_at = now
            previous.save(update_fields=['last_checked_at', 'updated_at'])
            vd.reconfirm_live_venue(previous, now)
            item = previous
            break
        candidate, _attempts = extract_with_escalation(record, pages, config, hints, escalation=escalation,
                                                       min_confidence=min_confidence, extractor=extractor, run=run)
        if candidate is None:
            errors.append('The AI could not extract rules from the pages.')
            break  # the pages were read; another URL on the same journal would not help the model
        item, _outcome = vd.upsert_candidate(candidate, fingerprint)
        break

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
                                    blocked_sites=blocked_sites, escalation=escalation, run=run)
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
