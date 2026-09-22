import json

from django.db import transaction
from django.http import JsonResponse
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .auth import require_admin
from .models import Organization, Venue, VenueAgentConfig


def _json_body(request):
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
        return payload if isinstance(payload, dict) else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _organization_summary(item):
    return {
        'id': str(item.id),
        'name': item.name,
        'slug': item.slug,
        'organization_type': item.organization_type,
        'status': item.status,
    }


def _config_summary(item):
    return {
        'id': item.id,
        'version': item.version,
        'is_active': item.is_active,
        'aims_scope': item.aims_scope,
        'article_types': item.article_types,
        'accepted_methods': item.accepted_methods,
        'quality_threshold': item.quality_threshold,
        'reviewer_criteria': item.reviewer_criteria,
        'operating_rules': item.operating_rules,
        'current_demand': item.current_demand,
        'editor_notes': item.editor_notes,
        'created_at': item.created_at.isoformat(),
    }


def _venue_summary(item, include_configs=False):
    data = {
        'id': str(item.id),
        'name': item.name,
        'slug': item.slug,
        'venue_type': item.venue_type,
        'status': item.status,
        'website': item.website,
        'description': item.description,
        'organization': _organization_summary(item.organization),
        'created_at': item.created_at.isoformat(),
        'updated_at': item.updated_at.isoformat(),
    }
    if include_configs:
        data['agent_configs'] = [_config_summary(c) for c in item.agent_configs.all()]
    return data


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@require_admin
def admin_venues(request):
    if request.method == 'GET':
        venues = (
            Venue.objects
            .select_related('organization')
            .prefetch_related('agent_configs')
            .all()
        )
        response = JsonResponse({'items': [_venue_summary(v, include_configs=True) for v in venues]})
        response['Cache-Control'] = 'no-store'
        return response

    data = _json_body(request)
    name = str(data.get('name', '')).strip()
    venue_type = str(data.get('venue_type', '')).strip()
    organization_name = str(data.get('organization_name', '')).strip()
    organization_slug = str(data.get('organization_slug', '')).strip() or slugify(organization_name)
    organization_type = str(data.get('organization_type', 'other')).strip() or 'other'
    venue_slug = str(data.get('slug', '')).strip() or slugify(name)

    errors = []
    if not name:
        errors.append('name is required')
    if venue_type not in {'journal', 'conference', 'publisher'}:
        errors.append('venue_type must be journal, conference, or publisher')
    if not organization_name:
        errors.append('organization_name is required')
    if not organization_slug:
        errors.append('organization_slug is required')
    if not venue_slug:
        errors.append('slug is required')
    if organization_type not in {'journal', 'conference', 'publisher', 'other'}:
        errors.append('organization_type is invalid')
    if errors:
        return JsonResponse({'detail': errors[0], 'errors': errors}, status=400)

    if Venue.objects.filter(slug=venue_slug).exists():
        return JsonResponse({'detail': 'A venue with this slug already exists'}, status=409)

    organization, created = Organization.objects.get_or_create(
        slug=organization_slug,
        defaults={
            'name': organization_name,
            'organization_type': organization_type,
            'status': 'active',
        },
    )
    if not created and organization.name != organization_name:
        return JsonResponse({
            'detail': 'organization_slug already belongs to another organization',
            'organization': _organization_summary(organization),
        }, status=409)

    venue = Venue.objects.create(
        organization=organization,
        name=name,
        slug=venue_slug,
        venue_type=venue_type,
        status=str(data.get('status', 'active')).strip() or 'active',
        website=str(data.get('website', '')).strip(),
        description=str(data.get('description', '')).strip(),
    )
    response = JsonResponse({'venue': _venue_summary(venue, include_configs=True)}, status=201)
    response['Cache-Control'] = 'no-store'
    return response


@csrf_exempt
@require_http_methods(['GET', 'PATCH'])
@require_admin
def admin_venue_detail(request, venue_id):
    try:
        venue = (
            Venue.objects
            .select_related('organization')
            .prefetch_related('agent_configs')
            .get(id=venue_id)
        )
    except Venue.DoesNotExist:
        return JsonResponse({'detail': 'Venue not found'}, status=404)

    if request.method == 'PATCH':
        data = _json_body(request)
        changed = []
        for field in ('name', 'website', 'description'):
            if field in data:
                value = str(data[field] or '').strip()
                if field == 'name' and not value:
                    return JsonResponse({'detail': 'name cannot be empty'}, status=400)
                setattr(venue, field, value)
                changed.append(field)
        if 'status' in data:
            status = str(data.get('status', '')).strip()
            if status not in {'active', 'inactive'}:
                return JsonResponse({'detail': 'status must be active or inactive'}, status=400)
            venue.status = status
            changed.append('status')
        if changed:
            venue.save(update_fields=changed + ['updated_at'])

    response = JsonResponse(_venue_summary(venue, include_configs=True))
    response['Cache-Control'] = 'no-store'
    return response


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@require_admin
def admin_venue_configs(request, venue_id):
    try:
        venue = Venue.objects.select_related('organization').get(id=venue_id)
    except Venue.DoesNotExist:
        return JsonResponse({'detail': 'Venue not found'}, status=404)

    if request.method == 'GET':
        configs = venue.agent_configs.all()
        response = JsonResponse({
            'venue': _venue_summary(venue),
            'items': [_config_summary(item) for item in configs],
        })
        response['Cache-Control'] = 'no-store'
        return response

    data = _json_body(request)
    latest = venue.agent_configs.order_by('-version').first()
    version = 1 if latest is None else latest.version + 1
    activate = bool(data.get('is_active', True))

    with transaction.atomic():
        if activate:
            venue.agent_configs.filter(is_active=True).update(is_active=False)
        config = VenueAgentConfig.objects.create(
            venue=venue,
            version=version,
            is_active=activate,
            aims_scope=str(data.get('aims_scope', '')).strip(),
            article_types=data.get('article_types') if isinstance(data.get('article_types'), list) else [],
            accepted_methods=data.get('accepted_methods') if isinstance(data.get('accepted_methods'), list) else [],
            quality_threshold=str(data.get('quality_threshold', '')).strip(),
            reviewer_criteria=data.get('reviewer_criteria') if isinstance(data.get('reviewer_criteria'), list) else [],
            operating_rules=data.get('operating_rules') if isinstance(data.get('operating_rules'), dict) else {},
            current_demand=data.get('current_demand') if isinstance(data.get('current_demand'), dict) else {},
            editor_notes=str(data.get('editor_notes', '')).strip(),
        )

    response = JsonResponse({'config': _config_summary(config)}, status=201)
    response['Cache-Control'] = 'no-store'
    return response


@csrf_exempt
@require_POST
@require_admin
def admin_activate_venue_config(request, venue_id, config_id):
    try:
        config = VenueAgentConfig.objects.select_related('venue').get(id=config_id, venue_id=venue_id)
    except VenueAgentConfig.DoesNotExist:
        return JsonResponse({'detail': 'Venue configuration not found'}, status=404)

    with transaction.atomic():
        VenueAgentConfig.objects.filter(venue_id=venue_id, is_active=True).exclude(id=config.id).update(is_active=False)
        if not config.is_active:
            config.is_active = True
            config.save(update_fields=['is_active'])

    response = JsonResponse({'config': _config_summary(config)})
    response['Cache-Control'] = 'no-store'
    return response
