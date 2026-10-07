"""Admin API for the venue index spine (platform superusers only). Build plan step 2."""
import json

from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .audit import record_audit_event
from .auth import require_platform_superuser
from .index_schedule import get_index_schedule, set_index_schedule
from .models import IndexedVenue, VenueIndexRun
from .services.venue_index import PROFILES, IndexConfig, coverage_counts, start_index_run

FILTERS = {
    'all': Q(),
    'open_access': Q(open_access=True),
    'doaj': Q(doaj_listed=True),
    'crossref': Q(crossref__registered=True),
    'no_crossref': Q(crossref__registered=False),
    'linked': Q(venue__isnull=False),
    'not_checked': Q(enriched_at__isnull=True),
    'missing': Q(missing_since__isnull=False),
}


def _iso(value):
    return value.isoformat() if value else None


def record_payload(item, detail=False):
    venue = item.venue
    payload = {
        'id': str(item.id),
        'title': item.title,
        'issn_l': item.issn_l,
        'issns': item.issns,
        'publisher': item.publisher,
        'country_code': item.country_code,
        'homepage_url': item.homepage_url,
        'open_access': item.open_access,
        'doaj_listed': item.doaj_listed,
        'primary_subfield': item.primary_subfield,
        'scope_share': item.scope_share,
        'first_publication_year': item.first_publication_year,
        'crossref_registered': (item.crossref or {}).get('registered'),
        'crossref_first_year': (item.crossref or {}).get('first_year'),
        'works_count': (item.metrics or {}).get('works_count'),
        'last_refreshed_at': _iso(item.last_refreshed_at),
        'enriched_at': _iso(item.enriched_at),
        'missing_since': _iso(item.missing_since),
        'trust': {
            'tier': item.trust_tier,
            'last_verified_at': _iso(venue.last_verified_at) if venue else None,
        },
        'venue': {'id': str(venue.id), 'name': venue.name} if venue else None,
    }
    if detail:
        payload.update({
            'openalex_id': item.openalex_id,
            'openalex_url': f'https://openalex.org/{item.openalex_id}' if item.openalex_id else '',
            'source_ids': item.source_ids,
            'subfields': item.subfields,
            'metrics': item.metrics,
            'last_publication_year': item.last_publication_year,
            'apc_usd': item.apc_usd,
            'crossref': item.crossref,
            'doaj': item.doaj,
            'issn_checks': item.issn_checks,
            'checked': item.checked,
            'first_imported_at': _iso(item.first_imported_at),
            'last_error': item.last_error,
        })
    return payload


def run_payload(run):
    if not run:
        return None
    return {
        'id': str(run.id),
        'status': run.status,
        'mode': run.mode,
        'trigger': run.trigger,
        'requested_by': run.requested_by,
        'created_at': _iso(run.created_at),
        'started_at': _iso(run.started_at),
        'completed_at': _iso(run.completed_at),
        'catalogue_method': run.catalogue_method,
        'catalogue_complete': run.catalogue_complete,
        'pages_fetched': run.pages_fetched,
        'records_seen': run.records_seen,
        'out_of_scope': run.out_of_scope,
        'created': run.created_count,
        'updated': run.updated_count,
        'linked': run.linked_count,
        'enriched': run.enriched_count,
        'flagged_missing': run.flagged_missing,
        'pending_after': run.pending_after,
        'errors': (run.errors or [])[-10:],
        'summary': run.summary,
    }


@require_GET
@require_platform_superuser
def index_list(request):
    config = IndexConfig()
    base = IndexedVenue.objects.filter(field_profile=config.profile).select_related('venue')
    query = request.GET.get('q', '').strip()
    if query:
        base = base.filter(Q(title__icontains=query) | Q(publisher__icontains=query) |
                           Q(issn_l__icontains=query) | Q(primary_subfield__icontains=query))
    key = request.GET.get('filter', 'all')
    items = base.filter(FILTERS.get(key, Q()))
    subfield = request.GET.get('subfield', '').strip()
    if subfield:
        items = items.filter(primary_subfield=subfield)
    order = {'title': 'title', 'works': '-metrics__works_count', 'recent': '-first_imported_at'}.get(
        request.GET.get('sort', 'title'), 'title')
    items = items.order_by(order, 'title')

    total = items.count()
    try:
        page_size = max(1, min(int(request.GET.get('page_size', 20)), 100))
    except (TypeError, ValueError):
        page_size = 20
    pages = max(1, -(-total // page_size))
    try:
        page = max(1, min(int(request.GET.get('page', 1)), pages))
    except (TypeError, ValueError):
        page = 1

    return JsonResponse({
        'profile': {'key': config.profile, 'label': PROFILES.get(config.profile, {}).get('label', config.profile),
                    'subfields': config.subfields},
        'counts': coverage_counts(config.profile),
        'filter_counts': {name: base.filter(q).count() for name, q in FILTERS.items()},
        'items': [record_payload(item) for item in items[(page - 1) * page_size: page * page_size]],
        'pagination': {'page': page, 'page_size': page_size, 'total': total, 'pages': pages},
        'last_run': run_payload(VenueIndexRun.objects.order_by('-created_at').first()),
        'schedule': get_index_schedule(),
    })


@require_GET
@require_platform_superuser
def index_detail(request, record_id):
    item = IndexedVenue.objects.select_related('venue').filter(id=record_id).first()
    if not item:
        return JsonResponse({'detail': 'Index record not found'}, status=404)
    return JsonResponse({'item': record_payload(item, detail=True)})


@require_POST
@require_platform_superuser
def index_run_now(request):
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        data = {}
    mode = 'enrich' if data.get('mode') == 'enrich' else 'full'
    run, created = start_index_run(mode=mode, trigger='manual', requested_by=request.editor_user.email)
    if created:
        from django_q.tasks import async_task
        async_task('review.tasks.run_venue_index_task', str(run.id))
        record_audit_event(request, 'venue_index.run_requested', resource_type='venue_index_run',
                           resource_id=run.id, detail={'mode': mode})
    return JsonResponse({'run': run_payload(run), 'already_running': not created}, status=202)


@require_http_methods(['GET', 'POST'])
@require_platform_superuser
def index_schedule(request):
    if request.method == 'GET':
        return JsonResponse({'schedule': get_index_schedule()})
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'detail': 'Invalid JSON'}, status=400)
    try:
        state = set_index_schedule(enabled=bool(data.get('enabled')), tz_name=str(data.get('timezone') or '') or None)
    except ValueError as exc:
        return JsonResponse({'detail': str(exc)}, status=400)
    record_audit_event(request, 'venue_index.schedule_changed', resource_type='venue_index_schedule',
                       resource_id='venue-index', detail={'enabled': state['enabled']})
    return JsonResponse({'schedule': state})
