"""Author API for the ordered submission plan (submission plan spec, 8 October, section 7).

    POST   /api/author/manuscripts/<id>/plan/generate/      202, queued job (poll /api/author/jobs/<id>/)
    GET    /api/author/manuscripts/<id>/plan/               the current plan with steps
    POST   /api/author/plans/<id>/activate/
    PATCH  /api/author/plans/<id>/reorder/                  {"steps": [step_id, ...]} in the new order
    POST   /api/author/plans/<id>/steps/<id>/skip/          {"note": "..."} required (why)
    POST   /api/author/plans/<id>/steps/<id>/outcome/       {"outcome": accepted|declined|withdrawn, "note": "..."}
    POST   /api/author/manuscripts/<id>/versions/           upload a revision (manuscript versions API)

Beyond the spec's list, needed by the same state machine:
    POST   /api/author/plans/<id>/steps/<id>/submitted/     for a venue outside Flexee (Flexee venues update it)
    POST   /api/author/plans/<id>/steps/<id>/remove/        take a venue out of the plan
    POST   /api/author/plans/<id>/revision/                 after a decline: {"answer": revised|unchanged, "version": n, "note": why (required for unchanged)}
    POST   /api/author/plans/<id>/abandon/                  end the plan
    GET    /api/admin/plans/?manuscript=<id>                read-only, platform admins (support)

Generation goes through the existing job queue and the same author access rules. No new auth path.
"""
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import submission_plan as plans
from .auth import require_platform_superuser
from .author_api import _author_access_error, _json_body, _queue_unique_job
from .models import Manuscript, SubmissionPlan, VenueSubmission

PLAN_JOB = 'submission_plan'
PLAN_TASK = 'review.tasks.run_submission_plan_task'


def _error(exc):
    return JsonResponse({'detail': str(exc), 'code': exc.code}, status=exc.status)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _manuscript_for(request, manuscript_id):
    try:
        manuscript = Manuscript.objects.select_related('current_version').get(id=manuscript_id)
    except Manuscript.DoesNotExist:
        return None, JsonResponse({'detail': 'Manuscript not found'}, status=404)
    access_error = _author_access_error(request, manuscript)
    if access_error:
        return None, access_error
    return manuscript, None


def _plan_for(request, plan_id):
    try:
        plan = SubmissionPlan.objects.select_related('manuscript').get(id=plan_id)
    except (SubmissionPlan.DoesNotExist, ValueError):
        return None, JsonResponse({'detail': 'Plan not found'}, status=404)
    access_error = _author_access_error(request, plan.manuscript)
    if access_error:
        return None, access_error
    return plan, None


def _plan_response(plan, status=200):
    plan.refresh_from_db()
    return JsonResponse({'plan': plans.plan_payload(plan)}, status=status)


def _queue_generation(manuscript):
    job, _created = _queue_unique_job(PLAN_JOB, manuscript.id, PLAN_TASK, manuscript.id)
    return job


@require_GET
def manuscript_plan(request, manuscript_id):
    manuscript, error = _manuscript_for(request, manuscript_id)
    if error:
        return error
    plan = plans.latest_plan(manuscript)
    from .models import ReviewJob
    job = (ReviewJob.objects.filter(job_type=PLAN_JOB, reference_id=str(manuscript.id))
           .order_by('-created_at').first())
    return JsonResponse({
        'plan': plans.plan_payload(plan) if plan else None,
        'current_version': manuscript.current_version.number if manuscript.current_version_id else None,
        'generation': {'job_id': job.id, 'status': job.status,
                       'error': job.error_message if job.status == 'failed' else None} if job else None,
    })


@require_POST
def generate_plan(request, manuscript_id):
    manuscript, error = _manuscript_for(request, manuscript_id)
    if error:
        return error
    try:
        plans.check_can_generate(manuscript)  # tell the author now, not after the job
    except plans.PlanError as exc:
        return _error(exc)
    job = _queue_generation(manuscript)
    return JsonResponse({'job_id': job.id, 'status': job.status}, status=202)


@require_POST
def activate_plan(request, plan_id):
    plan, error = _plan_for(request, plan_id)
    if error:
        return error
    try:
        plans.activate(plan.id)
    except plans.PlanError as exc:
        return _error(exc)
    return _plan_response(plan)


@require_http_methods(['PATCH', 'POST'])
def reorder_plan(request, plan_id):
    plan, error = _plan_for(request, plan_id)
    if error:
        return error
    data = _json_body(request)
    step_ids = data.get('steps') if isinstance(data, dict) else data
    if not isinstance(step_ids, list):
        return JsonResponse({'detail': 'Send {"steps": [step_id, ...]} in the new order.', 'code': 'bad_order'},
                            status=400)
    try:
        plans.reorder(plan.id, step_ids)
    except plans.PlanError as exc:
        return _error(exc)
    return _plan_response(plan)


@require_POST
def step_action(request, plan_id, step_id, action):
    plan, error = _plan_for(request, plan_id)
    if error:
        return error
    if not plan.steps.filter(id=step_id).exists():
        return JsonResponse({'detail': 'Plan step not found'}, status=404)
    data = _json_body(request)
    note = str(data.get('note', ''))[:2000]
    try:
        if action == 'skip':
            plans.skip(plan.id, step_id, note=note)
        elif action == 'remove':
            plans.remove(plan.id, step_id, note=note)
        elif action == 'submitted':
            submission = None
            if data.get('venue_submission_id'):
                submission = VenueSubmission.objects.filter(
                    id=data['venue_submission_id'], manuscript=plan.manuscript,
                    venue__plan_steps__id=step_id).first()
                if submission is None:
                    return JsonResponse({'detail': 'That submission is not this manuscript\'s submission to this venue.'},
                                        status=400)
            plans.mark_submitted(plan.id, step_id, venue_submission=submission)
        elif action == 'outcome':
            plans.report_outcome(plan.id, step_id, str(data.get('outcome', '')), note=note)
        else:
            return JsonResponse({'detail': 'Unknown plan action'}, status=404)
    except plans.PlanError as exc:
        return _error(exc)
    return _plan_response(plan)


@require_POST
def revision_answer(request, plan_id):
    """After a decline. 'revised' regenerates the plan against the new version in the job queue."""
    plan, error = _plan_for(request, plan_id)
    if error:
        return error
    data = _json_body(request)
    try:
        _plan, version = plans.answer_revision(plan.id, answer=str(data.get('answer', '')),
                                               version_number=_int(data.get('version')),
                                               note=str(data.get('note', ''))[:2000])
    except plans.PlanError as exc:
        return _error(exc)
    if version is not None:
        job = _queue_generation(plan.manuscript)
        plan.refresh_from_db()
        return JsonResponse({'plan': plans.plan_payload(plan), 'job_id': job.id, 'status': job.status}, status=202)
    return _plan_response(plan)


@require_POST
def abandon_plan(request, plan_id):
    plan, error = _plan_for(request, plan_id)
    if error:
        return error
    try:
        plans.abandon(plan.id, note=str(_json_body(request).get('note', ''))[:2000])
    except plans.PlanError as exc:
        return _error(exc)
    return _plan_response(plan)


@require_GET
@require_platform_superuser
def admin_plans(request):
    items = SubmissionPlan.objects.select_related('manuscript', 'manuscript_version').order_by('-created_at')
    if request.GET.get('manuscript'):
        import uuid
        try:
            manuscript_id = uuid.UUID(str(request.GET['manuscript']))
        except ValueError:  # audit item 22: a malformed id is the caller's mistake, not a server error
            return JsonResponse({'detail': 'manuscript must be a manuscript id.'}, status=400)
        items = items.filter(manuscript_id=manuscript_id)
    return JsonResponse({'plans': [{**plans.plan_payload(p), 'manuscript_id': str(p.manuscript_id),
                                    'manuscript_title': p.manuscript.title} for p in items[:50]]})
