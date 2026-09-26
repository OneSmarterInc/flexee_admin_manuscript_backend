import json

from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .auth import check_org_access, require_admin
from .models import EditorFeedback, SubmissionRequirementFile, Venue, VenueAgentConfig, VenueSubmission
from .services.email_service import send_acceptance_email, send_rejection_email, _send
from .author_api import _active_config, _json_body, _submission_payload, _venue_config_payload, _venue_payload


ALLOWED_VENUE_TYPES = {value for value, _ in Venue.TYPE_CHOICES}
EDITOR_STATUSES = {
    'submitted',
    'under_review',
    'revision_requested',
    'accepted',
    'rejected',
    'withdrawn',
    'transferred',
}
DECISION_STATUSES = {'revision_requested', 'accepted', 'rejected'}


def _feedback_payload(item):
    return {
        'id': str(item.id),
        'venue_id': str(item.venue_id),
        'venue_submission_id': str(item.venue_submission_id) if item.venue_submission_id else None,
        'assessment_field': item.assessment_field,
        'agent_value': item.agent_value,
        'editor_value': item.editor_value,
        'reason': item.reason,
        'created_at': item.created_at.isoformat(),
    }


def _editor_submission_payload(item, *, detail=False):
    manuscript = item.manuscript
    payload = {
        'id': str(item.id),
        'created_at': item.created_at.isoformat(),
        'updated_at': item.updated_at.isoformat(),
        'submitted_at': item.submitted_at.isoformat() if item.submitted_at else None,
        'status': item.status,
        'venue': _venue_payload(item.venue, include_config=False),
        'venue_config_version': item.venue_config.version if item.venue_config_id else None,
        'manuscript': {
            'id': str(manuscript.id),
            'title': manuscript.title,
            'author_name': manuscript.author_name,
            'author_email': manuscript.author_email,
            'coauthors': manuscript.coauthors,
            'manuscript_type': manuscript.manuscript_type,
            'manuscript_filename': manuscript.manuscript_filename,
            'manuscript_bytes': manuscript.manuscript_bytes,
            'keywords': manuscript.keywords,
            'abstract': manuscript.abstract,
            'disclosure': manuscript.disclosure,
            'notes': manuscript.notes,
            'parsed_profile': manuscript.parsed_profile,
        },
        'brief_summary': str((item.editorial_brief or {}).get('editor_summary', '')).strip(),
        'decision': item.decision or {},
    }
    if detail:
        full = _submission_payload(item)
        payload.update({
            'packet': full['packet'],
            'editorial_brief': full['editorial_brief'],
            'evidence': full['evidence'],
            'venue_config': _venue_config_payload(item.venue_config),
            'feedback': [_feedback_payload(row) for row in item.editor_feedback.all()],
        })
    return payload


@csrf_exempt
@require_admin
def admin_venue_detail(request, venue_id):
    try:
        venue = Venue.objects.select_related('organization').get(id=venue_id)
    except Venue.DoesNotExist:
        return JsonResponse({'detail': 'Venue not found'}, status=404)

    required_roles = ['owner', 'editor', 'viewer'] if request.method == 'GET' else ['owner']
    if not check_org_access(request.editor_user, venue.organization_id, required_roles):
        return JsonResponse({'detail': 'Forbidden'}, status=403)

    if request.method == 'GET':
        return JsonResponse({'venue': _venue_payload(venue)})
    if request.method not in {'PATCH', 'POST'}:
        return JsonResponse({'detail': 'Method not allowed'}, status=405)

    data = _json_body(request)
    if 'name' in data:
        name = str(data.get('name', '')).strip()
        if not name:
            return JsonResponse({'detail': 'name cannot be empty'}, status=400)
        venue.name = name
    if 'venue_type' in data:
        venue_type = str(data.get('venue_type', '')).strip()
        if venue_type not in ALLOWED_VENUE_TYPES:
            return JsonResponse({'detail': 'venue_type must be journal, conference, or publisher'}, status=400)
        venue.venue_type = venue_type
    if 'description' in data:
        venue.description = str(data.get('description', '')).strip()
    if 'active' in data:
        venue.active = bool(data.get('active'))
    venue.save()
    return JsonResponse({'venue': _venue_payload(venue)})


@require_GET
@require_admin
def admin_venue_configs(request, venue_id):
    try:
        venue = Venue.objects.get(id=venue_id)
    except Venue.DoesNotExist:
        return JsonResponse({'detail': 'Venue not found'}, status=404)
    if not check_org_access(request.editor_user, venue.organization_id, ['owner', 'editor', 'viewer']):
        return JsonResponse({'detail': 'Forbidden'}, status=403)

    required_roles = ['owner', 'editor', 'viewer'] if request.method == 'GET' else ['owner']
    if not check_org_access(request.editor_user, venue.organization_id, required_roles):
        return JsonResponse({'detail': 'Forbidden'}, status=403)
    configs = venue.agent_configs.order_by('-version', '-created_at')
    return JsonResponse({
        'venue': _venue_payload(venue, include_config=False),
        'configs': [_venue_config_payload(item) for item in configs],
    })


@csrf_exempt
@require_POST
@require_admin
@transaction.atomic
def admin_activate_venue_config(request, venue_id, config_id):
    try:
        venue = Venue.objects.select_for_update().get(id=venue_id)
        config = VenueAgentConfig.objects.get(id=config_id, venue=venue)
    except Venue.DoesNotExist:
        return JsonResponse({'detail': 'Venue not found'}, status=404)
    except VenueAgentConfig.DoesNotExist:
        return JsonResponse({'detail': 'Venue configuration not found'}, status=404)

    if not check_org_access(request.editor_user, venue.organization_id, ['owner']):
        return JsonResponse({'detail': 'Forbidden'}, status=403)

    venue.agent_configs.filter(active=True).exclude(id=config.id).update(active=False)
    if not config.active:
        config.active = True
        config.save(update_fields=['active'])
    return JsonResponse({
        'venue': _venue_payload(venue, include_config=False),
        'config': _venue_config_payload(config),
    })


@require_GET
@require_admin
def admin_venue_submissions(request):
    queryset = VenueSubmission.objects.select_related(
        'manuscript', 'venue', 'venue__organization', 'venue_config'
    ).all()

    user = request.editor_user
    if not user.platform_superuser:
        org_ids = user.memberships.values_list('organization_id', flat=True)
        queryset = queryset.filter(venue__organization_id__in=org_ids)

    venue_id = str(request.GET.get('venue_id', '')).strip()
    status = str(request.GET.get('status', '')).strip()
    query = str(request.GET.get('q', '')).strip()
    scope = str(request.GET.get('scope', 'editor')).strip()

    if scope == 'editor':
        queryset = queryset.filter(status__in=EDITOR_STATUSES)
    if venue_id:
        queryset = queryset.filter(venue_id=venue_id)
    if status:
        queryset = queryset.filter(status=status)
    if query:
        queryset = queryset.filter(
            Q(manuscript__title__icontains=query)
            | Q(manuscript__author_name__icontains=query)
            | Q(manuscript__author_email__icontains=query)
            | Q(venue__name__icontains=query)
        )

    counts_base = VenueSubmission.objects.all()
    if not user.platform_superuser:
        counts_base = counts_base.filter(venue__organization_id__in=org_ids)
    if venue_id:
        counts_base = counts_base.filter(venue_id=venue_id)
    counts = {
        'total': counts_base.filter(status__in=EDITOR_STATUSES).count(),
        'submitted': counts_base.filter(status='submitted').count(),
        'under_review': counts_base.filter(status='under_review').count(),
        'revision_requested': counts_base.filter(status='revision_requested').count(),
        'accepted': counts_base.filter(status='accepted').count(),
        'rejected': counts_base.filter(status='rejected').count(),
    }

    items = queryset.order_by('-submitted_at', '-created_at')[:500]
    return JsonResponse({
        'counts': counts,
        'items': [_editor_submission_payload(item) for item in items],
    })


@require_GET
@require_admin
def admin_venue_submission_detail(request, submission_id):
    try:
        item = VenueSubmission.objects.select_related(
            'manuscript', 'venue', 'venue__organization', 'venue_config'
        ).prefetch_related('evidence_findings', 'editor_feedback', 'requirement_files').get(id=submission_id)
    except VenueSubmission.DoesNotExist:
        return JsonResponse({'detail': 'Venue submission not found'}, status=404)
    if not check_org_access(request.editor_user, item.venue.organization_id, ['owner', 'editor', 'viewer']):
        return JsonResponse({'detail': 'Forbidden'}, status=403)
    return JsonResponse({'submission': _editor_submission_payload(item, detail=True)})


@csrf_exempt
@require_POST
@require_admin
def admin_start_venue_review(request, submission_id):
    try:
        item = VenueSubmission.objects.select_related('venue').get(id=submission_id)
    except VenueSubmission.DoesNotExist:
        return JsonResponse({'detail': 'Venue submission not found'}, status=404)

    if not check_org_access(request.editor_user, item.venue.organization_id, ['owner', 'editor']):
        return JsonResponse({'detail': 'Forbidden'}, status=403)

    if item.status not in {'submitted', 'revision_requested', 'under_review'}:
        return JsonResponse(
            {'detail': f'Cannot start editorial review from status {item.status}'},
            status=409,
        )
    if item.status != 'under_review':
        item.status = 'under_review'
        item.save(update_fields=['status', 'updated_at'])
    return JsonResponse({'submission': _editor_submission_payload(item)})


@csrf_exempt
@require_POST
@require_admin
def admin_venue_submission_decision(request, submission_id):
    try:
        item = VenueSubmission.objects.select_related('manuscript', 'venue').get(id=submission_id)
    except VenueSubmission.DoesNotExist:
        return JsonResponse({'detail': 'Venue submission not found'}, status=404)

    if not check_org_access(request.editor_user, item.venue.organization_id, ['owner', 'editor']):
        return JsonResponse({'detail': 'Forbidden'}, status=403)

    data = _json_body(request)
    decision = str(data.get('decision', '')).strip()
    note = str(data.get('note', '')).strip()
    if decision not in DECISION_STATUSES:
        return JsonResponse(
            {'detail': 'decision must be accepted, rejected, or revision_requested'},
            status=400,
        )
    if decision in {'rejected', 'revision_requested'} and not note:
        return JsonResponse({'detail': 'A note is required for rejection or revision request'}, status=400)
    if item.status not in {'submitted', 'under_review', 'revision_requested'}:
        return JsonResponse(
            {'detail': f'Cannot record an editorial decision from status {item.status}'},
            status=409,
        )

    decided_at = timezone.now()
    item.status = decision
    item.decision = {
        'decision': decision,
        'note': note,
        'decided_at': decided_at.isoformat(),
        'decided_by': request.flexee_admin.get('u', 'admin'),
        'human_decision': True,
    }
    item.save(update_fields=['status', 'decision', 'updated_at'])

    # ── Email notification to author ──────────────────────────────────────────
    # Build a lightweight adapter so we can reuse the existing email service
    # functions which expect an object with .author_name, .author_email, .title.
    manuscript = item.manuscript
    author_email = (manuscript.author_email or '').strip()
    if author_email:
        try:
            class _VenueSubmissionAdapter:
                """Bridges VenueSubmission → email_service interface."""
                author_name = manuscript.author_name
                author_email = manuscript.author_email
                title = manuscript.title

            adapter = _VenueSubmissionAdapter()
            venue_name = item.venue.name if item.venue_id else 'the journal'

            if decision == 'accepted':
                send_acceptance_email(adapter, note)
            elif decision == 'rejected':
                send_rejection_email(adapter, note or 'No additional reason was provided.')
            else:  # revision_requested
                subject = f'Revision requested for your submission to {venue_name}'
                body = (
                    f'Dear {manuscript.author_name},\n\n'
                    f'The editorial team at {venue_name} has reviewed your manuscript '
                    f'"{manuscript.title}" and is requesting revisions before a final decision can be made.\n\n'
                    f'Editor note:\n{note}\n\n'
                    f'Please address the comments above and resubmit your manuscript.\n\n'
                    f'Best regards,\nFlexee Editorial Team'
                )
                _send(author_email, subject, body)
        except Exception:
            # Email errors must never block the decision from being recorded.
            pass

    return JsonResponse({'submission': _editor_submission_payload(item)})


@require_GET
@require_admin
def admin_submission_requirement_download(request, submission_id, requirement_key):
    try:
        item = VenueSubmission.objects.select_related('venue').get(id=submission_id)
    except VenueSubmission.DoesNotExist:
        return JsonResponse({'detail': 'Venue submission not found'}, status=404)

    if not check_org_access(request.editor_user, item.venue.organization_id, ['owner', 'editor', 'viewer']):
        return JsonResponse({'detail': 'Forbidden'}, status=403)

    try:
        row = SubmissionRequirementFile.objects.get(
            venue_submission=item,
            requirement_key=requirement_key,
        )
    except SubmissionRequirementFile.DoesNotExist:
        return JsonResponse({'detail': 'Requirement file not found'}, status=404)

    try:
        row.file.open('rb')
        return FileResponse(
            row.file,
            as_attachment=True,
            filename=row.original_filename,
        )
    except (FileNotFoundError, OSError):
        return JsonResponse({'detail': 'Requirement file is unavailable'}, status=404)


@require_GET
@require_admin
def admin_venue_submission_download(request, submission_id):
    try:
        item = VenueSubmission.objects.select_related('manuscript', 'venue').get(id=submission_id)
    except VenueSubmission.DoesNotExist:
        return JsonResponse({'detail': 'Venue submission not found'}, status=404)

    if not check_org_access(request.editor_user, item.venue.organization_id, ['owner', 'editor', 'viewer']):
        return JsonResponse({'detail': 'Forbidden'}, status=403)

    manuscript = item.manuscript
    try:
        manuscript.manuscript_file.open('rb')
        response = FileResponse(
            manuscript.manuscript_file,
            as_attachment=True,
            filename=manuscript.manuscript_filename,
        )
        return response
    except (FileNotFoundError, OSError):
        return JsonResponse({'detail': 'Manuscript file is unavailable'}, status=404)
