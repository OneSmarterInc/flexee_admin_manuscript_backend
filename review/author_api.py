import hashlib
import hmac
import json
import os
import secrets

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .models import (
    Manuscript,
    ReadinessAssessment,
    Transfer,
    Venue,
    VenueMatch,
    VenueSubmission,
)


ALLOWED_MANUSCRIPT_EXTENSIONS = ('.docx', '.pdf', '.md', '.zip')


def _json_body(request):
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
        return payload if isinstance(payload, dict) else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _token_hash(token):
    return hashlib.sha256(str(token or '').encode('utf-8')).hexdigest()


def _manuscript_token(request):
    return request.headers.get('X-Manuscript-Token', '').strip()


def _authorized_manuscript(request, manuscript_id):
    try:
        manuscript = Manuscript.objects.get(id=manuscript_id)
    except Manuscript.DoesNotExist:
        return None, JsonResponse({'detail': 'Manuscript not found'}, status=404)

    token = _manuscript_token(request)
    if not token or not hmac.compare_digest(_token_hash(token), manuscript.access_token_hash):
        response = JsonResponse({'detail': 'Manuscript access token required'}, status=401)
        response['Cache-Control'] = 'no-store'
        return None, response
    return manuscript, None


def _parse_keywords(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raw = str(value or '').strip()
    if not raw:
        return []
    if raw.startswith('['):
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, list):
                return [str(item).strip() for item in decoded if str(item).strip()]
        except json.JSONDecodeError:
            pass
    return [part.strip() for part in raw.split(',') if part.strip()]


def _active_config(venue):
    return venue.agent_configs.filter(is_active=True).order_by('-version', '-created_at').first()


def _public_config(config):
    if not config:
        return None
    return {
        'id': config.id,
        'version': config.version,
        'aims_scope': config.aims_scope,
        'article_types': config.article_types,
        'accepted_methods': config.accepted_methods,
        'quality_threshold': config.quality_threshold,
        'reviewer_criteria': config.reviewer_criteria,
        'operating_rules': config.operating_rules,
        'current_demand': config.current_demand,
    }


def _venue_summary(venue, include_config=False):
    data = {
        'id': str(venue.id),
        'name': venue.name,
        'slug': venue.slug,
        'venue_type': venue.venue_type,
        'website': venue.website,
        'description': venue.description,
        'organization': {
            'id': str(venue.organization_id),
            'name': venue.organization.name,
            'slug': venue.organization.slug,
            'organization_type': venue.organization.organization_type,
        },
    }
    if include_config:
        data['active_config'] = _public_config(_active_config(venue))
    return data


def _manuscript_summary(manuscript):
    return {
        'id': str(manuscript.id),
        'created_at': manuscript.created_at.isoformat(),
        'updated_at': manuscript.updated_at.isoformat(),
        'status': manuscript.status,
        'author_name': manuscript.author_name,
        'author_email': manuscript.author_email,
        'coauthors': manuscript.coauthors,
        'title': manuscript.title,
        'manuscript_type': manuscript.manuscript_type,
        'abstract': manuscript.abstract,
        'keywords': manuscript.keywords,
        'ai_disclosure': manuscript.ai_disclosure,
        'notes': manuscript.notes,
        'manuscript_filename': manuscript.manuscript_filename,
        'manuscript_bytes': manuscript.manuscript_bytes,
        'manuscript_sha256': manuscript.manuscript_sha256,
        'profile': manuscript.profile,
    }


def _readiness_summary(item):
    if not item:
        return None
    return {
        'id': str(item.id),
        'created_at': item.created_at.isoformat(),
        'status': item.status,
        'engine_version': item.engine_version,
        'summary': item.summary,
        'findings': item.findings,
        'error': item.error,
    }


def _match_summary(item):
    return {
        'id': str(item.id),
        'created_at': item.created_at.isoformat(),
        'eligibility': item.eligibility,
        'fit_summary': item.fit_summary,
        'reasons': item.reasons,
        'gaps': item.gaps,
        'evidence': item.evidence,
        'engine_version': item.engine_version,
        'venue_config_version': item.venue_config.version if item.venue_config else None,
        'venue': _venue_summary(item.venue),
    }


def _submission_summary(item, include_packet=False):
    data = {
        'id': str(item.id),
        'created_at': item.created_at.isoformat(),
        'updated_at': item.updated_at.isoformat(),
        'submitted_at': item.submitted_at.isoformat() if item.submitted_at else None,
        'decided_at': item.decided_at.isoformat() if item.decided_at else None,
        'status': item.status,
        'decision': item.decision,
        'decision_detail': item.decision_detail,
        'venue_config_version': item.venue_config.version if item.venue_config else None,
        'venue': _venue_summary(item.venue),
        'manuscript': {
            'id': str(item.manuscript_id),
            'title': item.manuscript.title,
        },
    }
    if include_packet:
        data['packet'] = item.packet
        data['editorial_brief'] = item.editorial_brief
        data['evidence'] = [
            {
                'id': str(e.id),
                'source_type': e.source_type,
                'source_locator': e.source_locator,
                'claim': e.claim,
                'excerpt': e.excerpt,
                'external_url': e.external_url,
                'verification': e.verification,
            }
            for e in item.evidence_findings.all()
        ]
    return data


@require_GET
def author_venues(request):
    venues = (
        Venue.objects
        .filter(status='active', organization__status='active')
        .select_related('organization')
        .prefetch_related('agent_configs')
    )
    return JsonResponse({'items': [_venue_summary(v, include_config=True) for v in venues]})


@require_GET
def author_venue_detail(request, venue_id):
    try:
        venue = (
            Venue.objects
            .select_related('organization')
            .prefetch_related('agent_configs')
            .get(id=venue_id, status='active', organization__status='active')
        )
    except Venue.DoesNotExist:
        return JsonResponse({'detail': 'Venue not found'}, status=404)
    return JsonResponse(_venue_summary(venue, include_config=True))


@csrf_exempt
@require_http_methods(['POST'])
def author_manuscripts(request):
    max_bytes = int(os.getenv('MAX_MANUSCRIPT_BYTES', str(20 * 1024 * 1024)))
    upload = request.FILES.get('manuscript')
    author_name = request.POST.get('author', '').strip()
    author_email = request.POST.get('email', '').strip()
    coauthors = request.POST.get('coauthors', '').strip()
    title = request.POST.get('title', '').strip()
    manuscript_type = request.POST.get('manuscript_type', request.POST.get('type', '')).strip()
    abstract = request.POST.get('abstract', '').strip()
    keywords = _parse_keywords(request.POST.get('keywords', ''))
    ai_disclosure = request.POST.get('disclosure', '').strip()
    notes = request.POST.get('notes', '').strip()

    errors = []
    if not author_name:
        errors.append('author is required')
    if not title:
        errors.append('title is required')
    if not ai_disclosure:
        errors.append('disclosure is required')
    if not upload:
        errors.append('manuscript is required')
    elif upload.size <= 0:
        errors.append('manuscript is empty')
    elif upload.size > max_bytes:
        errors.append(f'manuscript exceeds the {max_bytes // 1024 // 1024} MB upload limit')
    elif not upload.name.lower().endswith(ALLOWED_MANUSCRIPT_EXTENSIONS):
        errors.append('manuscript must be a .docx, .pdf, .md, or .zip file')
    if errors:
        return JsonResponse({'detail': errors[0], 'errors': errors}, status=400)

    content = upload.read()
    upload.seek(0)
    digest = hashlib.sha256(content).hexdigest()
    raw_token = secrets.token_urlsafe(32)

    manuscript = Manuscript.objects.create(
        author_name=author_name,
        author_email=author_email,
        coauthors=coauthors,
        title=title,
        manuscript_type=manuscript_type,
        abstract=abstract,
        keywords=keywords,
        ai_disclosure=ai_disclosure,
        notes=notes,
        manuscript_filename=upload.name,
        manuscript_file=upload,
        manuscript_bytes=upload.size,
        manuscript_sha256=digest,
        access_token_hash=_token_hash(raw_token),
    )
    response = JsonResponse({
        'manuscript': _manuscript_summary(manuscript),
        'access_token': raw_token,
        'next': {
            'readiness': f'/api/author/manuscripts/{manuscript.id}/readiness/',
            'matches': f'/api/author/manuscripts/{manuscript.id}/matches/',
        },
    }, status=201)
    response['Cache-Control'] = 'no-store'
    return response


@csrf_exempt
@require_http_methods(['GET', 'PATCH'])
def author_manuscript_detail(request, manuscript_id):
    manuscript, error = _authorized_manuscript(request, manuscript_id)
    if error:
        return error

    if request.method == 'PATCH':
        data = _json_body(request)
        allowed = {
            'author_name', 'author_email', 'coauthors', 'title', 'manuscript_type',
            'abstract', 'ai_disclosure', 'notes',
        }
        changed = []
        for field in allowed:
            if field in data:
                value = str(data[field] or '').strip()
                if field in {'author_name', 'title', 'ai_disclosure'} and not value:
                    return JsonResponse({'detail': f'{field} cannot be empty'}, status=400)
                setattr(manuscript, field, value)
                changed.append(field)
        if 'keywords' in data:
            manuscript.keywords = _parse_keywords(data.get('keywords'))
            changed.append('keywords')
        if changed:
            manuscript.save(update_fields=changed + ['updated_at'])

    response = JsonResponse(_manuscript_summary(manuscript))
    response['Cache-Control'] = 'no-store'
    return response


@require_GET
def author_readiness(request, manuscript_id):
    manuscript, error = _authorized_manuscript(request, manuscript_id)
    if error:
        return error
    latest = manuscript.readiness_assessments.first()
    response = JsonResponse({
        'manuscript_id': str(manuscript.id),
        'assessment': _readiness_summary(latest),
        'available': latest is not None,
    })
    response['Cache-Control'] = 'no-store'
    return response


@require_GET
def author_matches(request, manuscript_id):
    manuscript, error = _authorized_manuscript(request, manuscript_id)
    if error:
        return error
    matches = (
        manuscript.venue_matches
        .select_related('venue', 'venue__organization', 'venue_config')
        .order_by('-created_at')
    )
    response = JsonResponse({
        'manuscript_id': str(manuscript.id),
        'items': [_match_summary(item) for item in matches],
    })
    response['Cache-Control'] = 'no-store'
    return response


@csrf_exempt
@require_POST
def author_choose_venue(request, manuscript_id):
    manuscript, error = _authorized_manuscript(request, manuscript_id)
    if error:
        return error
    data = _json_body(request)
    venue_id = data.get('venue_id')
    if not venue_id:
        return JsonResponse({'detail': 'venue_id is required'}, status=400)
    try:
        venue = Venue.objects.select_related('organization').get(
            id=venue_id,
            status='active',
            organization__status='active',
        )
    except (Venue.DoesNotExist, ValueError):
        return JsonResponse({'detail': 'Active venue not found'}, status=404)

    config = _active_config(venue)
    if not config:
        return JsonResponse({'detail': 'Venue does not have an active agent configuration'}, status=409)

    submission = VenueSubmission.objects.create(
        manuscript=manuscript,
        venue=venue,
        venue_config=config,
        status='draft',
    )
    response = JsonResponse({'submission': _submission_summary(submission, include_packet=True)}, status=201)
    response['Cache-Control'] = 'no-store'
    return response


@require_GET
def author_submission_detail(request, submission_id):
    try:
        submission = (
            VenueSubmission.objects
            .select_related('manuscript', 'venue', 'venue__organization', 'venue_config')
            .prefetch_related('evidence_findings')
            .get(id=submission_id)
        )
    except VenueSubmission.DoesNotExist:
        return JsonResponse({'detail': 'Venue submission not found'}, status=404)

    manuscript, error = _authorized_manuscript(request, submission.manuscript_id)
    if error:
        return error
    response = JsonResponse(_submission_summary(submission, include_packet=True))
    response['Cache-Control'] = 'no-store'
    return response


@csrf_exempt
@require_POST
def author_submit_packet(request, submission_id):
    try:
        submission = (
            VenueSubmission.objects
            .select_related('manuscript', 'venue', 'venue__organization', 'venue_config')
            .get(id=submission_id)
        )
    except VenueSubmission.DoesNotExist:
        return JsonResponse({'detail': 'Venue submission not found'}, status=404)

    manuscript, error = _authorized_manuscript(request, submission.manuscript_id)
    if error:
        return error
    if submission.status != 'packet_ready':
        return JsonResponse({
            'detail': 'Submission packet is not ready',
            'status': submission.status,
        }, status=409)

    submission.status = 'submitted'
    submission.submitted_at = timezone.now()
    submission.save(update_fields=['status', 'submitted_at', 'updated_at'])
    response = JsonResponse({'submission': _submission_summary(submission, include_packet=True)})
    response['Cache-Control'] = 'no-store'
    return response


@csrf_exempt
@require_POST
def author_transfer(request, submission_id):
    try:
        source = (
            VenueSubmission.objects
            .select_related('manuscript', 'venue', 'venue__organization')
            .get(id=submission_id)
        )
    except VenueSubmission.DoesNotExist:
        return JsonResponse({'detail': 'Venue submission not found'}, status=404)

    manuscript, error = _authorized_manuscript(request, source.manuscript_id)
    if error:
        return error
    if source.status not in {'rejected', 'withdrawn'}:
        return JsonResponse({
            'detail': 'Transfer is available after rejection or withdrawal',
            'status': source.status,
        }, status=409)

    data = _json_body(request)
    venue_id = data.get('venue_id')
    if not venue_id:
        return JsonResponse({'detail': 'venue_id is required'}, status=400)
    try:
        venue = Venue.objects.select_related('organization').get(
            id=venue_id,
            status='active',
            organization__status='active',
        )
    except (Venue.DoesNotExist, ValueError):
        return JsonResponse({'detail': 'Active venue not found'}, status=404)
    if venue.id == source.venue_id:
        return JsonResponse({'detail': 'Transfer destination must be a different venue'}, status=400)

    config = _active_config(venue)
    if not config:
        return JsonResponse({'detail': 'Venue does not have an active agent configuration'}, status=409)

    with transaction.atomic():
        target = VenueSubmission.objects.create(
            manuscript=manuscript,
            venue=venue,
            venue_config=config,
            status='draft',
        )
        transfer = Transfer.objects.create(
            manuscript=manuscript,
            from_submission=source,
            to_submission=target,
            status='prepared',
            detail={'from_venue_id': str(source.venue_id), 'to_venue_id': str(venue.id)},
        )
        source.status = 'transferred'
        source.save(update_fields=['status', 'updated_at'])

    response = JsonResponse({
        'transfer': {
            'id': str(transfer.id),
            'status': transfer.status,
            'created_at': transfer.created_at.isoformat(),
        },
        'submission': _submission_summary(target, include_packet=True),
    }, status=201)
    response['Cache-Control'] = 'no-store'
    return response
