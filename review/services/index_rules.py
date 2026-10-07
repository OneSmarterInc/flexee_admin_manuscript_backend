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

from django.db.models import Q
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
    exclusion decision, not already live, never read (or a failed read older than 30 days)."""
    now = now or timezone.now()
    _key, _label, subfields = rules_field()
    retry = now - timedelta(days=RETRY_AFTER_DAYS)
    return (IndexedVenue.objects.filter(field_profile=profile, excluded=False, venue__isnull=True,
                                        missing_since__isnull=True,
                                        screening_status__in=['clear', 'kept'], subfields__0__id__in=list(subfields))
            .exclude(Q(homepage_url='') & (Q(doaj__guidelines_url__isnull=True) | Q(doaj__guidelines_url='')))
            .filter(Q(rules_status='not_read') | Q(rules_status__in=['failed', 'incomplete'], rules_read_at__lt=retry)))


def entry_url(record):
    return (record.doaj or {}).get('guidelines_url') or record.homepage_url


def rules_found(item, min_confidence):
    """Did the read find real rules? Official pages, at least one quote verified on them, and at
    least one rule a manuscript can be checked against."""
    if item is None:
        return False, 'Nothing was extracted.'
    if not item.source_evidence:
        return False, 'No rule could be confirmed by a quote on the official pages.'
    if item.confidence < min_confidence:
        return False, f'Confidence {item.confidence} is below {min_confidence} (official pages not clear enough).'
    if not any(getattr(item, f) for f in RULE_FIELDS if f != 'aims_scope') and not item.aims_scope:
        return False, 'The pages did not state article types, limits, required items or scope.'
    return True, ''


def read_rules_for(record, fetcher, config, *, extractor=None, min_confidence=40, now=None):
    """Read one journal's rules. Updates the record; returns its new rules_status."""
    from . import venue_discovery as vd
    now = now or timezone.now()
    url = entry_url(record)
    hints = {'name': record.title, 'organization_name': record.publisher, 'website_url': record.homepage_url,
             'venue_type': 'journal'}
    item, error = None, ''
    try:
        item, _outcome = vd.process_candidate(url, fetcher, config, extractor=extractor, hints=hints)
    except vd.DiscoveryModelUnavailable as exc:
        raise RulesModelUnavailable(str(exc)) from exc
    except vd.DiscoveryFetchError as exc:
        error = f'Pages could not be read: {exc}'
    except vd.DiscoveryExtractionError as exc:
        error = f'The AI could not extract rules: {exc}'

    if item is not None:
        if item.origin != 'index' and not item.added_venue_id and item.discovery_status != 'ignored':
            item.origin = 'index'  # approved from Venue Index, not Venue Discovery
            item.save(update_fields=['origin', 'updated_at'])
        record.discovered = item
        ok, reason = rules_found(item, min_confidence)
        record.rules_status = 'ready' if ok else 'incomplete'
        record.rules_error = '' if ok else reason[:300]
    else:
        record.rules_status = 'failed'
        record.rules_error = error[:300]
    record.rules_read_at = now
    record.save(update_fields=['discovered', 'rules_status', 'rules_error', 'rules_read_at', 'updated_at'])
    return record.rules_status


def run_rules(run, profile, budget, *, say=lambda m: None, fetcher=None, extractor=None, limit=None):
    """Read rules for up to `limit` journals (the per-run ceiling) within the time budget."""
    from .venue_discovery import DiscoveryConfig, SafeFetcher
    key, label, _subfields = rules_field()
    limit = limit or _env_int('VENUE_INDEX_RULES_PER_RUN', 40, 1, 1000)
    min_confidence = _env_int('VENUE_INDEX_RULES_MIN_CONFIDENCE', 40, 0, 100)
    due = list(rules_candidates(profile).order_by('rules_read_at', '-metrics__works_count', 'title')
               .values_list('id', flat=True)[:limit])
    total_due = rules_candidates(profile).count()
    if not due:
        say(f'No journals in {label} are waiting for their rules to be read.')
        return
    config = replace(DiscoveryConfig.from_env(),
                     ai_provider=os.getenv('VENUE_INDEX_RULES_AI_PROVIDER', 'ollama').strip().lower() or 'ollama',
                     max_pages_per_run=len(due) * 6 + 10)
    fetcher = fetcher or SafeFetcher(config)
    provider = 'local Ollama' if config.ai_provider == 'ollama' else config.ai_provider
    say(f'Reading the rules of {len(due)} of {total_due} {label} journals from their official pages ({provider}; '
        f'every rule must be quoted on the page)…')
    for index, record_id in enumerate(due, 1):
        if not budget():
            say('Time limit reached; the remaining journals are read in the next run.')
            break
        record = IndexedVenue.objects.filter(id=record_id).first()
        if record is None:
            continue
        run.rules_attempted += 1
        try:
            status = read_rules_for(record, fetcher, config, extractor=extractor, min_confidence=min_confidence)
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
        run.save()
        say(f'[{index}/{len(due)}] {record.title}: '
            + {'ready': 'rules ready for approval', 'incomplete': 'rules not found on the pages',
               'failed': 'pages could not be read'}[status])


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
