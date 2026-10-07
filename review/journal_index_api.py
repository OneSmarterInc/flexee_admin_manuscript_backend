"""The author-facing journal index (build plan step 8).

Search across every venue an author may see, plus the journals Flexee has only listed so far, with
each one's trust tier and check date shown. Read-only and public: it holds no personal data, and
excluded journals (and journals still waiting for an exclusion decision) never appear.

    GET /api/journals/?q=&tier=&open_access=1&page=   search
    GET /api/journals/v/<slug>/                        a live venue's page
    GET /api/journals/i/<uuid>/                        a listed journal's page (catalogue facts only)
"""
from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from .models import IndexedVenue, Venue

PAGE_SIZE = 20
TIER_ORDER = {Venue.TIER_CLAIMED: 0, Venue.TIER_VERIFIED_INDEX: 1, Venue.TIER_LISTED: 2}
LISTED_TRUST = {'tier': 'listed', 'label': 'Listed only', 'last_verified_at': None, 'source_urls': []}


def _iso(value):
    return value.isoformat() if value else None


def listed_records():
    """Journals in the index that are not live: shown as 'Listed only'. Excluded journals and
    journals waiting for an exclusion decision are never shown."""
    return IndexedVenue.objects.filter(excluded=False, venue__isnull=True, missing_since__isnull=True,
                                       screening_status__in=['clear', 'kept'])


def _subjects(record, limit=3):
    return [s.get('name') for s in (record.subfields or [])[:limit] if isinstance(s, dict) and s.get('name')]


def catalogue_facts(record):
    """What the open catalogues say about a journal (OpenAlex, Crossref, DOAJ, ISSN)."""
    if record is None:
        return None
    return {
        'issn': record.issn_l or (record.issns or [''])[0],
        'issns': list(record.issns or [])[:4],
        'publisher': record.publisher,
        'country': record.country_code,
        'homepage_url': record.homepage_url,
        'open_access': record.open_access,
        'doaj_listed': record.doaj_listed,
        'apc_usd': record.apc_usd,
        'subjects': _subjects(record, 5),
        'first_year': record.first_publication_year,
        'works_count': (record.metrics or {}).get('works_count'),
        'catalogue_checked_at': _iso(record.last_refreshed_at),
    }


def _live_item(venue, record):
    from .author_api import _active_config, _venue_trust_payload
    from .services.freshness import visible_calls
    config = _active_config(venue)
    calls, _hidden = visible_calls(venue, config)
    return {
        'kind': 'venue', 'key': venue.slug, 'name': venue.name, 'venue_type': venue.venue_type,
        'publisher': (venue.organization.name if venue.organization_id else '') or (record.publisher if record else ''),
        'trust': _venue_trust_payload(venue),
        'summary': ((config.aims_scope if config else '') or venue.description or '')[:280],
        'subjects': _subjects(record) if record else [],
        'open_access': record.open_access if record else None,
        'open_calls': len(calls),
        'matchable': venue.trust_tier in Venue.RULE_TIERS,
    }


def _listed_item(record):
    return {
        'kind': 'indexed', 'key': str(record.id), 'name': record.title, 'venue_type': 'journal',
        'publisher': record.publisher, 'trust': LISTED_TRUST, 'summary': '',
        'subjects': _subjects(record), 'open_access': record.open_access, 'open_calls': 0, 'matchable': False,
        'issn': record.issn_l,
    }


@require_GET
def journal_search(request):
    q = (request.GET.get('q') or '').strip()[:200]
    tier = request.GET.get('tier', '')
    open_access = request.GET.get('open_access') in {'1', 'true'}
    try:
        page = max(1, int(request.GET.get('page', 1)))
    except (TypeError, ValueError):
        page = 1

    live = Venue.objects.author_visible().select_related('organization')
    listed = listed_records()
    if q:
        live = live.filter(Q(name__icontains=q) | Q(organization__name__icontains=q) | Q(description__icontains=q)
                           | Q(agent_configs__active=True, agent_configs__aims_scope__icontains=q)).distinct()
        listed = listed.filter(Q(title__icontains=q) | Q(publisher__icontains=q) | Q(issn_l__icontains=q)
                               | Q(primary_subfield__icontains=q))
    if tier in TIER_ORDER:
        live = live.filter(trust_tier=tier)
        if tier != Venue.TIER_LISTED:
            listed = listed.none()
    if open_access:
        live = live.filter(index_records__open_access=True)
        listed = listed.filter(open_access=True)

    # Live venues first (editor-confirmed, then checked from official pages), then listed journals.
    live_list = sorted(live, key=lambda v: (TIER_ORDER.get(v.trust_tier, 3), v.name.lower()))
    listed = listed.order_by('-metrics__works_count', 'title')
    total = len(live_list) + listed.count()
    start, end = (page - 1) * PAGE_SIZE, page * PAGE_SIZE
    records = {r.venue_id: r for r in IndexedVenue.objects.filter(venue__in=[v.id for v in live_list[start:end]])}
    items = [_live_item(v, records.get(v.id)) for v in live_list[start:end]]
    if end > len(live_list):
        from_listed = max(0, start - len(live_list))
        items += [_listed_item(r) for r in listed[from_listed:from_listed + (PAGE_SIZE - len(items))]]
    return JsonResponse({
        'items': items,
        'pagination': {'page': page, 'page_size': PAGE_SIZE, 'total': total,
                       'pages': max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)},
        'counts': {'claimed': Venue.objects.author_visible().filter(trust_tier=Venue.TIER_CLAIMED).count(),
                   'verified_index': Venue.objects.author_visible().filter(trust_tier=Venue.TIER_VERIFIED_INDEX).count(),
                   'listed': listed_records().count()},
    })


def _rule_messages(config):
    out = []
    for rule in (config.structured_desk_rejection_rules or []) if config else []:
        if isinstance(rule, dict) and rule.get('message'):
            out.append(rule['message'])
    return out[:12]


def _required_items(config):
    items = []
    for item in (config.required_submission_items or []) if config else []:
        if isinstance(item, dict):
            label = item.get('label') or item.get('name')
            if label:
                items.append(str(label))
        elif item:
            items.append(str(item))
    return items[:15]


@require_GET
def journal_venue_page(request, slug):
    from .author_api import _active_config, _venue_payload
    venue = Venue.objects.author_visible().select_related('organization').filter(slug=slug).first()
    if venue is None:
        return JsonResponse({'detail': 'Journal not found'}, status=404)
    payload = _venue_payload(venue)  # the author view: stale or expired calls already removed
    config = _active_config(venue)
    view = payload.get('config') or {}
    record = IndexedVenue.objects.filter(venue=venue).first()
    return JsonResponse({'journal': {
        'kind': 'venue', 'key': venue.slug, 'id': str(venue.id), 'name': venue.name, 'venue_type': venue.venue_type,
        'description': venue.description,
        'publisher': payload['organization']['name'] if payload.get('organization') else (record.publisher if record else ''),
        'trust': payload['trust'],
        'matchable': venue.trust_tier in Venue.RULE_TIERS,
        'rules': {
            'aims_scope': view.get('aims_scope', ''),
            'article_types': view.get('article_types') or [],
            'limits': _rule_messages(config),
            'required_items': _required_items(config),
            'accepted_methods': view.get('accepted_methods') or [],
            'reporting_standards': view.get('reporting_standards') or [],
            'deadlines': view.get('deadlines') or {},
        } if config else None,
        'open_calls': view.get('open_calls') or [],
        'catalogue': catalogue_facts(record),
    }})


@require_GET
def journal_listed_page(request, record_id):
    record = IndexedVenue.objects.filter(id=record_id, excluded=False, missing_since__isnull=True,
                                         screening_status__in=['clear', 'kept']).select_related('venue').first()
    if record is None:
        return JsonResponse({'detail': 'Journal not found'}, status=404)
    if record.venue_id and record.venue.active and not record.venue.excluded:
        # It went live since: send the author to the live page.
        return JsonResponse({'redirect': f'/journals/v/{record.venue.slug}', 'detail': 'This journal is live.'},
                            status=200)
    return JsonResponse({'journal': {
        'kind': 'indexed', 'key': str(record.id), 'name': record.title, 'venue_type': 'journal',
        'publisher': record.publisher, 'trust': LISTED_TRUST, 'matchable': False,
        'rules': None, 'open_calls': [], 'catalogue': catalogue_facts(record),
    }})
