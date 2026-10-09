"""Author API for the ordered submission plan (8 October instructions, 2.5).

    GET  /api/author/manuscripts/<id>/plan/                 the latest plan (active or finished)
    POST /api/author/manuscripts/<id>/plan/                 build one for the current version ({"replace": true} to rebuild)
    POST /api/author/plans/<plan>/positions/<pos>/<action>/ start | submitted | under-review | outcome |
                                                            resubmit | answer | skip
    POST /api/author/plans/<plan>/stop/                     end the plan
    GET  /api/admin/plans/?manuscript=<id>                  read-only, platform admins (support)
"""
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import submission_plan as plans
from .auth import require_platform_superuser
from .author_api import _author_access_error, _json_body
from .models import Manuscript, SubmissionPlan, VenueSubmission


def _error(exc):
    return JsonResponse({'detail': str(exc), 'code': exc.code}, status=exc.status)


@require_http_methods(['GET', 'POST'])
def manuscript_plan(request, manuscript_id):
    try:
        manuscript = Manuscript.objects.select_related('current_version').get(id=manuscript_id)
    except Manuscript.DoesNotExist:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)
    access_error = _author_access_error(request, manuscript)
    if access_error:
        return access_error
    if request.method == 'GET':
        plan = plans.active_plan(manuscript)
        return JsonResponse({'plan': plans.plan_payload(plan) if plan else None,
                             'current_version': manuscript.current_version.number if manuscript.current_version_id else None})
    try:
        plan = plans.build_plan(manuscript, replace=bool(_json_body(request).get('replace')))
    except plans.PlanError as exc:
        return _error(exc)
    return JsonResponse({'plan': plans.plan_payload(plan)}, status=201)


def _plan_for(request, plan_id):
    try:
        plan = SubmissionPlan.objects.select_related('manuscript').get(id=plan_id)
    except (SubmissionPlan.DoesNotExist, ValueError):
        return None, JsonResponse({'detail': 'Plan not found'}, status=404)
    access_error = _author_access_error(request, plan.manuscript)
    if access_error:
        return None, access_error
    return plan, None


@require_POST
def position_action(request, plan_id, position_id, action):
    plan, error = _plan_for(request, plan_id)
    if error:
        return error
    if not plan.positions.filter(id=position_id).exists():
        return JsonResponse({'detail': 'Plan position not found'}, status=404)
    data = _json_body(request)
    try:
        if action == 'start':
            plans.start(plan.id, position_id)
        elif action == 'submitted':
            submission = None
            if data.get('venue_submission_id'):
                submission = VenueSubmission.objects.filter(
                    id=data['venue_submission_id'], manuscript=plan.manuscript,
                    venue__plan_positions__id=position_id).first()
                if submission is None:
                    return JsonResponse({'detail': 'That submission is not this manuscript\'s submission to this venue.'},
                                        status=400)
            plans.mark_submitted(plan.id, position_id, venue_submission=submission)
        elif action == 'under-review':
            plans.mark_under_review(plan.id, position_id)
        elif action == 'outcome':
            plans.report_outcome(plan.id, position_id, str(data.get('outcome', '')), note=str(data.get('note', ''))[:2000])
        elif action == 'resubmit':
            plans.resubmit(plan.id, position_id, version_number=_int(data.get('version')))
        elif action == 'answer':
            plans.answer_decline(plan.id, position_id, answer=str(data.get('answer', '')),
                                 version_number=_int(data.get('version')), reason=str(data.get('reason', '')),
                                 confirm=data.get('confirm') is True)
        elif action == 'skip':
            plans.skip(plan.id, position_id, reason=str(data.get('reason', '')))
        else:
            return JsonResponse({'detail': 'Unknown plan action'}, status=404)
    except plans.PlanError as exc:
        return _error(exc)
    plan.refresh_from_db()
    return JsonResponse({'plan': plans.plan_payload(plan)})


@require_POST
def stop_plan(request, plan_id):
    plan, error = _plan_for(request, plan_id)
    if error:
        return error
    try:
        plans.stop(plan.id, note=str(_json_body(request).get('note', ''))[:2000])
    except plans.PlanError as exc:
        return _error(exc)
    plan.refresh_from_db()
    return JsonResponse({'plan': plans.plan_payload(plan)})


@require_GET
@require_platform_superuser
def admin_plans(request):
    items = SubmissionPlan.objects.select_related('manuscript', 'built_on_version').order_by('-created_at')
    if request.GET.get('manuscript'):
        items = items.filter(manuscript_id=request.GET['manuscript'])
    return JsonResponse({'plans': [{**plans.plan_payload(p), 'manuscript_id': str(p.manuscript_id),
                                    'manuscript_title': p.manuscript.title} for p in items[:50]]})


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
