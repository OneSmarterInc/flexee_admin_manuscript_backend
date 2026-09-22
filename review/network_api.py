import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .auth import require_admin
from .models import (
    EditorFeedback,
    EvidenceFinding,
    Manuscript,
    ReadinessAssessment,
    Venue,
    VenueAgentConfig,
    VenueAssessment,
    VenueMatch,
    VenueSubmission,
    VenueSubmissionEvent,
)


ALLOWED_MANUSCRIPT_EXTENSIONS = {'.docx', '.pdf', '.md', '.zip'}


def _json_body(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _list_value(value, *, separator=','):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or '').strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except (ValueError, TypeError, json.JSONDecodeError):
        pass
    return [item.strip() for item in text.split(separator) if item.strip()]


def _bool_value(value):
    if isinstance(value, bool):
        return value
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on', 'accepted'}


def _hash_access_key(raw):
    return hashlib.sha256(str(raw or '').encode('utf-8')).hexdigest()


def _new_access_key():
    return secrets.token_urlsafe(32)


def _request_access_key(request):
    direct = request.META.get('HTTP_X_MANUSCRIPT_KEY', '').strip()
    if direct:
        return direct
    authorization = request.META.get('HTTP_AUTHORIZATION', '').strip()
    if authorization.lower().startswith('bearer '):
        return authorization[7:].strip()
    return ''


def _get_author_manuscript(request, manuscript_id):
    try:
        manuscript = Manuscript.objects.get(id=manuscript_id)
    except Manuscript.DoesNotExist:
        return None

    supplied = _request_access_key(request)
    if not supplied:
        return None
    if not hmac.compare_digest(_hash_access_key(supplied), manuscript.access_key_hash):
        return None
    return manuscript


def _manuscript_summary(manuscript):
    return {
        'id': str(manuscript.id),
        'created_at': manuscript.created_at.isoformat(),
        'updated_at': manuscript.updated_at.isoformat(),
        'status': manuscript.status,
        'title': manuscript.title,
        'manuscript_type': manuscript.manuscript_type,
        'primary_author_name': manuscript.primary_author_name,
        'primary_author_email': manuscript.primary_author_email,
        'coauthors': manuscript.coauthors,
        'abstract': manuscript.abstract,
        'keywords': manuscript.keywords,
        'ai_disclosure': manuscript.ai_disclosure,
        'authorship_attested': manuscript.authorship_attested,
        'author_notes': manuscript.author_notes,
        'manuscript_filename': manuscript.manuscript_filename,
        'manuscript_bytes': manuscript.manuscript_bytes,
        'manuscript_sha256': manuscript.manuscript_sha256,
        'metadata': manuscript.metadata,
    }


def _venue_public(venue, *, include_config=False):
    data = {
        'id': str(venue.id),
        'name': venue.name,
        'slug': venue.slug,
        'venue_type': venue.venue_type,
        'subscriber_name': venue.subscriber_name,
        'description': venue.description,
        'website': venue.website,
        'active': venue.active,
    }
    if include_config:
        try:
            config = venue.agent_config
        except VenueAgentConfig.DoesNotExist:
            config = None
        data['agent_config'] = _venue_config(config) if config else None
    return data


def _venue_config(config):
    return {
        'version': config.version,
        'aims_scope': config.aims_scope,
        'accepted_article_types': config.accepted_article_types,
        'accepted_methods': config.accepted_methods,
        'quality_threshold': config.quality_threshold,
        'policies': config.policies,
        'disclosures': config.disclosures,
        'reporting_standards': config.reporting_standards,
        'desk_rejection_rules': config.desk_rejection_rules,
        'deadline_notes': config.deadline_notes,
        'submission_capacity': config.submission_capacity,
        'current_demand': config.current_demand,
        'reviewer_criteria': config.reviewer_criteria,
        'updated_at': config.updated_at.isoformat() if config.updated_at else None,
    }


def _readiness_payload(item):
    if not item:
        return {'status': 'not_started', 'assessment': None}
    return {
        'status': item.status,
        'assessment': {
            'id': str(item.id),
            'created_at': item.created_at.isoformat(),
            'overall_state': item.overall_state,
            'checks': item.checks,
            'summary': item.summary,
            'warnings_count': item.warnings_count,
            'blocking_count': item.blocking_count,
            'engine_version': item.engine_version,
            'error': item.error,
            'evidence': [_evidence_payload(e) for e in item.evidence.all()],
        },
    }


def _match_payload(item):
    return {
        'id': str(item.id),
        'created_at': item.created_at.isoformat(),
        'venue': _venue_public(item.venue, include_config=False),
        'fit_level': item.fit_level,
        'explanation': item.explanation,
        'reasons': item.reasons,
        'gaps': item.gaps,
        'required_changes': item.required_changes,
        'matching_metadata': item.matching_metadata,
    }


def _evidence_payload(item):
    return {
        'id': str(item.id),
        'finding_type': item.finding_type,
        'finding': item.finding,
        'source_type': item.source_type,
        'source_reference': item.source_reference,
        'source_excerpt': item.source_excerpt,
        'external_url': item.external_url,
        'verification_status': item.verification_status,
        'detail': item.detail,
    }


def _assessment_payload(item):
    if not item:
        return {'status': 'not_started', 'assessment': None}
    return {
        'status': item.status,
        'assessment': {
            'id': str(item.id),
            'created_at': item.created_at.isoformat(),
            'venue': _venue_public(item.venue, include_config=True),
            'editorial_brief': item.editorial_brief,
            'unresolved_risks': item.unresolved_risks,
            'reviewer_expertise': item.reviewer_expertise,
            'agent_config_version': item.agent_config_version,
            'engine_version': item.engine_version,
            'error': item.error,
            'evidence': [_evidence_payload(e) for e in item.evidence.all()],
        },
    }


def _submission_payload(item, *, include_events=False):
    data = {
        'id': str(item.id),
        'manuscript_id': str(item.manuscript_id),
        'venue': _venue_public(item.venue, include_config=False),
        'parent_submission_id': str(item.parent_submission_id) if item.parent_submission_id else None,
        'created_at': item.created_at.isoformat(),
        'updated_at': item.updated_at.isoformat(),
        'selected_at': item.selected_at.isoformat(),
        'submitted_at': item.submitted_at.isoformat() if item.submitted_at else None,
        'decided_at': item.decided_at.isoformat() if item.decided_at else None,
        'status': item.status,
        'packet': item.packet,
        'decision_note': item.decision_note,
        'is_current': item.is_current,
    }
    if include_events:
        data['events'] = [
            {
                'id': event.id,
                'created_at': event.created_at.isoformat(),
                'event_type': event.event_type,
                'detail': event.detail,
            }
            for event in item.events.all()
        ]
    return data


@csrf_exempt
@require_POST
def author_create_manuscript(request):
    max_bytes = int(os.getenv('MAX_MANUSCRIPT_BYTES', str(20 * 1024 * 1024)))
    upload = request.FILES.get('manuscript')
    title = request.POST.get('title', '').strip()
    manuscript_type = request.POST.get('manuscript_type', request.POST.get('type', '')).strip()
    author_name = request.POST.get('author', request.POST.get('primary_author_name', '')).strip()
    author_email = request.POST.get('email', request.POST.get('primary_author_email', '')).strip()
    disclosure = request.POST.get('disclosure', request.POST.get('ai_disclosure', '')).strip()
    attested = _bool_value(request.POST.get('attestation', request.POST.get('authorship_attested', '')))

    errors = []
    if not title:
        errors.append('title is required')
    if not manuscript_type:
        errors.append('manuscript_type is required')
    if not author_name:
        errors.append('author is required')
    if not disclosure:
        errors.append('ai_disclosure is required')
    if not attested:
        errors.append('authorship attestation is required')
    if not upload:
        errors.append('manuscript is required')
    elif upload.size <= 0:
        errors.append('manuscript is empty')
    elif upload.size > max_bytes:
        errors.append(f'manuscript exceeds the {max_bytes // 1024 // 1024} MB upload limit')
    elif Path(upload.name).suffix.lower() not in ALLOWED_MANUSCRIPT_EXTENSIONS:
        errors.append('manuscript must be a .docx, .pdf, .md, or .zip file')

    if errors:
        return JsonResponse({'detail': errors[0], 'errors': errors}, status=400)

    content = upload.read()
    upload.seek(0)
    access_key = _new_access_key()

    manuscript = Manuscript.objects.create(
        title=title,
        manuscript_type=manuscript_type,
        primary_author_name=author_name,
        primary_author_email=author_email,
        coauthors=_list_value(request.POST.get('coauthors', ''), separator=';'),
        abstract=request.POST.get('abstract', '').strip(),
        keywords=_list_value(request.POST.get('keywords', ''), separator=','),
        ai_disclosure=disclosure,
        authorship_attested=True,
        author_notes=request.POST.get('notes', request.POST.get('author_notes', '')).strip(),
        manuscript_filename=upload.name,
        manuscript_file=upload,
        manuscript_bytes=len(content),
        manuscript_sha256=hashlib.sha256(content).hexdigest(),
        access_key_hash=_hash_access_key(access_key),
        metadata={},
    )

    response = JsonResponse({
        'manuscript': _manuscript_summary(manuscript),
        'access_key': access_key,
        'next': f'/api/author/manuscripts/{manuscript.id}/readiness/',
    }, status=201)
    response['Cache-Control'] = 'no-store'
    return response


@require_GET
def author_manuscript_detail(request, manuscript_id):
    manuscript = _get_author_manuscript(request, manuscript_id)
    if not manuscript:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)

    latest_readiness = manuscript.readiness_assessments.prefetch_related('evidence').first()
    matches = manuscript.venue_matches.select_related('venue').all()
    current_submission = manuscript.venue_submissions.select_related('venue').filter(is_current=True).first()

    response = JsonResponse({
        'manuscript': _manuscript_summary(manuscript),
        'readiness': _readiness_payload(latest_readiness),
        'venue_matches': [_match_payload(item) for item in matches],
        'current_submission': _submission_payload(current_submission) if current_submission else None,
    })
    response['Cache-Control'] = 'no-store'
    return response


@require_GET
def author_readiness(request, manuscript_id):
    manuscript = _get_author_manuscript(request, manuscript_id)
    if not manuscript:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)
    latest = manuscript.readiness_assessments.prefetch_related('evidence').first()
    response = JsonResponse(_readiness_payload(latest))
    response['Cache-Control'] = 'no-store'
    return response


@require_GET
def author_venue_matches(request, manuscript_id):
    manuscript = _get_author_manuscript(request, manuscript_id)
    if not manuscript:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)
    matches = manuscript.venue_matches.select_related('venue').filter(venue__active=True)
    response = JsonResponse({'items': [_match_payload(item) for item in matches]})
    response['Cache-Control'] = 'no-store'
    return response


@require_GET
def author_venue_assessment(request, manuscript_id, venue_slug):
    manuscript = _get_author_manuscript(request, manuscript_id)
    if not manuscript:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)
    try:
        venue = Venue.objects.get(slug=venue_slug, active=True)
    except Venue.DoesNotExist:
        return JsonResponse({'detail': 'Venue not found'}, status=404)

    item = manuscript.venue_assessments.select_related('venue').prefetch_related('evidence').filter(venue=venue).first()
    response = JsonResponse(_assessment_payload(item))
    response['Cache-Control'] = 'no-store'
    return response


@require_GET
def public_venues(request):
    items = Venue.objects.filter(active=True).select_related('agent_config')
    response = JsonResponse({'items': [_venue_public(item, include_config=True) for item in items]})
    response['Cache-Control'] = 'no-store'
    return response


def _packet_snapshot(manuscript, venue, assessment):
    try:
        config = venue.agent_config
    except VenueAgentConfig.DoesNotExist:
        config = None
    return {
        'manuscript': {
            'id': str(manuscript.id),
            'title': manuscript.title,
            'manuscript_type': manuscript.manuscript_type,
            'filename': manuscript.manuscript_filename,
            'sha256': manuscript.manuscript_sha256,
            'primary_author_name': manuscript.primary_author_name,
            'primary_author_email': manuscript.primary_author_email,
            'coauthors': manuscript.coauthors,
            'ai_disclosure': manuscript.ai_disclosure,
        },
        'venue': {
            'id': str(venue.id),
            'name': venue.name,
            'slug': venue.slug,
            'venue_type': venue.venue_type,
            'agent_config_version': config.version if config else None,
        },
        'assessment_id': str(assessment.id) if assessment else None,
        'prepared_at': timezone.now().isoformat(),
    }


@csrf_exempt
@require_POST
def author_choose_venue(request, manuscript_id):
    manuscript = _get_author_manuscript(request, manuscript_id)
    if not manuscript:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)
    data = _json_body(request)
    venue_slug = str(data.get('venue_slug', '')).strip()
    if not venue_slug:
        return JsonResponse({'detail': 'venue_slug is required'}, status=400)
    try:
        venue = Venue.objects.get(slug=venue_slug, active=True)
    except Venue.DoesNotExist:
        return JsonResponse({'detail': 'Venue not found'}, status=404)

    match = manuscript.venue_matches.filter(venue=venue).first()
    if not match:
        return JsonResponse({'detail': 'This venue has not been matched to the manuscript yet'}, status=409)
    if match.fit_level == 'not_fit':
        return JsonResponse({'detail': 'This venue is marked as not a fit for the manuscript'}, status=409)

    readiness = manuscript.readiness_assessments.filter(status='completed').first()
    assessment = manuscript.venue_assessments.filter(venue=venue, status='completed').first()
    packet_ready = bool(readiness and readiness.overall_state == 'ready' and assessment)

    with transaction.atomic():
        manuscript.venue_submissions.filter(is_current=True).update(is_current=False)
        submission = VenueSubmission.objects.create(
            manuscript=manuscript,
            venue=venue,
            status='packet_ready' if packet_ready else 'draft',
            packet=_packet_snapshot(manuscript, venue, assessment),
            is_current=True,
        )
        VenueSubmissionEvent.objects.create(
            submission=submission,
            event_type='venue_selected',
            detail={
                'venue_slug': venue.slug,
                'packet_ready': packet_ready,
                'readiness_id': str(readiness.id) if readiness else None,
                'assessment_id': str(assessment.id) if assessment else None,
            },
        )
        manuscript.status = 'venue_selected'
        manuscript.save(update_fields=['status', 'updated_at'])

    response = JsonResponse({
        'submission': _submission_payload(submission),
        'packet_ready': packet_ready,
        'detail': None if packet_ready else 'Venue selected, but a completed ready-state assessment is required before formal submission.',
    }, status=201)
    response['Cache-Control'] = 'no-store'
    return response


@csrf_exempt
@require_POST
def author_submit_packet(request, manuscript_id):
    manuscript = _get_author_manuscript(request, manuscript_id)
    if not manuscript:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)

    with transaction.atomic():
        submission = manuscript.venue_submissions.select_for_update().select_related('venue').filter(is_current=True).first()
        if not submission:
            return JsonResponse({'detail': 'Choose a venue before submitting'}, status=409)
        if submission.status != 'packet_ready':
            return JsonResponse({'detail': 'Submission packet is not ready'}, status=409)

        now = timezone.now()
        submission.status = 'submitted'
        submission.submitted_at = now
        submission.save(update_fields=['status', 'submitted_at', 'updated_at'])
        VenueSubmissionEvent.objects.create(
            submission=submission,
            event_type='submitted',
            detail={'submitted_at': now.isoformat()},
        )
        manuscript.status = 'submitted'
        manuscript.save(update_fields=['status', 'updated_at'])

    response = JsonResponse({'submission': _submission_payload(submission)})
    response['Cache-Control'] = 'no-store'
    return response


@csrf_exempt
@require_POST
def author_transfer_submission(request, manuscript_id):
    manuscript = _get_author_manuscript(request, manuscript_id)
    if not manuscript:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)
    data = _json_body(request)
    source_id = str(data.get('source_submission_id', '')).strip()
    venue_slug = str(data.get('venue_slug', '')).strip()
    if not source_id or not venue_slug:
        return JsonResponse({'detail': 'source_submission_id and venue_slug are required'}, status=400)

    try:
        source = manuscript.venue_submissions.select_related('venue').get(id=source_id)
    except (VenueSubmission.DoesNotExist, ValueError):
        return JsonResponse({'detail': 'Source submission not found'}, status=404)
    if source.status not in {'rejected', 'withdrawn'}:
        return JsonResponse({'detail': 'Transfer is available after rejection or withdrawal'}, status=409)

    try:
        venue = Venue.objects.get(slug=venue_slug, active=True)
    except Venue.DoesNotExist:
        return JsonResponse({'detail': 'Venue not found'}, status=404)
    if venue.id == source.venue_id:
        return JsonResponse({'detail': 'Choose a different venue for transfer'}, status=400)

    match = manuscript.venue_matches.filter(venue=venue).first()
    if not match or match.fit_level == 'not_fit':
        return JsonResponse({'detail': 'The destination venue is not an eligible manuscript match'}, status=409)

    readiness = manuscript.readiness_assessments.filter(status='completed').first()
    assessment = manuscript.venue_assessments.filter(venue=venue, status='completed').first()
    packet_ready = bool(readiness and readiness.overall_state == 'ready' and assessment)

    with transaction.atomic():
        manuscript.venue_submissions.filter(is_current=True).update(is_current=False)
        submission = VenueSubmission.objects.create(
            manuscript=manuscript,
            venue=venue,
            parent_submission=source,
            status='packet_ready' if packet_ready else 'draft',
            packet=_packet_snapshot(manuscript, venue, assessment),
            is_current=True,
        )
        VenueSubmissionEvent.objects.create(
            submission=submission,
            event_type='transfer_prepared',
            detail={'source_submission_id': str(source.id), 'venue_slug': venue.slug, 'packet_ready': packet_ready},
        )
        manuscript.status = 'venue_selected'
        manuscript.save(update_fields=['status', 'updated_at'])

    response = JsonResponse({'submission': _submission_payload(submission), 'packet_ready': packet_ready}, status=201)
    response['Cache-Control'] = 'no-store'
    return response


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@require_admin
def admin_venues(request):
    if request.method == 'GET':
        items = Venue.objects.select_related('agent_config').all()
        response = JsonResponse({'items': [_venue_public(item, include_config=True) for item in items]})
        response['Cache-Control'] = 'no-store'
        return response

    data = _json_body(request)
    name = str(data.get('name', '')).strip()
    venue_type = str(data.get('venue_type', '')).strip()
    slug = str(data.get('slug', '')).strip() or slugify(name)
    errors = []
    if not name:
        errors.append('name is required')
    if venue_type not in dict(Venue.TYPE_CHOICES):
        errors.append('venue_type is invalid')
    if not slug:
        errors.append('slug is required')
    if Venue.objects.filter(slug=slug).exists():
        errors.append('slug already exists')
    if errors:
        return JsonResponse({'detail': errors[0], 'errors': errors}, status=400)

    with transaction.atomic():
        venue = Venue.objects.create(
            name=name,
            slug=slug,
            venue_type=venue_type,
            subscriber_name=str(data.get('subscriber_name', '')).strip(),
            description=str(data.get('description', '')).strip(),
            website=str(data.get('website', '')).strip(),
            active=_bool_value(data.get('active', True)),
        )
        VenueAgentConfig.objects.create(
            venue=venue,
            aims_scope=str(data.get('aims_scope', '')).strip(),
            accepted_article_types=_list_value(data.get('accepted_article_types', [])),
            accepted_methods=_list_value(data.get('accepted_methods', [])),
            quality_threshold=str(data.get('quality_threshold', '')).strip(),
            policies=data.get('policies') if isinstance(data.get('policies'), dict) else {},
            disclosures=_list_value(data.get('disclosures', [])),
            reporting_standards=_list_value(data.get('reporting_standards', [])),
            desk_rejection_rules=_list_value(data.get('desk_rejection_rules', [])),
            deadline_notes=str(data.get('deadline_notes', '')).strip(),
            submission_capacity=data.get('submission_capacity') or None,
            current_demand=data.get('current_demand') if isinstance(data.get('current_demand'), dict) else {},
            reviewer_criteria=data.get('reviewer_criteria') if isinstance(data.get('reviewer_criteria'), dict) else {},
        )
    return JsonResponse({'venue': _venue_public(venue, include_config=True)}, status=201)


@csrf_exempt
@require_http_methods(['GET', 'POST'])
@require_admin
def admin_venue_detail(request, venue_id):
    try:
        venue = Venue.objects.select_related('agent_config').get(id=venue_id)
    except Venue.DoesNotExist:
        return JsonResponse({'detail': 'Venue not found'}, status=404)

    if request.method == 'GET':
        response = JsonResponse({'venue': _venue_public(venue, include_config=True)})
        response['Cache-Control'] = 'no-store'
        return response

    data = _json_body(request)
    venue_fields = {
        'name': 'name',
        'slug': 'slug',
        'venue_type': 'venue_type',
        'subscriber_name': 'subscriber_name',
        'description': 'description',
        'website': 'website',
    }
    changed = []
    for key, field in venue_fields.items():
        if key in data:
            value = str(data.get(key, '')).strip()
            if key == 'venue_type' and value not in dict(Venue.TYPE_CHOICES):
                return JsonResponse({'detail': 'venue_type is invalid'}, status=400)
            if key == 'slug' and (not value or Venue.objects.exclude(id=venue.id).filter(slug=value).exists()):
                return JsonResponse({'detail': 'slug is invalid or already exists'}, status=400)
            setattr(venue, field, value)
            changed.append(field)
    if 'active' in data:
        venue.active = _bool_value(data.get('active'))
        changed.append('active')
    if changed:
        venue.save(update_fields=list(dict.fromkeys(changed + ['updated_at'])))

    config, _ = VenueAgentConfig.objects.get_or_create(venue=venue)
    config_fields = {
        'aims_scope': 'text',
        'accepted_article_types': 'list',
        'accepted_methods': 'list',
        'quality_threshold': 'text',
        'policies': 'dict',
        'disclosures': 'list',
        'reporting_standards': 'list',
        'desk_rejection_rules': 'list',
        'deadline_notes': 'text',
        'submission_capacity': 'capacity',
        'current_demand': 'dict',
        'reviewer_criteria': 'dict',
    }
    config_changed = []
    for field, kind in config_fields.items():
        if field not in data:
            continue
        raw = data.get(field)
        if kind == 'text':
            value = str(raw or '').strip()
        elif kind == 'list':
            value = _list_value(raw)
        elif kind == 'dict':
            if not isinstance(raw, dict):
                return JsonResponse({'detail': f'{field} must be an object'}, status=400)
            value = raw
        elif kind == 'capacity':
            if raw in (None, ''):
                value = None
            else:
                try:
                    value = int(raw)
                except (ValueError, TypeError):
                    return JsonResponse({'detail': 'submission_capacity must be an integer'}, status=400)
                if value < 0:
                    return JsonResponse({'detail': 'submission_capacity cannot be negative'}, status=400)
        setattr(config, field, value)
        config_changed.append(field)

    if config_changed:
        config.version += 1
        config.save(update_fields=list(dict.fromkeys(config_changed + ['version', 'updated_at'])))

    return JsonResponse({'venue': _venue_public(venue, include_config=True)})


@csrf_exempt
@require_POST
@require_admin
def admin_venue_feedback(request, venue_id):
    try:
        venue = Venue.objects.get(id=venue_id)
    except Venue.DoesNotExist:
        return JsonResponse({'detail': 'Venue not found'}, status=404)
    data = _json_body(request)
    correction = str(data.get('correction', '')).strip()
    category = str(data.get('category', '')).strip()
    if not category or not correction:
        return JsonResponse({'detail': 'category and correction are required'}, status=400)

    manuscript = None
    manuscript_id = data.get('manuscript_id')
    if manuscript_id:
        try:
            manuscript = Manuscript.objects.get(id=manuscript_id)
        except (Manuscript.DoesNotExist, ValueError):
            return JsonResponse({'detail': 'Manuscript not found'}, status=404)

    assessment = None
    assessment_id = data.get('venue_assessment_id')
    if assessment_id:
        try:
            assessment = VenueAssessment.objects.get(id=assessment_id, venue=venue)
        except (VenueAssessment.DoesNotExist, ValueError):
            return JsonResponse({'detail': 'Venue assessment not found'}, status=404)

    feedback = EditorFeedback.objects.create(
        venue=venue,
        manuscript=manuscript,
        venue_assessment=assessment,
        category=category,
        original_finding=str(data.get('original_finding', '')).strip(),
        correction=correction,
        reason=str(data.get('reason', '')).strip(),
        metadata=data.get('metadata') if isinstance(data.get('metadata'), dict) else {},
    )
    return JsonResponse({
        'feedback': {
            'id': feedback.id,
            'venue_id': str(venue.id),
            'category': feedback.category,
            'correction': feedback.correction,
            'reason': feedback.reason,
            'applied_to_agent': feedback.applied_to_agent,
            'created_at': feedback.created_at.isoformat(),
        }
    }, status=201)


@require_GET
@require_admin
def admin_network_submissions(request):
    qs = VenueSubmission.objects.select_related('manuscript', 'venue').all()
    status = request.GET.get('status', '').strip()
    venue_slug = request.GET.get('venue', '').strip()
    if status in dict(VenueSubmission.STATUS_CHOICES):
        qs = qs.filter(status=status)
    if venue_slug:
        qs = qs.filter(venue__slug=venue_slug)
    limit = min(max(int(request.GET.get('limit', '100') or 100), 1), 200)
    items = []
    for item in qs[:limit]:
        payload = _submission_payload(item)
        payload['manuscript'] = {
            'id': str(item.manuscript_id),
            'title': item.manuscript.title,
            'manuscript_type': item.manuscript.manuscript_type,
            'primary_author_name': item.manuscript.primary_author_name,
        }
        items.append(payload)
    response = JsonResponse({'items': items})
    response['Cache-Control'] = 'no-store'
    return response


@csrf_exempt
@require_POST
@require_admin
def admin_network_submission_decision(request, submission_id):
    try:
        submission = VenueSubmission.objects.select_related('manuscript', 'venue').get(id=submission_id)
    except VenueSubmission.DoesNotExist:
        return JsonResponse({'detail': 'Submission not found'}, status=404)

    data = _json_body(request)
    decision = str(data.get('decision', '')).strip()
    allowed = {'accepted', 'rejected', 'revision_requested'}
    if decision not in allowed:
        return JsonResponse({'detail': 'decision must be accepted, rejected, or revision_requested'}, status=400)
    if submission.status not in {'submitted', 'under_review', 'revision_requested'}:
        return JsonResponse({'detail': 'Submission is not awaiting an editorial decision'}, status=409)

    now = timezone.now()
    submission.status = decision
    submission.decision_note = str(data.get('note', '')).strip()
    submission.decided_at = now if decision in {'accepted', 'rejected'} else None
    submission.save(update_fields=['status', 'decision_note', 'decided_at', 'updated_at'])
    VenueSubmissionEvent.objects.create(
        submission=submission,
        event_type='editorial_decision',
        detail={'decision': decision, 'note': submission.decision_note},
    )
    return JsonResponse({'submission': _submission_payload(submission, include_events=True)})
