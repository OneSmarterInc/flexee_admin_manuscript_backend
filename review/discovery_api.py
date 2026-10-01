"""Admin API for daily venue discovery (platform superusers only).

The one-click "Add to Venue Agent" endpoint turns a staged DiscoveredVenue into
a live Venue plus an active VenueAgentConfig in a single transaction, reusing
the same slug and configuration validation as manual venue setup.
"""
import json

from django.db import transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .audit import record_audit_event
from .auth import require_platform_superuser
from .models import DiscoveredVenue, Organization, Venue, VenueAgentConfig, VenueDiscoveryRun
from .services.venue_discovery import (
    DiscoveryConfig, TYPE_LABELS, canonical_host, normalize_name, registrable_domain, start_run,
)

LIST_LIMIT = 300


def _iso(value):
    return value.isoformat() if value else None


def _primary_source(item):
    for evidence in item.source_evidence or []:
        if evidence.get('url'):
            return evidence['url']
    return item.submission_url or item.website_url or ''


def discovered_payload(item, *, detail=False, last_run_started=None):
    payload = {
        'id': str(item.id),
        'name': item.name,
        'organization_name': item.organization_name,
        'venue_type': item.venue_type,
        'acceptance_status': item.acceptance_status,
        'submission_types': item.submission_types or [],
        'submission_type_labels': [TYPE_LABELS.get(t, t) for t in item.submission_types or []],
        'confidence': item.confidence,
        'confidence_label': 'high' if item.confidence >= 80 else 'medium' if item.confidence >= 60 else 'low',
        'discovery_status': item.discovery_status,
        'website_url': item.website_url,
        'submission_url': item.submission_url,
        'primary_source_url': _primary_source(item),
        'source_domain': registrable_domain(canonical_host(item.submission_url or item.website_url or '')),
        'first_discovered_at': _iso(item.first_discovered_at),
        'last_checked_at': _iso(item.last_checked_at),
        'added_at': _iso(item.added_at),
        'added_venue_id': str(item.added_venue_id) if item.added_venue_id else None,
        'added_venue_config_id': item.added_venue_config_id,
        'change_summary': item.change_summary,
        'last_error': item.last_error,
        'checked_in_last_run': bool(last_run_started and item.last_checked_at and item.last_checked_at >= last_run_started),
    }
    if detail:
        payload.update({
            'description': item.description,
            'aims_scope': item.aims_scope,
            'article_types': item.article_types,
            'accepted_methods': item.accepted_methods,
            'quality_threshold': item.quality_threshold,
            'reviewer_criteria': item.reviewer_criteria,
            'policies': item.policies,
            'disclosures': item.disclosures,
            'reporting_standards': item.reporting_standards,
            'desk_rejection_rules': item.desk_rejection_rules,
            'structured_desk_rejection_rules': item.structured_desk_rejection_rules,
            'required_submission_items': item.required_submission_items,
            'retention_days': item.retention_days,
            'deadlines': item.deadlines,
            'submission_capacity': item.submission_capacity,
            'current_demand': item.current_demand,
            'config_notes': item.config_notes,
            'source_evidence': item.source_evidence,
            'source_urls': item.source_urls,
        })
    return payload


def discovery_settings_payload(config):
    import os
    if config.mode == 'claude_agent':
        from .services.venue_discovery_agent import agent_settings
        agent = agent_settings()
        return {'enabled': config.enabled, 'mode': 'claude_agent', 'search_provider': f"Claude agent ({agent['model']})",
                'search_configured': bool(agent['api_key']), 'missing_key': 'ANTHROPIC_API_KEY'}
    names = [n.strip() for n in (config.provider or '').split(',') if n.strip()]
    missing = []
    if 'tavily' in names and not config.api_key:
        missing.append('VENUE_SEARCH_API_KEY')
    if 'searxng' in names and not os.getenv('VENUE_SEARXNG_URL', '').strip():
        missing.append('VENUE_SEARXNG_URL')
    label = ' + '.join(names) or '—'
    if config.ai_provider == 'ollama':
        from .services.venue_discovery import local_ai_settings
        label += f" · local {local_ai_settings()['model']}"
    return {'enabled': config.enabled, 'mode': config.mode, 'search_provider': label,
            'search_configured': not missing and bool(names), 'missing_key': ', '.join(missing) or 'VENUE_SEARCH_PROVIDER'}


def run_payload(run):
    if not run:
        return None
    return {
        'id': str(run.id),
        'status': run.status,
        'trigger': run.trigger,
        'created_at': _iso(run.created_at),
        'started_at': _iso(run.started_at),
        'completed_at': _iso(run.completed_at),
        'queries_run': run.queries_run,
        'results_seen': run.results_seen,
        'official_pages_checked': run.official_pages_checked,
        'candidates_created': run.candidates_created,
        'candidates_updated': run.candidates_updated,
        'candidates_changed': run.candidates_changed,
        'error_count': len(run.errors or []),
        'summary': run.summary,
    }


@require_GET
@require_platform_superuser
def discovery_list(request):
    status = request.GET.get('status', 'new')
    acceptance = request.GET.get('acceptance', '')
    venue_type = request.GET.get('type', '')
    query = request.GET.get('q', '').strip()

    # Type and search filters apply to the tab counts too, so a count always
    # matches what the table can show; the status filter is reported separately.
    base = DiscoveredVenue.objects.all()
    if venue_type in {'journal', 'publisher', 'conference'}:
        base = base.filter(venue_type=venue_type)
    if query:
        base = base.filter(Q(name__icontains=query) | Q(organization_name__icontains=query) | Q(aims_scope__icontains=query))
    filtered = base.filter(acceptance_status=acceptance) if acceptance in {'accepting', 'unclear', 'closed'} else base

    items = filtered
    if status in {'new', 'added', 'ignored', 'changed', 'error'}:
        items = items.filter(discovery_status=status)
    items = items.order_by('-confidence', '-last_checked_at')[:LIST_LIMIT]

    keys = ('new', 'added', 'changed', 'ignored', 'error')
    counts = dict(filtered.values_list('discovery_status').annotate(n=Count('id')))
    counts_all = dict(base.values_list('discovery_status').annotate(n=Count('id')))
    hidden_by_status = (counts_all.get(status, 0) - counts.get(status, 0)) if filtered is not base else 0
    last_run = VenueDiscoveryRun.objects.order_by('-created_at').first()
    last_run_started = last_run.started_at if last_run and last_run.status == 'completed' else None
    config = DiscoveryConfig.from_env()
    return JsonResponse({
        'items': [discovered_payload(item, last_run_started=last_run_started) for item in items],
        'counts': {key: counts.get(key, 0) for key in keys},
        'counts_all_statuses': {key: counts_all.get(key, 0) for key in keys},
        'hidden_by_status_filter': max(0, hidden_by_status),
        'last_run': run_payload(last_run),
        'settings': discovery_settings_payload(config),
    })


@require_GET
@require_platform_superuser
def discovery_detail(request, discovered_id):
    try:
        item = DiscoveredVenue.objects.get(id=discovered_id)
    except DiscoveredVenue.DoesNotExist:
        return JsonResponse({'detail': 'Discovered venue not found'}, status=404)
    return JsonResponse({'item': discovered_payload(item, detail=True)})


@require_POST
@require_platform_superuser
def discovery_run_now(request):
    config = DiscoveryConfig.from_env()
    if not config.enabled:
        return JsonResponse({
            'detail': 'Venue discovery is disabled. Set VENUE_DISCOVERY_ENABLED=true on the server to turn it on.',
            'code': 'discovery_disabled',
        }, status=409)
    run, created = start_run(trigger='manual', requested_by=request.editor_user.email)
    if created:
        from django_q.tasks import async_task
        async_task('review.tasks.run_venue_discovery_task', str(run.id))
        record_audit_event(request, 'venue_discovery.run_requested', resource_type='venue_discovery_run',
                           resource_id=run.id, detail={'trigger': 'manual'})
    return JsonResponse({'run': run_payload(run), 'already_running': not created}, status=202)


def _organization_for(item):
    """Reuse an organization only on an exact (case-insensitive) name match."""
    name = (item.organization_name or '').strip() or item.name
    match = Organization.objects.filter(name__iexact=name).first()
    if match:
        return match, False
    org_type = {'publisher': 'publisher', 'conference': 'conference', 'journal': 'journal'}.get(item.venue_type, 'other')
    return Organization.objects.create(name=name[:300], organization_type=org_type), True


def _config_data(item):
    today = timezone.localdate().isoformat()
    checked = timezone.localtime(item.last_checked_at).date().isoformat() if item.last_checked_at else today
    provenance = f'Discovered automatically on {timezone.localtime(item.first_discovered_at).date().isoformat()}. ' \
                 f'Official sources last checked on {checked}.'
    notes = '\n'.join(part for part in [item.config_notes.strip(), provenance] if part)
    return {
        'aims_scope': item.aims_scope,
        'article_types': item.article_types,
        'accepted_methods': item.accepted_methods,
        'quality_threshold': item.quality_threshold,
        'reviewer_criteria': item.reviewer_criteria,
        'policies': item.policies,
        'disclosures': item.disclosures,
        'reporting_standards': item.reporting_standards,
        'desk_rejection_rules': item.desk_rejection_rules,
        'structured_desk_rejection_rules': item.structured_desk_rejection_rules,
        'required_submission_items': item.required_submission_items,
        'retention_days': item.retention_days,
        'deadlines': item.deadlines,
        'submission_capacity': item.submission_capacity,
        'current_demand': item.current_demand,
        'config_notes': notes[:4000],
    }


def _added_response(item, *, already_added, status=200):
    from .author_api import _venue_config_payload, _venue_payload
    venue = item.added_venue
    config = item.added_venue_config
    return JsonResponse({
        'already_added': already_added,
        'discovered_venue': discovered_payload(item),
        'venue': _venue_payload(venue, include_config=False) if venue else None,
        'config': _venue_config_payload(config) if config else None,
    }, status=status)


@require_POST
@require_platform_superuser
def discovery_add_to_venue_agent(request, discovered_id):
    from .author_api import build_venue_config_fields, unique_venue_slug
    try:
        with transaction.atomic():
            try:
                item = DiscoveredVenue.objects.select_for_update().get(id=discovered_id)
            except DiscoveredVenue.DoesNotExist:
                return JsonResponse({'detail': 'Discovered venue not found'}, status=404)
            if item.added_venue_id:
                return _added_response(item, already_added=True)
            if item.discovery_status == 'ignored':
                return JsonResponse({'detail': 'Restore this venue before adding it.'}, status=409)
            if item.acceptance_status == 'closed':
                return JsonResponse({'detail': 'This venue is closed to submissions, so it cannot be added.'}, status=409)
            if not item.name.strip() or item.venue_type not in {'journal', 'conference', 'publisher'}:
                return JsonResponse({'detail': 'This discovered venue is incomplete and cannot be added.'}, status=409)

            config_fields = build_venue_config_fields(_config_data(item))
            organization, org_created = _organization_for(item)
            venue = Venue.objects.create(
                organization=organization,
                name=item.name[:300],
                slug=unique_venue_slug(item.name),
                venue_type=item.venue_type,
                description=(item.description or '')[:2000],
                active=True,
            )
            config = VenueAgentConfig.objects.create(venue=venue, version=1, active=True, **config_fields)

            item.added_venue = venue
            item.added_venue_config = config
            item.discovery_status = 'added'
            item.change_summary = ''
            item.added_at = timezone.now()
            item.save(update_fields=['added_venue', 'added_venue_config', 'discovery_status', 'change_summary',
                                     'added_at', 'updated_at'])

            audit_detail = {
                'discovered_venue_id': str(item.id),
                'venue_id': str(venue.id),
                'venue_config_id': config.id,
                'source_domains': sorted({registrable_domain(canonical_host(u)) for u in (item.source_urls or []) if u})[:5],
                'confidence': item.confidence,
                'acceptance_status': item.acceptance_status,
                'organization_created': org_created,
            }
            record_audit_event(request, 'venue.created_from_discovery', resource_type='venue', resource_id=venue.id,
                               organization_id=organization.id, venue_id=venue.id,
                               detail={'name': venue.name, 'venue_type': venue.venue_type, **audit_detail})
            record_audit_event(request, 'venue_config.created_from_discovery', resource_type='venue_config',
                               resource_id=config.id, organization_id=organization.id, venue_id=venue.id,
                               detail={'version': 1, **audit_detail})
            record_audit_event(request, 'venue_discovery.candidate_added', resource_type='discovered_venue',
                               resource_id=item.id, organization_id=organization.id, venue_id=venue.id,
                               detail=audit_detail)
    except (ValueError, TypeError) as exc:
        # Validation failed: the transaction rolled back, so nothing was created.
        return JsonResponse({'detail': f'The discovered configuration is not valid: {exc}'}, status=422)

    item.refresh_from_db()
    return _added_response(item, already_added=False, status=201)


@require_POST
@require_platform_superuser
def discovery_ignore(request, discovered_id):
    with transaction.atomic():
        try:
            item = DiscoveredVenue.objects.select_for_update().get(id=discovered_id)
        except DiscoveredVenue.DoesNotExist:
            return JsonResponse({'detail': 'Discovered venue not found'}, status=404)
        if item.added_venue_id:
            return JsonResponse({'detail': 'Added venues are managed in Venue Agents.'}, status=409)
        item.discovery_status = 'ignored'
        item.save(update_fields=['discovery_status', 'updated_at'])
        record_audit_event(request, 'venue_discovery.candidate_ignored', resource_type='discovered_venue',
                           resource_id=item.id, detail={'name': item.name})
    return JsonResponse({'item': discovered_payload(item)})


@require_POST
@require_platform_superuser
def discovery_restore(request, discovered_id):
    with transaction.atomic():
        try:
            item = DiscoveredVenue.objects.select_for_update().get(id=discovered_id)
        except DiscoveredVenue.DoesNotExist:
            return JsonResponse({'detail': 'Discovered venue not found'}, status=404)
        if item.discovery_status != 'ignored':
            return JsonResponse({'detail': 'Only ignored venues can be restored.'}, status=409)
        item.discovery_status = 'new'
        item.save(update_fields=['discovery_status', 'updated_at'])
        record_audit_event(request, 'venue_discovery.candidate_restored', resource_type='discovered_venue',
                           resource_id=item.id, detail={'name': item.name})
    return JsonResponse({'item': discovered_payload(item)})
