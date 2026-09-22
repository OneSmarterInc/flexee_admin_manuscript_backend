import hashlib
import json
import os
import re
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .auth import require_admin
from .models import (
    EditorFeedback,
    EvidenceFinding,
    Manuscript,
    Organization,
    ReadinessAssessment,
    SubmissionTransfer,
    Venue,
    VenueAgentConfig,
    VenueMatch,
    VenueSubmission,
)
from .services.review_engine import extract_text, word_count


ALLOWED_MANUSCRIPT_TYPES = {value for value, _ in Manuscript.TYPE_CHOICES}
ALLOWED_VENUE_TYPES = {value for value, _ in Venue.TYPE_CHOICES}


def _json_body(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _json_list(value):
    if isinstance(value, list):
        return value
    if value in (None, ''):
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass
        return [item.strip() for item in stripped.split(',') if item.strip()]
    return []


def _clean_keywords(value):
    return [str(item).strip() for item in _json_list(value) if str(item).strip()][:50]


def _manuscript_payload(item):
    latest_readiness = item.readiness_assessments.first()
    return {
        'id': str(item.id),
        'created_at': item.created_at.isoformat(),
        'updated_at': item.updated_at.isoformat(),
        'author_name': item.author_name,
        'author_email': item.author_email,
        'coauthors': item.coauthors,
        'title': item.title,
        'manuscript_type': item.manuscript_type,
        'abstract': item.abstract,
        'keywords': item.keywords,
        'disclosure': item.disclosure,
        'notes': item.notes,
        'manuscript_filename': item.manuscript_filename,
        'manuscript_bytes': item.manuscript_bytes,
        'manuscript_sha256': item.manuscript_sha256,
        'parsed_profile': item.parsed_profile,
        'latest_readiness': _readiness_payload(latest_readiness) if latest_readiness else None,
    }


def _venue_config_payload(config):
    if not config:
        return None
    return {
        'id': config.id,
        'version': config.version,
        'created_at': config.created_at.isoformat(),
        'effective_at': config.effective_at.isoformat(),
        'active': config.active,
        'aims_scope': config.aims_scope,
        'article_types': config.article_types,
        'accepted_methods': config.accepted_methods,
        'quality_threshold': config.quality_threshold,
        'reviewer_criteria': config.reviewer_criteria,
        'policies': config.policies,
        'disclosures': config.disclosures,
        'reporting_standards': config.reporting_standards,
        'desk_rejection_rules': config.desk_rejection_rules,
        'deadlines': config.deadlines,
        'submission_capacity': config.submission_capacity,
        'current_demand': config.current_demand,
        'config_notes': config.config_notes,
    }


def _active_config(venue):
    return venue.agent_configs.filter(active=True).order_by('-version', '-created_at').first()


def _venue_payload(venue, include_config=True):
    payload = {
        'id': str(venue.id),
        'name': venue.name,
        'slug': venue.slug,
        'venue_type': venue.venue_type,
        'description': venue.description,
        'active': venue.active,
        'organization': {
            'id': str(venue.organization_id),
            'name': venue.organization.name,
            'organization_type': venue.organization.organization_type,
        } if venue.organization_id else None,
    }
    if include_config:
        payload['config'] = _venue_config_payload(_active_config(venue))
    return payload


def _readiness_payload(item):
    return {
        'id': str(item.id),
        'manuscript_id': str(item.manuscript_id),
        'created_at': item.created_at.isoformat(),
        'completed_at': item.completed_at.isoformat() if item.completed_at else None,
        'status': item.status,
        'engine_version': item.engine_version,
        'summary': item.summary,
        'findings': item.findings,
        'error': item.error,
    }


def _match_payload(item):
    return {
        'id': str(item.id),
        'manuscript_id': str(item.manuscript_id),
        'venue': _venue_payload(item.venue, include_config=False),
        'venue_config_version': item.venue_config.version if item.venue_config_id else None,
        'eligibility': item.eligibility,
        'fit_summary': item.fit_summary,
        'reasons': item.reasons,
        'gaps': item.gaps,
        'evidence': item.evidence,
        'created_at': item.created_at.isoformat(),
    }


def _submission_payload(item):
    return {
        'id': str(item.id),
        'manuscript_id': str(item.manuscript_id),
        'venue': _venue_payload(item.venue, include_config=False),
        'venue_config_version': item.venue_config.version if item.venue_config_id else None,
        'created_at': item.created_at.isoformat(),
        'updated_at': item.updated_at.isoformat(),
        'submitted_at': item.submitted_at.isoformat() if item.submitted_at else None,
        'status': item.status,
        'packet': item.packet,
        'editorial_brief': item.editorial_brief,
        'decision': item.decision,
        'evidence': [
            {
                'id': str(e.id),
                'finding_type': e.finding_type,
                'claim': e.claim,
                'source_type': e.source_type,
                'source_locator': e.source_locator,
                'source_url': e.source_url,
                'excerpt': e.excerpt,
                'verification': e.verification,
            }
            for e in item.evidence_findings.all()
        ],
    }


@csrf_exempt
@require_POST
def author_manuscripts(request):
    max_bytes = int(os.getenv('MAX_MANUSCRIPT_BYTES', str(20 * 1024 * 1024)))
    upload = request.FILES.get('manuscript')
    title = request.POST.get('title', '').strip()
    author_name = request.POST.get('author', request.POST.get('author_name', '')).strip()
    author_email = request.POST.get('email', request.POST.get('author_email', '')).strip()
    manuscript_type = request.POST.get('manuscript_type', request.POST.get('type', 'other')).strip()
    disclosure = request.POST.get('disclosure', '').strip()
    attestation = request.POST.get('attestation', '').strip().lower()

    errors = []
    if not title:
        errors.append('title is required')
    if not author_name:
        errors.append('author is required')
    if manuscript_type not in ALLOWED_MANUSCRIPT_TYPES:
        errors.append('unsupported manuscript_type')
    if not disclosure:
        errors.append('disclosure is required')
    if attestation not in {'true', '1', 'yes', 'on', 'human-authored-with-ai-assistance'}:
        errors.append('authorship attestation is required')
    if not upload:
        errors.append('manuscript is required')
    elif upload.size <= 0:
        errors.append('manuscript is empty')
    elif upload.size > max_bytes:
        errors.append(f'manuscript exceeds the {max_bytes // 1024 // 1024} MB upload limit')
    elif not upload.name.lower().endswith(('.docx', '.pdf', '.md', '.zip')):
        errors.append('manuscript must be a .docx, .pdf, .md, or .zip file')

    if errors:
        return JsonResponse({'detail': errors[0], 'errors': errors}, status=400)

    content = upload.read()
    upload.seek(0)
    digest = hashlib.sha256(content).hexdigest()

    item = Manuscript.objects.create(
        author_name=author_name,
        author_email=author_email,
        coauthors=request.POST.get('coauthors', '').strip(),
        title=title,
        manuscript_type=manuscript_type,
        abstract=request.POST.get('abstract', '').strip(),
        keywords=_clean_keywords(request.POST.get('keywords', '')),
        disclosure=disclosure,
        notes=request.POST.get('notes', '').strip(),
        attestation=True,
        manuscript_filename=upload.name,
        manuscript_file=upload,
        manuscript_bytes=len(content),
        manuscript_sha256=digest,
    )
    return JsonResponse({'manuscript': _manuscript_payload(item)}, status=201)


@require_GET
def author_manuscript_detail(request, manuscript_id):
    try:
        item = Manuscript.objects.get(id=manuscript_id)
    except Manuscript.DoesNotExist:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)
    return JsonResponse({'manuscript': _manuscript_payload(item)})


@csrf_exempt
@require_POST
def author_run_readiness(request, manuscript_id):
    try:
        manuscript = Manuscript.objects.get(id=manuscript_id)
    except Manuscript.DoesNotExist:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)

    assessment = ReadinessAssessment.objects.create(
        manuscript=manuscript,
        status='pending',
        engine_version='mechanical-v1',
    )

    try:
        manuscript.manuscript_file.open('rb')
        content = manuscript.manuscript_file.read()
        manuscript.manuscript_file.close()
        text = extract_text(content, manuscript.manuscript_filename)
        words = word_count(text)
        lowered = text.lower()

        findings = [
            {
                'code': 'file_readable',
                'label': 'Manuscript file',
                'status': 'pass' if text.strip() else 'fail',
                'detail': 'Readable manuscript text was extracted.' if text.strip() else 'No extractable manuscript text was found.',
                'source': {'type': 'manuscript', 'locator': manuscript.manuscript_filename},
            },
            {
                'code': 'abstract',
                'label': 'Abstract',
                'status': 'pass' if manuscript.abstract.strip() or re.search(r'(?im)^\s*abstract\b', text) else 'warning',
                'detail': 'Abstract information is present.' if manuscript.abstract.strip() or re.search(r'(?im)^\s*abstract\b', text) else 'No clear abstract was detected.',
                'source': {'type': 'manuscript', 'locator': 'metadata or manuscript heading'},
            },
            {
                'code': 'ai_disclosure',
                'label': 'AI-use disclosure',
                'status': 'pass' if manuscript.disclosure.strip() else 'fail',
                'detail': 'AI-use disclosure is present.' if manuscript.disclosure.strip() else 'AI-use disclosure is missing.',
                'source': {'type': 'manuscript', 'locator': 'submission metadata'},
            },
            {
                'code': 'references',
                'label': 'References',
                'status': 'pass' if re.search(r'(?im)^\s*(references|bibliography|works cited)\s*$', text) else 'warning',
                'detail': 'A reference section heading was detected.' if re.search(r'(?im)^\s*(references|bibliography|works cited)\s*$', text) else 'No clear reference section heading was detected.',
                'source': {'type': 'manuscript', 'locator': 'reference section'},
            },
            {
                'code': 'data_availability',
                'label': 'Data availability statement',
                'status': 'pass' if 'data availability' in lowered else 'warning',
                'detail': 'A data availability statement was detected.' if 'data availability' in lowered else 'No explicit data availability statement was detected; whether one is required depends on the selected venue.',
                'source': {'type': 'manuscript', 'locator': 'full text'},
            },
        ]

        blocking = sum(1 for finding in findings if finding['status'] == 'fail')
        warnings = sum(1 for finding in findings if finding['status'] == 'warning')
        summary = {
            'word_count': words,
            'blocking_issues': blocking,
            'warnings': warnings,
            'ready_for_matching': blocking == 0,
            'note': 'This assessment contains deterministic/mechanical checks only. Semantic quality and venue-specific checks are handled separately.',
        }
        profile = {
            **(manuscript.parsed_profile or {}),
            'word_count': words,
            'has_abstract': findings[1]['status'] == 'pass',
            'has_references_section': findings[3]['status'] == 'pass',
            'has_data_availability_statement': findings[4]['status'] == 'pass',
        }
        manuscript.parsed_profile = profile
        manuscript.save(update_fields=['parsed_profile', 'updated_at'])

        assessment.status = 'completed'
        assessment.completed_at = timezone.now()
        assessment.summary = summary
        assessment.findings = findings
        assessment.save(update_fields=['status', 'completed_at', 'summary', 'findings'])
    except Exception as exc:
        assessment.status = 'failed'
        assessment.completed_at = timezone.now()
        assessment.error = {'detail': str(exc)}
        assessment.save(update_fields=['status', 'completed_at', 'error'])
        return JsonResponse({'readiness': _readiness_payload(assessment)}, status=422)

    return JsonResponse({'readiness': _readiness_payload(assessment)}, status=201)


@require_GET
def author_readiness(request, manuscript_id):
    try:
        manuscript = Manuscript.objects.get(id=manuscript_id)
    except Manuscript.DoesNotExist:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)
    item = manuscript.readiness_assessments.first()
    if not item:
        return JsonResponse({'detail': 'No readiness assessment exists for this manuscript'}, status=404)
    return JsonResponse({'readiness': _readiness_payload(item)})


@require_GET
def public_venues(request):
    items = Venue.objects.filter(active=True).select_related('organization')
    return JsonResponse({'venues': [_venue_payload(item) for item in items]})


def _normalise_label(value):
    return re.sub(r'[^a-z0-9]+', '_', str(value or '').strip().lower()).strip('_')


@csrf_exempt
@require_POST
def author_generate_matches(request, manuscript_id):
    try:
        manuscript = Manuscript.objects.get(id=manuscript_id)
    except Manuscript.DoesNotExist:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)

    latest_readiness = manuscript.readiness_assessments.first()
    if not latest_readiness or latest_readiness.status != 'completed':
        return JsonResponse({'detail': 'Complete a readiness assessment before generating venue matches'}, status=409)
    if not latest_readiness.summary.get('ready_for_matching', False):
        return JsonResponse({'detail': 'Resolve blocking readiness issues before generating venue matches'}, status=409)

    generated = []
    for venue in Venue.objects.filter(active=True).select_related('organization'):
        config = _active_config(venue)
        reasons = []
        gaps = []
        evidence = []
        eligibility = 'needs_changes'

        if not config:
            gaps.append('This venue does not yet have an active venue-agent configuration.')
        else:
            accepted = {_normalise_label(item) for item in config.article_types}
            manuscript_type = _normalise_label(manuscript.manuscript_type)
            if accepted:
                if manuscript_type in accepted:
                    reasons.append('The manuscript type is accepted by this venue.')
                    evidence.append({
                        'source_type': 'venue_policy',
                        'source_locator': f'venue config v{config.version} · article_types',
                        'claim': 'Manuscript type is accepted.',
                    })
                    eligibility = 'eligible'
                else:
                    gaps.append('The manuscript type is not listed among this venue’s accepted article types.')
                    evidence.append({
                        'source_type': 'venue_policy',
                        'source_locator': f'venue config v{config.version} · article_types',
                        'claim': 'Manuscript type requires editorial review before routing.',
                    })
                    eligibility = 'needs_changes'
            else:
                gaps.append('Accepted article types are not configured for this venue.')

            if config.aims_scope:
                reasons.append('Aims and scope are configured and ready for semantic fit analysis.')
            else:
                gaps.append('Aims and scope are not yet configured.')

        fit_summary = (
            'Passes the currently configured deterministic routing checks. Semantic scope fit still requires the matching agent.'
            if eligibility == 'eligible'
            else 'Requires configuration review or manuscript changes before semantic matching.'
        )

        match, _ = VenueMatch.objects.update_or_create(
            manuscript=manuscript,
            venue=venue,
            defaults={
                'venue_config': config,
                'eligibility': eligibility,
                'fit_summary': fit_summary,
                'reasons': reasons,
                'gaps': gaps,
                'evidence': evidence,
            },
        )
        generated.append(_match_payload(match))

    return JsonResponse({
        'matches': generated,
        'matching_stage': 'deterministic_policy_gate_v1',
        'note': 'No semantic fit ranking is performed by this endpoint. The AI matching agent will extend these persisted records.',
    }, status=201)


@require_GET
def author_matches(request, manuscript_id):
    try:
        manuscript = Manuscript.objects.get(id=manuscript_id)
    except Manuscript.DoesNotExist:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)
    items = manuscript.venue_matches.select_related('venue', 'venue__organization', 'venue_config').all()
    return JsonResponse({'matches': [_match_payload(item) for item in items]})


@csrf_exempt
@require_POST
def author_create_submission(request, manuscript_id):
    try:
        manuscript = Manuscript.objects.get(id=manuscript_id)
    except Manuscript.DoesNotExist:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)

    data = _json_body(request)
    venue_id = data.get('venue_id')
    if not venue_id:
        return JsonResponse({'detail': 'venue_id is required'}, status=400)
    try:
        venue = Venue.objects.get(id=venue_id, active=True)
    except (Venue.DoesNotExist, ValueError):
        return JsonResponse({'detail': 'Active venue not found'}, status=404)

    config = _active_config(venue)
    item = VenueSubmission.objects.create(
        manuscript=manuscript,
        venue=venue,
        venue_config=config,
        status='draft',
        packet={
            'manuscript_filename': manuscript.manuscript_filename,
            'author_name': manuscript.author_name,
            'author_email': manuscript.author_email,
            'coauthors': manuscript.coauthors,
            'title': manuscript.title,
            'manuscript_type': manuscript.manuscript_type,
            'disclosure': manuscript.disclosure,
        },
    )
    return JsonResponse({'submission': _submission_payload(item)}, status=201)


@require_GET
def author_submission_detail(request, submission_id):
    try:
        item = VenueSubmission.objects.select_related(
            'venue', 'venue__organization', 'venue_config'
        ).prefetch_related('evidence_findings').get(id=submission_id)
    except VenueSubmission.DoesNotExist:
        return JsonResponse({'detail': 'Venue submission not found'}, status=404)
    return JsonResponse({'submission': _submission_payload(item)})


@csrf_exempt
@require_POST
def author_submit_packet(request, submission_id):
    try:
        item = VenueSubmission.objects.get(id=submission_id)
    except VenueSubmission.DoesNotExist:
        return JsonResponse({'detail': 'Venue submission not found'}, status=404)

    if item.status not in {'draft', 'packet_ready'}:
        return JsonResponse({'detail': f'Submission cannot be submitted from status {item.status}'}, status=409)

    item.status = 'submitted'
    item.submitted_at = timezone.now()
    item.save(update_fields=['status', 'submitted_at', 'updated_at'])
    return JsonResponse({'submission': _submission_payload(item)})


@csrf_exempt
@require_POST
@transaction.atomic
def author_transfer_submission(request, submission_id):
    try:
        source = VenueSubmission.objects.select_for_update().get(id=submission_id)
    except VenueSubmission.DoesNotExist:
        return JsonResponse({'detail': 'Venue submission not found'}, status=404)

    data = _json_body(request)
    venue_id = data.get('venue_id')
    if not venue_id:
        return JsonResponse({'detail': 'venue_id is required'}, status=400)
    try:
        target_venue = Venue.objects.get(id=venue_id, active=True)
    except (Venue.DoesNotExist, ValueError):
        return JsonResponse({'detail': 'Active venue not found'}, status=404)
    if target_venue.id == source.venue_id:
        return JsonResponse({'detail': 'Transfer destination must be a different venue'}, status=400)

    target = VenueSubmission.objects.create(
        manuscript=source.manuscript,
        venue=target_venue,
        venue_config=_active_config(target_venue),
        status='draft',
        packet={
            'manuscript_filename': source.manuscript.manuscript_filename,
            'author_name': source.manuscript.author_name,
            'author_email': source.manuscript.author_email,
            'coauthors': source.manuscript.coauthors,
            'title': source.manuscript.title,
            'manuscript_type': source.manuscript.manuscript_type,
            'disclosure': source.manuscript.disclosure,
            'transferred_from_submission_id': str(source.id),
        },
    )
    SubmissionTransfer.objects.create(
        manuscript=source.manuscript,
        from_submission=source,
        to_submission=target,
        reason=str(data.get('reason', '')).strip(),
    )
    source.status = 'transferred'
    source.save(update_fields=['status', 'updated_at'])
    return JsonResponse({'submission': _submission_payload(target)}, status=201)


@csrf_exempt
@require_admin
def admin_venues(request):
    if request.method == 'GET':
        items = Venue.objects.select_related('organization').all()
        return JsonResponse({'venues': [_venue_payload(item) for item in items]})
    if request.method != 'POST':
        return JsonResponse({'detail': 'Method not allowed'}, status=405)

    data = _json_body(request)
    name = str(data.get('name', '')).strip()
    venue_type = str(data.get('venue_type', '')).strip()
    if not name:
        return JsonResponse({'detail': 'name is required'}, status=400)
    if venue_type not in ALLOWED_VENUE_TYPES:
        return JsonResponse({'detail': 'venue_type must be journal, conference, or publisher'}, status=400)

    organization = None
    organization_id = data.get('organization_id')
    if organization_id:
        try:
            organization = Organization.objects.get(id=organization_id)
        except (Organization.DoesNotExist, ValueError):
            return JsonResponse({'detail': 'Organization not found'}, status=404)
    elif str(data.get('organization_name', '')).strip():
        organization = Organization.objects.create(
            name=str(data.get('organization_name')).strip(),
            organization_type=str(data.get('organization_type', 'other')).strip() or 'other',
        )

    base_slug = slugify(str(data.get('slug', '')).strip() or name)[:160] or 'venue'
    candidate = base_slug
    suffix = 2
    while Venue.objects.filter(slug=candidate).exists():
        candidate = f'{base_slug[:150]}-{suffix}'
        suffix += 1

    venue = Venue.objects.create(
        organization=organization,
        name=name,
        slug=candidate,
        venue_type=venue_type,
        description=str(data.get('description', '')).strip(),
        active=bool(data.get('active', True)),
    )
    return JsonResponse({'venue': _venue_payload(venue)}, status=201)


@csrf_exempt
@require_admin
def admin_venue_config(request, venue_id):
    try:
        venue = Venue.objects.get(id=venue_id)
    except Venue.DoesNotExist:
        return JsonResponse({'detail': 'Venue not found'}, status=404)

    if request.method == 'GET':
        config = _active_config(venue)
        if not config:
            return JsonResponse({'detail': 'No venue agent configuration exists'}, status=404)
        return JsonResponse({'venue': _venue_payload(venue, include_config=False), 'config': _venue_config_payload(config)})
    if request.method != 'POST':
        return JsonResponse({'detail': 'Method not allowed'}, status=405)

    data = _json_body(request)
    current = venue.agent_configs.order_by('-version').first()
    next_version = (current.version + 1) if current else 1

    venue.agent_configs.filter(active=True).update(active=False)
    config = VenueAgentConfig.objects.create(
        venue=venue,
        version=next_version,
        active=True,
        aims_scope=str(data.get('aims_scope', '')).strip(),
        article_types=_json_list(data.get('article_types')),
        accepted_methods=_json_list(data.get('accepted_methods')),
        quality_threshold=str(data.get('quality_threshold', '')).strip(),
        reviewer_criteria=_json_list(data.get('reviewer_criteria')),
        policies=data.get('policies') if isinstance(data.get('policies'), dict) else {},
        disclosures=_json_list(data.get('disclosures')),
        reporting_standards=_json_list(data.get('reporting_standards')),
        desk_rejection_rules=_json_list(data.get('desk_rejection_rules')),
        deadlines=data.get('deadlines') if isinstance(data.get('deadlines'), dict) else {},
        submission_capacity=data.get('submission_capacity') if isinstance(data.get('submission_capacity'), dict) else {},
        current_demand=data.get('current_demand') if isinstance(data.get('current_demand'), dict) else {},
        config_notes=str(data.get('config_notes', '')).strip(),
    )
    return JsonResponse({
        'venue': _venue_payload(venue, include_config=False),
        'config': _venue_config_payload(config),
    }, status=201)


@csrf_exempt
@require_POST
@require_admin
def admin_editor_feedback(request, venue_id):
    try:
        venue = Venue.objects.get(id=venue_id)
    except Venue.DoesNotExist:
        return JsonResponse({'detail': 'Venue not found'}, status=404)

    data = _json_body(request)
    field = str(data.get('assessment_field', '')).strip()
    if not field:
        return JsonResponse({'detail': 'assessment_field is required'}, status=400)

    submission = None
    submission_id = data.get('venue_submission_id')
    if submission_id:
        try:
            submission = VenueSubmission.objects.get(id=submission_id, venue=venue)
        except (VenueSubmission.DoesNotExist, ValueError):
            return JsonResponse({'detail': 'Venue submission not found for this venue'}, status=404)

    feedback = EditorFeedback.objects.create(
        venue=venue,
        venue_submission=submission,
        assessment_field=field,
        agent_value=data.get('agent_value'),
        editor_value=data.get('editor_value'),
        reason=str(data.get('reason', '')).strip(),
    )
    return JsonResponse({
        'feedback': {
            'id': str(feedback.id),
            'venue_id': str(feedback.venue_id),
            'venue_submission_id': str(feedback.venue_submission_id) if feedback.venue_submission_id else None,
            'assessment_field': feedback.assessment_field,
            'agent_value': feedback.agent_value,
            'editor_value': feedback.editor_value,
            'reason': feedback.reason,
            'created_at': feedback.created_at.isoformat(),
        }
    }, status=201)
