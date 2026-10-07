"""Freshness: re-check on a cadence, and never show a stale call (build plan step 7, section 5).

    Spine metadata                     monthly    keep, flag         (the monthly index refresh)
    Scope, article types, limits       quarterly  keep, show the age (rules re-read every 90 days)
    Open calls, deadlines              weekly     hide, never stale  (this module; no AI)

Rule: an open call that has not been re-confirmed inside its window is hidden, not shown with an
old date. One expired call shown as live costs more trust than ten missing journals.

Calls on Flexee-verified venues are re-read from the official pages by the same phrase-and-date
detector Venue Discovery uses; no language model is involved. Calls on editor-configured (claimed)
venues are the editor's to maintain: they are hidden once their deadline passes.
"""
import logging
import os
from datetime import date, timedelta

from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

CALL_PREFIX = 'Call:'


def _env_int(name, default, low, high):
    try:
        return max(low, min(int(os.getenv(name, str(default))), high))
    except ValueError:
        return default


def confirm_days():
    """How long a re-confirmed call stays visible: the weekly check plus a few days' grace."""
    return _env_int('VENUE_CALLS_CONFIRM_DAYS', 10, 1, 60)


def recheck_days():
    return _env_int('VENUE_CALLS_RECHECK_DAYS', 6, 1, 30)


def rules_refresh_days():
    return _env_int('VENUE_INDEX_RULES_REFRESH_DAYS', 90, 7, 730)


def _parse_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _parse_dt(value):
    from django.utils.dateparse import parse_datetime
    if not value:
        return None
    if hasattr(value, 'isoformat'):
        return value
    parsed = parse_datetime(str(value))
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


# ---------------------------------------------------------------------------
# What authors may see
# ---------------------------------------------------------------------------

def _config_calls(config):
    demand = (config.current_demand or {}) if config is not None else {}
    calls = demand.get('calls_for_papers') if isinstance(demand, dict) else None
    return [c for c in (calls or []) if isinstance(c, dict)]


def call_candidates(venue, config):
    """All calls known for a venue, each with when it was last confirmed (may be None)."""
    from ..models import Venue
    if venue.trust_tier == Venue.TIER_VERIFIED_INDEX and venue.calls_checked_at:
        return [dict(c) for c in (venue.open_calls or []) if isinstance(c, dict)]
    # Not checked by the weekly job yet: the calls read when the venue was added count as confirmed then.
    return [dict(c, confirmed_at=c.get('confirmed_at') or (venue.last_verified_at.isoformat()
                                                            if venue.last_verified_at else None))
            for c in _config_calls(config)]


def visible_calls(venue, config, now=None):
    """Calls an author may see: deadline not passed and, on Flexee-verified venues, re-confirmed
    within the window. Returns (visible, hidden_count)."""
    from ..models import Venue
    now = now or timezone.now()
    today = timezone.localdate(now)
    window = now - timedelta(days=confirm_days())
    visible, hidden = [], 0
    for call in call_candidates(venue, config):
        deadline = _parse_date(call.get('deadline'))
        if deadline is None or deadline < today:
            hidden += 1
            continue
        if venue.trust_tier == Venue.TIER_VERIFIED_INDEX:
            confirmed = _parse_dt(call.get('confirmed_at'))
            if confirmed is None or confirmed < window:
                hidden += 1
                continue
        visible.append({'title': str(call.get('title') or '')[:200], 'deadline': deadline.isoformat(),
                        'url': call.get('url') or '', 'confirmed_at': call.get('confirmed_at')})
    visible.sort(key=lambda c: c['deadline'])
    return visible, hidden


def author_config_view(venue, config, payload, now=None):
    """The author-facing copy of a config payload: stale or expired calls removed everywhere they
    appear (current_demand, 'Call: ...' deadlines, dated deadlines) and the live ones listed once."""
    if payload is None:
        return None
    now = now or timezone.now()
    today = timezone.localdate(now)
    payload = dict(payload)
    demand = dict(payload.get('current_demand') or {})
    demand.pop('calls_for_papers', None)
    payload['current_demand'] = demand
    deadlines = {}
    for key, value in (payload.get('deadlines') or {}).items():
        if str(key).startswith(CALL_PREFIX):
            continue  # calls are listed (and checked) in open_calls
        parsed = _parse_date(value)
        if parsed is not None and parsed < today:
            continue  # a dated deadline that has passed is not shown
        deadlines[key] = value
    payload['deadlines'] = deadlines
    payload['open_calls'], payload['calls_hidden'] = visible_calls(venue, config, now)
    return payload


# ---------------------------------------------------------------------------
# The weekly re-check (no AI)
# ---------------------------------------------------------------------------

def calls_due(now=None):
    from ..models import Venue
    now = now or timezone.now()
    cutoff = now - timedelta(days=recheck_days())
    return (Venue.objects.filter(trust_tier=Venue.TIER_VERIFIED_INDEX, active=True, excluded=False)
            .filter(Q(calls_attempted_at__isnull=True) | Q(calls_attempted_at__lt=cutoff))
            .order_by('calls_attempted_at', 'name'))


def _entry_urls(venue):
    from ..models import DiscoveredVenue
    urls = []
    discovered = DiscoveredVenue.objects.filter(added_venue=venue).first()
    if discovered:
        urls += [discovered.submission_url, discovered.website_url]
    urls += list(venue.source_urls or [])[:3]
    return [u for u in dict.fromkeys(u for u in urls if u)]


def check_calls(venue, fetcher, config, now=None):
    """Re-read a venue's official pages for open calls. Returns the number of open calls, or raises
    the fetch error (the venue keeps its old calls, which then age out of the window)."""
    from . import venue_discovery as vd
    now = now or timezone.now()
    venue.calls_attempted_at = now
    urls = _entry_urls(venue)
    if not urls:
        venue.calls_error = 'No official page is known for this venue.'
        venue.save(update_fields=['calls_attempted_at', 'calls_error', 'updated_at'])
        return 0
    pages, error = [], ''
    for url in urls:
        try:
            pages = vd.gather_pages(url, fetcher, config)
            break
        except vd.DiscoveryFetchError as exc:
            error = str(exc)
    if not pages:
        venue.calls_error = f'Pages could not be read: {error}'[:300]
        venue.save(update_fields=['calls_attempted_at', 'calls_error', 'updated_at'])
        raise vd.DiscoveryFetchError(error or 'no page')
    # Previously known call pages are read too, so a call on its own page is re-confirmed.
    known = {vd.canonical_url(p.url) for p in pages}
    for call in (venue.open_calls or [])[:3]:
        url = call.get('url') if isinstance(call, dict) else None
        if url and vd.canonical_url(url) not in known:
            try:
                pages.append(fetcher.fetch(url))
                known.add(vd.canonical_url(url))
            except vd.DiscoveryFetchError:
                pass
    official = [p for p in pages if not vd.is_third_party(p.url)]
    stamp = now.isoformat()
    calls = [dict(c, confirmed_at=stamp) for c in vd.detect_calls_for_papers(official, today=timezone.localdate(now))]
    venue.open_calls = calls
    venue.calls_checked_at = now
    venue.calls_error = ''
    venue.save(update_fields=['open_calls', 'calls_checked_at', 'calls_attempted_at', 'calls_error', 'updated_at'])
    return len(calls)


def run_calls(run, budget, *, say=lambda m: None, fetcher=None, limit=None, now=None):
    """Re-confirm open calls for every Flexee-verified venue that is due (weekly)."""
    from dataclasses import replace
    from .venue_discovery import DiscoveryConfig, DiscoveryFetchError, SafeFetcher
    now = now or timezone.now()
    limit = limit or _env_int('VENUE_CALLS_PER_RUN', 200, 1, 5000)
    due = list(calls_due(now)[:limit])
    if not due:
        say('No venues are due for an open-call check.')
        return
    config = replace(DiscoveryConfig.from_env(), max_pages_per_run=len(due) * 8 + 10)
    fetcher = fetcher or SafeFetcher(config)
    say(f'Re-confirming open calls for {len(due)} venues from their official pages (no AI)…')
    for index, venue in enumerate(due, 1):
        if not budget():
            say('Time limit reached; the remaining venues are checked in the next run.')
            break
        try:
            found = check_calls(venue, fetcher, config, now=now)
            run.calls_checked += 1
            run.calls_open += found
            say(f'[{index}/{len(due)}] {venue.name}: ' + (f'{found} open call{"s" if found != 1 else ""}'
                                                          if found else 'no open calls'))
        except DiscoveryFetchError as exc:
            run.calls_failed += 1
            say(f'[{index}/{len(due)}] {venue.name}: pages could not be read ({exc}); its calls are hidden '
                f'after {confirm_days()} days without confirmation')
        except Exception as exc:  # one venue must not stop the run
            logger.exception('Open-call check failed for %s', venue.name)
            run.calls_failed += 1
            say(f'[{index}/{len(due)}] {venue.name}: check failed ({exc})')
        run.save()


# ---------------------------------------------------------------------------
# For the admin: how fresh is what authors see?
# ---------------------------------------------------------------------------

def freshness_stats(now=None):
    from ..models import IndexedVenue, Venue
    now = now or timezone.now()
    live = list(Venue.objects.filter(trust_tier=Venue.TIER_VERIFIED_INDEX, active=True, excluded=False))
    ages = sorted((now - v.last_verified_at).days for v in live if v.last_verified_at)
    median = ages[len(ages) // 2] if ages else None
    shown = hidden = 0
    for venue in live:
        config = venue.agent_configs.filter(active=True).order_by('-version').first()
        visible, stale = visible_calls(venue, config, now)
        shown += len(visible)
        hidden += stale
    refresh_cutoff = now - timedelta(days=rules_refresh_days())
    return {
        'live_verified': len(live),
        'median_age_days': median,
        'oldest_age_days': ages[-1] if ages else None,
        'rules_refresh_days': rules_refresh_days(),
        'rules_refresh_due': IndexedVenue.objects.filter(venue__isnull=False, excluded=False,
                                                         rules_read_at__lt=refresh_cutoff).count(),
        'calls_shown': shown,
        'calls_hidden': hidden,
        'calls_due': calls_due(now).count(),
        'calls_confirm_days': confirm_days(),
        'calls_failing': sum(1 for v in live if v.calls_error),
    }
