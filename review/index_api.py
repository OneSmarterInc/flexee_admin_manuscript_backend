"""Admin API for the venue index spine (platform superusers only). Build plan step 2."""
import json

from django.db.models import Q
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .audit import record_audit_event
from .auth import require_platform_superuser
from .index_schedule import get_index_schedule, set_index_schedule
from .models import BlockedPublisher, IndexedVenue, VenueIndexRun
from .services.index_screening import CRITERIA, FLAG_TO_CRITERION, DecisionError, decide, screen_record, blocked_publisher_names
from .services.index_rules import rules_candidates, rules_field, rules_summary
from .services.rules_escalation import escalation_stats
from .services.freshness import freshness_stats
from .services.venue_discovery import normalize_name
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
    'review': Q(screening_status='flagged') | Q(rereview_suggested=True),
    'excluded': Q(excluded=True),
    'kept': Q(screening_status='kept'),
    'rules_ready': Q(rules_status='ready', venue__isnull=True),
    'rules_missing': Q(rules_status__in=['incomplete', 'failed', 'blocked'], venue__isnull=True),
    'rules_changed': Q(venue__isnull=False, discovered__discovery_status='changed'),
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
        'screening': {
            'status': item.screening_status,
            'points': item.screening_points,
            'concerns': [f['label'] for f in (item.screening_flags or []) if f.get('kind') == 'negative'][:4],
            'rereview_suggested': item.rereview_suggested,
        },
        'excluded': item.excluded,
        'rules': {'status': item.rules_status, 'error': item.rules_error, 'read_at': _iso(item.rules_read_at),
                  # step 7: a live journal whose pages now say something different waits for an admin
                  'changes': (item.discovered.change_summary or 'The official pages changed.')
                  if venue and item.discovered_id and item.discovered.discovery_status == 'changed' else ''},
    }
    if detail and venue:
        from .services.freshness import visible_calls
        config = venue.agent_configs.filter(active=True).order_by('-version').first()
        shown, hidden = visible_calls(venue, config)
        payload['calls'] = {'shown': shown, 'hidden': hidden, 'checked_at': _iso(venue.calls_checked_at),
                            'error': venue.calls_error}
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
            'screening_flags': item.screening_flags,
            'screened_at': _iso(item.screened_at),
            'pages_checked_at': _iso(item.pages_checked_at),
            'exclusion_reason': item.exclusion_reason,
            'suggested_criteria': sorted({FLAG_TO_CRITERION[f['code']] for f in (item.screening_flags or [])
                                          if f.get('code') in FLAG_TO_CRITERION}),
            'suggested_evidence': [u for u in dict.fromkeys(
                [f.get('evidence_url') for f in (item.screening_flags or [])
                 if f.get('kind') == 'negative' and f.get('evidence_url')] + [item.homepage_url]) if u][:5],
            'publisher_blocked': bool(item.publisher) and BlockedPublisher.objects.filter(
                normalized_name=normalize_name(item.publisher)).exists(),
            'rules_read': rules_summary(item.discovered),
            'rules_attempts': [{
                'attempt': a.attempt, 'stage': a.stage, 'provider': a.provider, 'model': a.model, 'outcome': a.outcome,
                'reason': a.reason, 'missing_fields': a.missing_fields, 'created_at': _iso(a.created_at),
            } for a in item.rules_attempts.order_by('-created_at', '-attempt')[:9]][::-1],
            'decisions': [{
                'decision': d.decision, 'criteria': d.criteria, 'evidence_urls': d.evidence_urls, 'note': d.note,
                'decided_by': d.decided_by, 'decided_at': _iso(d.decided_at),
            } for d in item.review_decisions.all()[:20]],
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
        'worklist': {k: v for k, v in (run.worklist or {}).items() if k != 'not_found_sample'},
        'size_cutoff': run.size_cutoff,
        'pages_fetched': run.pages_fetched,
        'records_seen': run.records_seen,
        'out_of_scope': run.out_of_scope,
        'created': run.created_count,
        'updated': run.updated_count,
        'linked': run.linked_count,
        'removed': run.removed_count,
        'enriched': run.enriched_count,
        'screened': run.screened_count,
        'flagged': run.flagged_count,
        'pages_checked': run.pages_checked,
        'rules_attempted': run.rules_attempted,
        'rules_ready': run.rules_ready,
        'rules_failed': run.rules_failed,
        'rules_retried': run.rules_retried,
        'rules_escalated': run.rules_escalated,
        'calls_checked': run.calls_checked,
        'calls_open': run.calls_open,
        'calls_failed': run.calls_failed,
        'flagged_missing': run.flagged_missing,
        'pending_after': run.pending_after,
        'errors': (run.errors or [])[-10:],
        'summary': run.summary,
    }


@require_GET
@require_platform_superuser
def index_list(request):
    config = IndexConfig()
    base = IndexedVenue.objects.filter(field_profile=config.profile).select_related('venue', 'discovered')
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

    rules_key, rules_label, _subfields = rules_field()
    return JsonResponse({
        'profile': {'key': config.profile, 'label': PROFILES.get(config.profile, {}).get('label', config.profile),
                    'subfields': config.subfields},
        'counts': coverage_counts(config.profile),
        'filter_counts': {name: base.filter(q).count() for name, q in FILTERS.items()},
        'items': [record_payload(item) for item in items[(page - 1) * page_size: page * page_size]],
        'pagination': {'page': page, 'page_size': page_size, 'total': total, 'pages': pages},
        'last_run': run_payload(VenueIndexRun.objects.order_by('-created_at').first()),
        'schedule': get_index_schedule(),
        'criteria': CRITERIA,
        'rules_field': {'key': rules_key, 'label': rules_label, 'due': rules_candidates(config.profile).count()},
        'escalation': escalation_stats(),
        'freshness': freshness_stats(),
        'blocked_publishers': [{'id': str(b.id), 'name': b.name, 'added_by': b.added_by,
                                'created_at': _iso(b.created_at)} for b in BlockedPublisher.objects.all()[:200]],
    })


@require_GET
@require_platform_superuser
def index_detail(request, record_id):
    item = IndexedVenue.objects.select_related('venue', 'discovered').filter(id=record_id).first()
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
    mode = data.get('mode') if data.get('mode') in {'enrich', 'screen', 'rules', 'calls'} else 'full'
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


@require_POST
@require_platform_superuser
def index_decision(request, record_id):
    """A person excludes, keeps or restores a journal. Exclusion needs criteria and evidence."""
    item = IndexedVenue.objects.filter(id=record_id).first()
    if not item:
        return JsonResponse({'detail': 'Index record not found'}, status=404)
    try:
        data = json.loads(request.body or b'{}')
    except ValueError:
        return JsonResponse({'detail': 'Invalid JSON'}, status=400)
    try:
        item = decide(item, decision=str(data.get('decision', '')), user_email=request.editor_user.email,
                      criteria=data.get('criteria') or [], evidence_urls=data.get('evidence_urls') or [],
                      note=data.get('note', ''), block_publisher=bool(data.get('block_publisher')))
    except DecisionError as exc:
        return JsonResponse({'detail': str(exc)}, status=400)
    record_audit_event(request, f'venue_index.{item.screening_status if data.get("decision") != "restore" else "restored"}',
                       resource_type='indexed_venue', resource_id=item.id, venue_id=item.venue_id,
                       detail={'decision': data.get('decision'), 'criteria': (item.exclusion_reason or {}).get('criteria', []),
                               'block_publisher': bool(data.get('block_publisher'))})
    item.refresh_from_db()
    return JsonResponse({'item': record_payload(item, detail=True)})


@require_http_methods(['DELETE'])
@require_platform_superuser
def index_unblock_publisher(request, block_id):
    block = BlockedPublisher.objects.filter(id=block_id).first()
    if not block:
        return JsonResponse({'detail': 'Not found'}, status=404)
    name = block.normalized_name
    block.delete()
    record_audit_event(request, 'venue_index.publisher_unblocked', resource_type='blocked_publisher',
                       resource_id=block_id, detail={'name': name})
    # Its titles are re-screened, so the blocklist concern disappears from the queue.
    names = blocked_publisher_names()
    for record in IndexedVenue.objects.exclude(publisher=''):
        if normalize_name(record.publisher) == name:
            screen_record(record, names)
    return JsonResponse({'removed': True})


@require_POST
@require_platform_superuser
def index_apply_changes(request, record_id):
    """The quarterly re-read found different rules on a live journal's pages: an admin applies them."""
    from .discovery_api import AddToVenueError, apply_discovered_changes
    item = IndexedVenue.objects.select_related('discovered').filter(id=record_id).first()
    if not item or not item.discovered_id:
        return JsonResponse({'detail': 'Index record not found'}, status=404)
    if item.excluded:
        return JsonResponse({'detail': 'This journal is excluded.'}, status=409)
    try:
        apply_discovered_changes(request, item.discovered_id)
    except AddToVenueError as exc:
        return JsonResponse({'detail': exc.detail}, status=exc.status)
    item = IndexedVenue.objects.select_related('venue', 'discovered').get(id=item.id)
    return JsonResponse({'item': record_payload(item, detail=True)})


@require_POST
@require_platform_superuser
def index_publish(request, record_id):
    """An admin approves the rules the AI read: the journal becomes a live venue for authors,
    labelled "Checked from official pages". Reading rules never publishes by itself."""
    from .discovery_api import AddToVenueError, add_discovered_to_venue_agent
    item = IndexedVenue.objects.select_related('discovered').filter(id=record_id).first()
    if not item:
        return JsonResponse({'detail': 'Index record not found'}, status=404)
    if item.excluded:
        return JsonResponse({'detail': 'This journal is excluded. Restore it before publishing.'}, status=409)
    if item.screening_status == 'flagged':
        return JsonResponse({'detail': 'Decide the exclusion review first (Keep or Exclude).'}, status=409)
    if item.rules_status != 'ready' or not item.discovered_id:
        return JsonResponse({'detail': 'Its rules have not been read successfully yet.'}, status=409)
    try:
        discovered, _already = add_discovered_to_venue_agent(request, item.discovered_id)
    except AddToVenueError as exc:
        return JsonResponse({'detail': exc.detail}, status=exc.status)
    item.venue_id = discovered.added_venue_id
    item.save(update_fields=['venue', 'updated_at'])
    record_audit_event(request, 'venue_index.published', resource_type='indexed_venue', resource_id=item.id,
                       venue_id=item.venue_id, detail={'title': item.title, 'confidence': discovered.confidence})
    item = IndexedVenue.objects.select_related('venue', 'discovered').get(id=item.id)
    return JsonResponse({'item': record_payload(item, detail=True)})
