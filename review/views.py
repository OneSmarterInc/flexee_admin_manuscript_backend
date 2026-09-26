import hashlib
import json
import os
import re
import urllib.parse
import uuid
import zipfile
import concurrent.futures
from datetime import timedelta
from io import BytesIO
from django.db.models import Q, Count
from django.http import JsonResponse, FileResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from .auth import (
    clear_session_cookie, issue_session, read_session, remote_hash, require_admin,
    set_session_cookie, verify_password, verify_totp, require_platform_superuser,
)
from .models import AdminAuthEvent, ReviewEvent, Submission, SMTPSettings
from .services.email_service import send_review_emails, send_acceptance_email, send_rejection_email
from .services.review_engine import run_review
from .audit import record_audit_event
from .monitoring import capture_exception
from .queue_health import queue_health_snapshot
from .ai_usage import ai_usage_snapshot
from .storage_security import (
    UploadSecurityError,
    sanitize_original_filename,
    secure_download_response,
    validate_manuscript_filename,
    validate_manuscript_zip,
)


def _clean_summary_text(value, limit=700):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + '…'
    return text


def _chapter_figure_count(text):
    return sum(
        1 for line in str(text or '').split('\n')
        if re.match(r'^\s*(figure|fig\.?)\s+\d+', line, re.I)
    )


def _format_chapter_editor_summary(filename, word_count, figure_count, model_summary='', key_gaps='', conclusion=''):
    summary = _clean_summary_text(model_summary) or 'No chapter-level narrative summary was returned by the model.'
    gaps = _clean_summary_text(key_gaps) or 'No specific chapter-level gaps were identified by the model.'
    final = _clean_summary_text(conclusion) or 'This chapter/PDF summary is provided for human review alongside the overall manuscript summary.'
    extracted_status = 'PASS' if word_count > 0 else 'FAIL'
    return '\n'.join([
        'Editor Summary',
        '',
        '1. Structural Findings',
        f'   - Source file: {filename or "Unknown"}',
        f'   - Total word count: {word_count:,}',
        '   - Chapter count: 1',
        f'   - Figure count: {figure_count}',
        f'   - Text extraction: {extracted_status} - Extracted {word_count:,} words from this ZIP document.',
        '',
        '2. Rubric Findings',
        '   - Criteria that passed: Not evaluated at chapter-summary level.',
        '   - Criteria needing work: Not evaluated at chapter-summary level.',
        '   - Criteria that failed: Not evaluated at chapter-summary level.',
        f'   - Supporting evidence: {summary}',
        '',
        '3. Key Gaps / Issues',
        f'   - Problems identified: {gaps}',
        '   - What needs attention or correction: review this chapter/PDF together with the overall manuscript findings.',
        '',
        '4. Overall Review Conclusion',
        '   - Decision: See overall manuscript review.',
        f'   - {final}',
    ])


def _generate_chapter_summary(text, filename='', word_count=0):
    text = str(text or '')
    word_count = int(word_count or 0)
    figure_count = _chapter_figure_count(text)
    if not text.strip():
        return _format_chapter_editor_summary(
            filename,
            word_count,
            figure_count,
            model_summary='No extractable text was found for this chapter/PDF.',
            key_gaps='The uploaded chapter/PDF may be scanned, image-only, empty, or unsupported by text extraction.',
            conclusion='No chapter-level summary can be generated until extractable text is available.',
        )
    preview = text[:500].strip()
    if len(text) > 500:
        preview += '…'
    if os.getenv('MOCK_AI_REVIEW', 'false').lower() in {'1', 'true', 'yes', 'on'}:
        return _format_chapter_editor_summary(
            filename,
            word_count,
            figure_count,
            model_summary=f'[MOCK AI] This is a mock AI summary for a chapter of {len(text)} characters.',
            key_gaps='Mock mode did not evaluate chapter-level gaps.',
            conclusion='Mock mode generated a structured chapter summary placeholder.',
        )

    # Chapter summaries are intentionally small and use a shorter context/output
    # budget than the full rubric review. This keeps ZIP uploads responsive on
    # 8 GB RAM development machines while preserving the existing response shape.
    from .services.ai_provider import ai_chat_json
    prompt = (
        'Summarize this ZIP document/chapter using only the supplied text. '
        'Return JSON only with keys: "summary", "key_gaps", and "conclusion". '
        'summary should be 1-3 concise sentences about the main plot points or core arguments. '
        'key_gaps should list any obvious chapter-level problems, or say none identified. '
        'conclusion should be one concise sentence. Do not invent facts.\n\n'
        + text[:12000]
    )
    try:
        _, output = ai_chat_json(
            prompt,
            max_tokens=220,
            timeout=120,
            force_provider='ollama',
            operation='chapter_summary',
        )
        payload = json.loads(output)
        if isinstance(payload, dict):
            summary = payload.get('summary', '')
            key_gaps = payload.get('key_gaps', '')
            conclusion = payload.get('conclusion', '')
            return _format_chapter_editor_summary(
                filename,
                word_count,
                figure_count,
                model_summary=summary,
                key_gaps=key_gaps,
                conclusion=conclusion,
            )
    except (RuntimeError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return _format_chapter_editor_summary(
        filename,
        word_count,
        figure_count,
        model_summary=preview,
        key_gaps='The local model did not return a valid structured chapter summary, so a text preview was used.',
        conclusion='This fallback chapter summary should be reviewed by a human editor.',
    )


def _json_body(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        return {}


def _submission_summary(item):
    zip_contents = None
    if item.review_record and isinstance(item.review_record, dict):
        zip_contents = item.review_record.get('zip_contents')
    return {
        'id': str(item.id),
        'created_at': item.created_at.isoformat(),
        'completed_at': item.completed_at.isoformat() if item.completed_at else None,
        'status': item.status,
        'kind': item.kind,
        'author_name': item.author_name,
        'author_email': item.author_email,
        'title': item.title,
        'decision': item.decision,
        'admin_decision': item.admin_decision,
        'model': item.model,
        'total_words': item.total_words,
        'manuscript_filename': item.manuscript_filename,
        'notification_status': item.notification_status,
        'editor_summary': item.editor_summary,
        'zip_contents': zip_contents,
    }


@require_GET
def health(request):
    return JsonResponse({'ok': True, 'service': 'flexee-manuscript-django-sqlite'})


@csrf_exempt
@require_POST
def submit(request):
    from django.utils import timezone
    from datetime import timedelta
    from .models import AuthorAuthEvent
    from .auth import remote_hash
    
    rh = remote_hash(request)
    window_minutes = int(os.getenv('PUBLIC_SUBMIT_WINDOW_MINUTES', '60'))
    max_submissions = int(os.getenv('PUBLIC_SUBMIT_MAX_SUBMISSIONS', '3'))
    recent_submissions = AuthorAuthEvent.objects.filter(
        remote_hash=rh,
        detail__action='public_submit',
        occurred_at__gte=timezone.now() - timedelta(minutes=window_minutes)
    ).count()
    if recent_submissions >= max_submissions:
        return JsonResponse({'detail': 'Too many public submissions. Try again later.', 'errors': ['Rate limited']}, status=429)

    max_bytes = int(os.getenv('MAX_MANUSCRIPT_BYTES', str(20 * 1024 * 1024)))
    kind = request.POST.get('type', '').strip()
    author = request.POST.get('author', '').strip()
    author_email = request.POST.get('email', '').strip()
    coauthors = request.POST.get('coauthors', '').strip()
    title = request.POST.get('title', '').strip()
    declared_sim = request.POST.get('sim', '').strip()
    if declared_sim == '__other':
        declared_sim = request.POST.get('sim_other', '').strip()
    disclosure = request.POST.get('disclosure', '').strip()
    notes = request.POST.get('notes', '').strip()
    attestation = request.POST.get('attestation', '').strip()
    upload = request.FILES.get('manuscript')

    errors = []
    if kind not in {'book', 'article'}:
        errors.append('type must be book or article')
    if not author:
        errors.append('author is required')
    if not title:
        errors.append('title is required')
    if not disclosure:
        errors.append('disclosure is required')
    if attestation != 'human-authored-with-ai-assistance':
        errors.append('authorship attestation is required')
    if kind == 'book' and not declared_sim:
        errors.append('a paired simulation is required for a book')
    if not upload:
        errors.append('manuscript is required')
    elif upload.size <= 0:
        errors.append('manuscript is empty')
    elif upload.size > max_bytes:
        errors.append(f'manuscript exceeds the {max_bytes // 1024 // 1024} MB upload limit')
    else:
        try:
            safe_upload_name = validate_manuscript_filename(upload.name)
        except UploadSecurityError as exc:
            errors.append(str(exc))
    if errors:
        return JsonResponse({'detail': errors[0], 'errors': errors}, status=400)

    AuthorAuthEvent.objects.create(
        remote_hash=rh,
        success=True,
        detail={'action': 'public_submit', 'email': author_email}
    )

    content = upload.read()
    upload.seek(0)
    digest = hashlib.sha256(content).hexdigest()
    safe_upload_name = sanitize_original_filename(upload.name, default='manuscript')
    upload.name = safe_upload_name
    if safe_upload_name.lower().endswith('.zip'):
        try:
            validate_manuscript_zip(content)
        except UploadSecurityError as exc:
            return JsonResponse({'detail': str(exc), 'errors': [str(exc)]}, status=400)

    submission = Submission.objects.create(
        status='processing', kind=kind, author_name=author, author_email=author_email,
        coauthors=coauthors, title=title, declared_sim=declared_sim, disclosure=disclosure,
        notes=notes, attestation=True, manuscript_filename=safe_upload_name,
        manuscript_file=upload,
        manuscript_bytes=len(content), manuscript_sha256=digest,
    )
    ReviewEvent.objects.create(submission=submission, event_type='accepted', detail={'filename': safe_upload_name, 'bytes': len(content)})

    from .models import ReviewJob
    from django_q.tasks import async_task
    
    job = ReviewJob.objects.create(
        job_type='public_review',
        reference_id=str(submission.id),
        status='queued'
    )
    
    async_task('review.tasks.run_public_review_task', job.id, submission.id)
    
    return JsonResponse({
        'id': str(submission.id),
        'job_id': job.id,
        'status': 'queued'
    }, status=202)


@require_GET
def submission_status(request, submission_id):
    from django.utils import timezone
    from datetime import timedelta
    from .models import AuthorAuthEvent, Submission
    from .auth import remote_hash
    
    rh = remote_hash(request)
    window_minutes = int(os.getenv('PUBLIC_STATUS_WINDOW_MINUTES', '60'))
    max_polls = int(os.getenv('PUBLIC_STATUS_MAX_POLLS', '1200'))
    
    recent_polls = AuthorAuthEvent.objects.filter(
        remote_hash=rh,
        detail__action='public_status',
        occurred_at__gte=timezone.now() - timedelta(minutes=window_minutes)
    ).count()
    if recent_polls >= max_polls:
        return JsonResponse({'detail': 'Too many status requests. Try again later.', 'errors': ['Rate limited']}, status=429)

    AuthorAuthEvent.objects.create(
        remote_hash=rh,
        success=True,
        detail={'action': 'public_status', 'submission_id': str(submission_id)}
    )

    try:
        submission = Submission.objects.get(id=submission_id)
    except Submission.DoesNotExist:
        return JsonResponse({'detail': 'Submission not found'}, status=404)

    if submission.status in ['queued', 'processing']:
        return JsonResponse({
            'status': submission.status,
            'progress': 'Review is running...'
        })
    elif submission.status == 'completed':
        email_warning = None
        if submission.notification_status == 'error' and isinstance(submission.notification_detail, dict):
            email_warning = submission.notification_detail.get('error')
            
        return JsonResponse({
            'status': submission.status,
            'decision': submission.decision,
            'total_words': submission.total_words,
            'author_letter': submission.author_letter,
            'email_warning': email_warning
        })
    else:
        return JsonResponse({
            'status': submission.status,
            'error': 'Review processing failed. Please contact support.' if submission.error else None
        })


@csrf_exempt
@require_POST
def admin_verify_password(request):
    data = _json_body(request)
    rh = remote_hash(request)
    window_minutes = int(os.getenv('ADMIN_LOGIN_WINDOW_MINUTES', '15'))
    max_failures = int(os.getenv('ADMIN_LOGIN_MAX_FAILURES', '10'))
    recent_failures = AdminAuthEvent.objects.filter(
        remote_hash=rh, success=False, occurred_at__gte=timezone.now() - timedelta(minutes=window_minutes)
    ).count()
    if recent_failures >= max_failures:
        AdminAuthEvent.objects.create(remote_hash=rh, success=False, detail={'reason': 'rate_limited'})
        return JsonResponse({'detail': 'Too many failed login attempts. Try again later.'}, status=429)

    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))

    from .models import EditorUser
    user = EditorUser.objects.filter(email=username).first()
    ok = bool(user and verify_password(password, user.password_hash) and os.getenv('ADMIN_SESSION_SECRET', ''))

    if not ok:
        AdminAuthEvent.objects.create(remote_hash=rh, success=False, detail={'username': username, 'reason': 'invalid_credentials'})
        return JsonResponse({'detail': 'Invalid username or password.'}, status=401)

    requires_totp = user.memberships.filter(role__in=['owner', 'editor']).exists() or user.platform_superuser
    totp_setup_uri = None

    if requires_totp and not user.totp_secret:
        import base64
        import secrets
        from urllib.parse import quote
        user.totp_secret = base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')
        user.save(update_fields=['totp_secret'])
        
        label = quote(f'Flexee Admin:{user.email}')
        issuer = quote('Flexee Manuscript Admin')
        totp_setup_uri = f'otpauth://totp/{label}?secret={user.totp_secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30'

    resp = {'ok': True}
    if totp_setup_uri:
        resp['totp_setup_uri'] = totp_setup_uri

    return JsonResponse(resp)


@csrf_exempt
@require_POST
def admin_login(request):
    data = _json_body(request)
    rh = remote_hash(request)
    window_minutes = int(os.getenv('ADMIN_LOGIN_WINDOW_MINUTES', '15'))
    max_failures = int(os.getenv('ADMIN_LOGIN_MAX_FAILURES', '10'))
    recent_failures = AdminAuthEvent.objects.filter(
        remote_hash=rh, success=False, occurred_at__gte=timezone.now() - timedelta(minutes=window_minutes)
    ).count()
    if recent_failures >= max_failures:
        AdminAuthEvent.objects.create(remote_hash=rh, success=False, detail={'reason': 'rate_limited'})
        return JsonResponse({'detail': 'Too many failed login attempts. Try again later.'}, status=429)

    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))
    code = str(data.get('totp', '')).strip()
    
    from .models import EditorUser
    user = EditorUser.objects.filter(email=username).first()
    
    ok = False
    totp_missing_error = False
    
    if user and os.getenv('ADMIN_SESSION_SECRET', '') and verify_password(password, user.password_hash):
        requires_totp = user.memberships.filter(role__in=['owner', 'editor']).exists() or user.platform_superuser
        if requires_totp:
            if not user.totp_secret:
                totp_missing_error = True
            elif verify_totp(user.totp_secret, code):
                ok = True
        else:
            if not user.totp_secret or verify_totp(user.totp_secret, code):
                ok = True
                
    if totp_missing_error:
        AdminAuthEvent.objects.create(remote_hash=rh, success=False, detail={'username': username, 'reason': 'totp_required'})
        return JsonResponse({'detail': 'Two-factor authentication is required for this account, but no authenticator has been configured.'}, status=403)
        
    AdminAuthEvent.objects.create(remote_hash=rh, success=ok, detail={'username': username, 'reason': 'ok' if ok else 'invalid_credentials'})
    if not ok:
        return JsonResponse({'detail': 'Invalid username, password, or authenticator code.'}, status=401)
    token, max_age = issue_session(username)
    response = JsonResponse({'ok': True, 'username': username})
    set_session_cookie(response, token, max_age)
    response['Cache-Control'] = 'no-store'
    return response


@csrf_exempt
@require_POST
def admin_logout(request):
    response = JsonResponse({'ok': True})
    clear_session_cookie(response)
    response['Cache-Control'] = 'no-store'
    return response


@require_GET
def admin_session(request):
    session = read_session(request)
    user = None
    memberships = []
    if session:
        from .models import EditorUser
        user = EditorUser.objects.filter(email=session.get('u')).first()
        if user:
            memberships = list(
                user.memberships.select_related('organization')
                .values('organization_id', 'organization__name', 'role')
            )

    response = JsonResponse({
        'authenticated': bool(session and user),
        'username': user.email if user else None,
        'platform_superuser': bool(user and user.platform_superuser),
        'memberships': memberships,
    })
    response['Cache-Control'] = 'no-store'
    return response


@require_GET
@require_platform_superuser
def admin_submissions(request):
    qs = Submission.objects.all()
    q = request.GET.get('q', '').strip()
    kind = request.GET.get('kind', '').strip()
    status = request.GET.get('status', '').strip()
    decision = request.GET.get('decision', '').strip()
    if q:
        lookup = Q(title__icontains=q) | Q(author_name__icontains=q) | Q(author_email__icontains=q)
        try:
            lookup |= Q(id=uuid.UUID(q))
        except (ValueError, AttributeError):
            pass
        qs = qs.filter(lookup)
    if kind in {'book', 'article'}:
        qs = qs.filter(kind=kind)
    if status in {'processing', 'completed', 'failed'}:
        qs = qs.filter(status=status)
    if decision in {'PASS_TO_HUMAN', 'REFER_TO_HUMAN_WITH_FLAGS', 'RETURN_TO_AUTHOR'}:
        qs = qs.filter(decision=decision)
    elif decision in {'ACCEPTED', 'REJECTED'}:
        qs = qs.filter(admin_decision=decision)
    limit = min(max(int(request.GET.get('limit', '100') or 100), 1), 200)
    items = list(qs[:limit])
    counts = Submission.objects.aggregate(
        total=Count('id'),
        processing=Count('id', filter=Q(status='processing')),
        completed=Count('id', filter=Q(status='completed')),
        failed=Count('id', filter=Q(status='failed')),
    )
    response = JsonResponse({'counts': counts, 'items': [_submission_summary(item) for item in items]})
    response['Cache-Control'] = 'no-store'
    return response


@require_GET
@require_platform_superuser
def admin_queue_health(request):
    response = JsonResponse(queue_health_snapshot())
    response['Cache-Control'] = 'no-store'
    return response


@require_GET
@require_platform_superuser
def admin_ai_usage(request):
    response = JsonResponse(ai_usage_snapshot())
    response['Cache-Control'] = 'no-store'
    return response


@require_GET
@require_platform_superuser
def admin_submission_detail(request, submission_id):
    try:
        item = Submission.objects.prefetch_related('events').get(id=submission_id)
    except Submission.DoesNotExist:
        return JsonResponse({'detail': 'Submission not found'}, status=404)
    data = _submission_summary(item)
    # Extract zip_contents from the review_record if present
    zip_contents = None
    if item.review_record and isinstance(item.review_record, dict):
        zip_contents = item.review_record.get('zip_contents')
    data.update({
        'coauthors': item.coauthors,
        'declared_sim': item.declared_sim,
        'disclosure': item.disclosure,
        'notes': item.notes,
        'attestation': item.attestation,
        'manuscript_bytes': item.manuscript_bytes,
        'manuscript_sha256': item.manuscript_sha256,
        'review_record': item.review_record,
        'editor_summary': item.editor_summary,
        'author_letter': item.author_letter,
        'error': item.error,
        'zip_contents': zip_contents,
        'notification_detail': item.notification_detail,
        'notified_at': item.notified_at.isoformat() if item.notified_at else None,
        'events': [
            {'id': event.id, 'created_at': event.created_at.isoformat(), 'event_type': event.event_type, 'detail': event.detail}
            for event in item.events.all()
        ],
    })
    record_audit_event(
        request,
        'legacy_submission.viewed',
        resource_type='legacy_submission',
        resource_id=submission_id,
        detail={'status': item.status, 'title': item.title},
    )
    response = JsonResponse(data)
    response['Cache-Control'] = 'no-store'
    return response


@csrf_exempt
@require_POST
@require_platform_superuser
def admin_submission_accept(request, submission_id):
    try:
        submission = Submission.objects.get(id=submission_id)
    except Submission.DoesNotExist:
        return JsonResponse({'detail': 'Submission not found'}, status=404)

    if submission.admin_decision:
        return JsonResponse({'detail': 'Submission already has an admin decision'}, status=400)

    data = _json_body(request)
    message = str(data.get('message', '')).strip()

    submission.admin_decision = 'ACCEPTED'
    submission.acceptance_message = message
    submission.save(update_fields=['admin_decision', 'acceptance_message', 'updated_at'])
    ReviewEvent.objects.create(submission=submission, event_type='admin_accepted', detail={'message': message})
    record_audit_event(
        request,
        'legacy_submission.decision_recorded',
        resource_type='legacy_submission',
        resource_id=submission.id,
        detail={'decision': 'ACCEPTED', 'message_present': bool(message)},
    )

    email_warning = None
    try:
        delivery = send_acceptance_email(submission, message)
        submission.notification_status = delivery['status']
        submission.notification_detail = delivery['detail']
        submission.notified_at = delivery['notified_at']
        submission.save(update_fields=['notification_status', 'notification_detail', 'notified_at', 'updated_at'])
        ReviewEvent.objects.create(submission=submission, event_type='acceptance_email_sent', detail=delivery['detail'])
    except Exception as email_error:
        capture_exception(
            email_error,
            component='email',
            operation='legacy_acceptance_notification',
            tags={'submission_flow': 'legacy'},
        )
        email_warning = str(email_error)
        submission.notification_status = 'error'
        submission.notification_detail = {'error': email_warning}
        submission.save(update_fields=['notification_status', 'notification_detail', 'updated_at'])
        ReviewEvent.objects.create(submission=submission, event_type='acceptance_email_error', detail={'error': email_warning})

    return JsonResponse({'ok': True, 'admin_decision': submission.admin_decision, 'email_warning': email_warning})


@csrf_exempt
@require_POST
@require_platform_superuser
def admin_submission_reject(request, submission_id):
    try:
        submission = Submission.objects.get(id=submission_id)
    except Submission.DoesNotExist:
        return JsonResponse({'detail': 'Submission not found'}, status=404)

    if submission.admin_decision:
        return JsonResponse({'detail': 'Submission already has an admin decision'}, status=400)

    data = _json_body(request)
    reason = str(data.get('reason', '')).strip()
    if not reason:
        return JsonResponse({'detail': 'Rejection reason is required'}, status=400)

    submission.admin_decision = 'REJECTED'
    submission.rejection_reason = reason
    submission.save(update_fields=['admin_decision', 'rejection_reason', 'updated_at'])
    ReviewEvent.objects.create(submission=submission, event_type='admin_rejected', detail={'reason': reason})
    record_audit_event(
        request,
        'legacy_submission.decision_recorded',
        resource_type='legacy_submission',
        resource_id=submission.id,
        detail={'decision': 'REJECTED', 'reason_present': bool(reason)},
    )

    email_warning = None
    try:
        delivery = send_rejection_email(submission, reason)
        submission.notification_status = delivery['status']
        submission.notification_detail = delivery['detail']
        submission.notified_at = delivery['notified_at']
        submission.save(update_fields=['notification_status', 'notification_detail', 'notified_at', 'updated_at'])
        ReviewEvent.objects.create(submission=submission, event_type='rejection_email_sent', detail=delivery['detail'])
    except Exception as email_error:
        capture_exception(
            email_error,
            component='email',
            operation='legacy_rejection_notification',
            tags={'submission_flow': 'legacy'},
        )
        email_warning = str(email_error)
        submission.notification_status = 'error'
        submission.notification_detail = {'error': email_warning}
        submission.save(update_fields=['notification_status', 'notification_detail', 'updated_at'])
        ReviewEvent.objects.create(submission=submission, event_type='rejection_email_error', detail={'error': email_warning})

    return JsonResponse({'ok': True, 'admin_decision': submission.admin_decision, 'email_warning': email_warning})


@csrf_exempt
@require_POST
@require_platform_superuser
def admin_submission_delete(request, submission_id):
    try:
        submission = Submission.objects.get(id=submission_id)
        record_audit_event(
            request,
            'legacy_submission.deleted',
            resource_type='legacy_submission',
            resource_id=submission.id,
            detail={'title': submission.title},
        )
        submission.delete()
        return JsonResponse({'ok': True})
    except Submission.DoesNotExist:
        return JsonResponse({'detail': 'Submission not found'}, status=404)

@csrf_exempt
@require_POST
@require_platform_superuser
def admin_submission_send_email(request, submission_id):
    try:
        submission = Submission.objects.get(id=submission_id)
    except Submission.DoesNotExist:
        return JsonResponse({'detail': 'Submission not found'}, status=404)

    data = _json_body(request)
    subject = str(data.get('subject', '')).strip()
    body = str(data.get('body', '')).strip()

    if not subject or not body:
        return JsonResponse({'detail': 'Subject and body are required'}, status=400)

    email_warning = None
    try:
        from .services.email_service import send_custom_email
        delivery = send_custom_email(submission, subject, body)
        submission.notification_status = delivery['status']
        submission.notification_detail = delivery['detail']
        submission.notified_at = delivery['notified_at']
        submission.save(update_fields=['notification_status', 'notification_detail', 'notified_at', 'updated_at'])
        ReviewEvent.objects.create(submission=submission, event_type='custom_email_sent', detail=delivery['detail'])
    except Exception as email_error:
        capture_exception(
            email_error,
            component='email',
            operation='legacy_custom_notification',
            tags={'submission_flow': 'legacy'},
        )
        email_warning = str(email_error)
        submission.notification_status = 'error'
        submission.notification_detail = {'error': email_warning}
        submission.save(update_fields=['notification_status', 'notification_detail', 'updated_at'])
        ReviewEvent.objects.create(submission=submission, event_type='custom_email_error', detail={'error': email_warning})

    record_audit_event(
        request,
        'legacy_submission.email_sent',
        resource_type='legacy_submission',
        resource_id=submission.id,
        detail={'subject': subject, 'delivery_warning': bool(email_warning)},
    )
    return JsonResponse({'ok': True, 'email_warning': email_warning})


@require_GET
@require_platform_superuser
def admin_submission_download(request, submission_id):
    try:
        submission = Submission.objects.get(id=submission_id)
        if not submission.manuscript_file:
            return JsonResponse({'detail': 'No manuscript file available for this submission (likely submitted before storage feature)'}, status=404)

        # Determine content type based on filename
        content_type = 'application/pdf'
        if submission.manuscript_filename.lower().endswith('.docx'):
            content_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        elif submission.manuscript_filename.lower().endswith('.md'):
            content_type = 'text/markdown'
        elif submission.manuscript_filename.lower().endswith('.zip'):
            content_type = 'application/zip'

        file_handle = submission.manuscript_file.open('rb')
        record_audit_event(
            request,
            'legacy_submission.manuscript_downloaded',
            resource_type='legacy_submission',
            resource_id=submission.id,
            detail={'filename': submission.manuscript_filename},
        )
        return secure_download_response(
            file_handle,
            content_type=content_type,
            filename=submission.manuscript_filename,
        )
    except Submission.DoesNotExist:
        return JsonResponse({'detail': 'Submission not found'}, status=404)

@csrf_exempt
@require_platform_superuser
def admin_smtp_settings(request):
    smtp, _ = SMTPSettings.objects.get_or_create(id=1)
    if request.method == 'GET':
        return JsonResponse({
            'sender_name': smtp.sender_name,
            'sender_email': smtp.sender_email,
            'reply_to_email': smtp.reply_to_email,
            'host': smtp.host,
            'port': smtp.port,
            'username': smtp.username,
            'use_tls': smtp.use_tls,
            'use_ssl': smtp.use_ssl,
            'admin_notification_emails': smtp.admin_notification_emails,
        })
    elif request.method == 'POST':
        try:
            body = json.loads(request.body)
            smtp.sender_name = body.get('sender_name', smtp.sender_name)
            smtp.sender_email = body.get('sender_email', smtp.sender_email)
            smtp.reply_to_email = body.get('reply_to_email', smtp.reply_to_email)
            smtp.host = body.get('host', smtp.host)
            smtp.port = int(body.get('port', smtp.port))
            smtp.username = body.get('username', smtp.username)
            if 'password' in body and body['password'].strip() != '':
                smtp.password = body['password']
            smtp.use_tls = bool(body.get('use_tls', smtp.use_tls))
            smtp.use_ssl = bool(body.get('use_ssl', smtp.use_ssl))
            smtp.admin_notification_emails = body.get('admin_notification_emails', smtp.admin_notification_emails)
            smtp.save()
            record_audit_event(
                request,
                'smtp.updated',
                resource_type='smtp_settings',
                resource_id=smtp.id,
                detail={
                    'host': smtp.host,
                    'port': smtp.port,
                    'username_present': bool(smtp.username),
                    'password_changed': bool('password' in body and str(body.get('password', '')).strip()),
                },
            )
            return JsonResponse({'ok': True})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Method not allowed'}, status=405)

@csrf_exempt
@require_platform_superuser
def admin_smtp_test(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)

    try:
        body = json.loads(request.body)
        host = body.get('host', '')
        port = int(body.get('port', 587))
        username = body.get('username', '')
        password = body.get('password', '')
        use_tls = bool(body.get('use_tls', True))
        use_ssl = bool(body.get('use_ssl', False))
        sender_email = body.get('sender_email', '')
        sender_name = body.get('sender_name', '')
        test_email = body.get('test_email', '').strip()

        if password.strip() == '':
            smtp, _ = SMTPSettings.objects.get_or_create(id=1)
            password = smtp.password

        from django.core.mail import get_connection, EmailMultiAlternatives
        connection = get_connection(
            backend='django.core.mail.backends.smtp.EmailBackend',
            host=host,
            port=port,
            username=username,
            password=password,
            use_tls=use_tls,
            use_ssl=use_ssl,
            fail_silently=False,
        )

        from_header = f"{sender_name} <{sender_email}>" if sender_name and sender_email else (sender_email or username)
        target_email = test_email or sender_email or username
        message = EmailMultiAlternatives(
            subject='Test SMTP Connection - Flexee',
            body='This is a test email to verify your SMTP configuration in Flexee.',
            from_email=from_header,
            to=[target_email],
            connection=connection
        )
        sent = message.send(fail_silently=False)
        if sent:
            return JsonResponse({'ok': True, 'message': 'Test email sent successfully!'})
        else:
            return JsonResponse({'error': 'Failed to send test email for unknown reasons.'}, status=400)

    except Exception as e:
        capture_exception(
            e,
            component='email',
            operation='smtp_test',
            tags={'source': 'admin_smtp_test'},
        )
        return JsonResponse({'error': str(e)}, status=400)
