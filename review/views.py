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
    set_session_cookie, verify_password, verify_totp,
)
from .models import AdminAuthEvent, ReviewEvent, Submission, SMTPSettings
from .services.email_service import send_review_emails, send_acceptance_email, send_rejection_email
from .services.review_engine import run_review


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
    from .services.local_llm import ollama_chat_json
    prompt = (
        'Summarize this ZIP document/chapter using only the supplied text. '
        'Return JSON only with keys: "summary", "key_gaps", and "conclusion". '
        'summary should be 1-3 concise sentences about the main plot points or core arguments. '
        'key_gaps should list any obvious chapter-level problems, or say none identified. '
        'conclusion should be one concise sentence. Do not invent facts.\n\n'
        + text[:12000]
    )
    try:
        _, output = ollama_chat_json(
            prompt,
            max_tokens=220,
            timeout=120,
            num_ctx=4096,
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
    elif not upload.name.lower().endswith(('.docx', '.pdf', '.md', '.zip')):
        errors.append('manuscript must be a .docx, .pdf, .md, or .zip file')
    if errors:
        return JsonResponse({'detail': errors[0], 'errors': errors}, status=400)

    content = upload.read()
    upload.seek(0)
    digest = hashlib.sha256(content).hexdigest()

    review_content = content
    review_filename = upload.name
    zip_contents = None
    zip_overall_source = None
    if upload.name.lower().endswith('.zip'):
        try:
            from .services.review_engine import extract_text, word_count as wc_fn
            with zipfile.ZipFile(BytesIO(content)) as zf:
                manuscript_entries = []
                for name in zf.namelist():
                    if name.lower().endswith(('.docx', '.pdf', '.md')) and not name.startswith('__MACOSX') and not os.path.basename(name).startswith('.'):
                        manuscript_entries.append(name)
                if not manuscript_entries:
                    return JsonResponse({'detail': 'No .docx, .pdf, or .md file found inside the ZIP archive', 'errors': ['No .docx, .pdf, or .md file found inside the ZIP archive']}, status=400)

                zip_docs = []
                docs_to_summarize = []
                combined_parts = []
                for index, entry_name in enumerate(manuscript_entries, start=1):
                    entry_bytes = zf.read(entry_name)
                    base_name = os.path.basename(entry_name)
                    try:
                        doc_text = extract_text(entry_bytes, base_name)
                        doc_words = wc_fn(doc_text)
                    except Exception:
                        doc_text = ''
                        doc_words = 0

                    docs_to_summarize.append((base_name, entry_name, doc_words, doc_text))
                    if doc_text.strip():
                        combined_parts.append(
                            f"# ZIP Document {index}: {base_name}\n"
                            f"Source path: {entry_name}\n"
                            f"Word count: {doc_words}\n\n"
                            f"{doc_text.strip()}"
                        )

                if not combined_parts:
                    return JsonResponse({'detail': 'No extractable text found inside the ZIP archive', 'errors': ['No extractable text found inside the ZIP archive']}, status=400)

                # Keep concurrency low so CPU/RAM contention does not make local
                # Qwen2.5 slower on an 8 GB machine. Ollama reuses the loaded model.
                max_workers = max(1, min(int(os.getenv('OLLAMA_CHAPTER_WORKERS', '2')), 2))
                with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(_generate_chapter_summary, d[3], d[0], d[2]): d
                        for d in docs_to_summarize
                    }
                    results_map = {}
                    for future in concurrent.futures.as_completed(futures):
                        d = futures[future]
                        try:
                            results_map[d[1]] = future.result()
                        except Exception:
                            results_map[d[1]] = _format_chapter_editor_summary(
                                d[0],
                                d[2],
                                _chapter_figure_count(d[3]),
                                model_summary='Chapter/PDF summary generation failed.',
                                key_gaps='The chapter-level summary call failed for this document.',
                                conclusion='A human editor should inspect this chapter/PDF directly.',
                            )

                for d in docs_to_summarize:
                    base_name, entry_name, doc_words, doc_text = d
                    zip_docs.append({
                        'filename': base_name,
                        'path': entry_name,
                        'word_count': doc_words,
                        'preview': results_map.get(entry_name, "(error)"),
                    })

                zip_contents = zip_docs

                # Generate the main editor summary from all extracted ZIP documents,
                # not only the first chapter/file. The per-file chapter summaries
                # remain available separately in zip_contents.
                combined_text = "\n\n---\n\n".join(combined_parts)
                review_content = combined_text.encode('utf-8')
                review_filename = f"{os.path.splitext(os.path.basename(upload.name))[0]}_combined.md"
                zip_overall_source = {
                    'type': 'combined_zip_text',
                    'document_count': len(zip_docs),
                    'filename': review_filename,
                }

        except zipfile.BadZipFile:
            return JsonResponse({'detail': 'The uploaded file is not a valid ZIP archive', 'errors': ['The uploaded file is not a valid ZIP archive']}, status=400)

    submission = Submission.objects.create(
        status='processing', kind=kind, author_name=author, author_email=author_email,
        coauthors=coauthors, title=title, declared_sim=declared_sim, disclosure=disclosure,
        notes=notes, attestation=True, manuscript_filename=upload.name,
        manuscript_file=upload,
        manuscript_bytes=len(content), manuscript_sha256=digest,
    )
    ReviewEvent.objects.create(submission=submission, event_type='accepted', detail={'filename': upload.name, 'bytes': len(content)})

    try:
        result = run_review(review_content, review_filename, kind, declared_sim, disclosure)
        submission.status = 'completed'
        submission.completed_at = timezone.now()
        submission.decision = result['decision']
        submission.model = result['model']
        submission.total_words = result['total_words']
        review_record = result['record']
        if zip_contents:
            review_record['zip_contents'] = zip_contents
        if zip_overall_source:
            review_record['zip_overall_summary_source'] = zip_overall_source
        submission.review_record = review_record
        submission.editor_summary = result['editor_summary']
        submission.author_letter = result['author_letter']
        submission.notification_status = 'pending'
        submission.save()
        ReviewEvent.objects.create(submission=submission, event_type='review_completed', detail={'decision': result['decision'], 'model': result['model']})
        email_warning = None
        try:
            delivery = send_review_emails(submission, result)
            submission.notification_status = delivery['status']
            submission.notification_detail = delivery['detail']
            submission.notified_at = delivery['notified_at']
            submission.save(update_fields=['notification_status', 'notification_detail', 'notified_at', 'updated_at'])
            ReviewEvent.objects.create(submission=submission, event_type='notification_sent', detail=delivery['detail'])
        except Exception as email_error:
            email_warning = str(email_error)
            submission.notification_status = 'error'
            submission.notification_detail = {'error': email_warning}
            submission.save(update_fields=['notification_status', 'notification_detail', 'updated_at'])
            ReviewEvent.objects.create(submission=submission, event_type='notification_error', detail={'error': email_warning})
        return JsonResponse({
            'id': str(submission.id), 'status': submission.status, 'decision': submission.decision,
            'total_words': submission.total_words, 'model': submission.model,
            'editor_summary': submission.editor_summary, 'author_letter': submission.author_letter,
            'notification_status': submission.notification_status, 'email_warning': email_warning,
        })
    except Exception as error:
        submission.status = 'failed'
        submission.completed_at = timezone.now()
        submission.error = {'type': error.__class__.__name__, 'message': str(error)}
        submission.save(update_fields=['status', 'completed_at', 'error', 'updated_at'])
        ReviewEvent.objects.create(submission=submission, event_type='review_failed', detail=submission.error)
        return JsonResponse({'id': str(submission.id), 'status': 'failed', 'detail': str(error)}, status=503)


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

    return JsonResponse({'ok': True})


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
    
    ok = bool(
        user
        and os.getenv('ADMIN_SESSION_SECRET', '')
        and verify_password(password, user.password_hash)
        and (not user.totp_secret or verify_totp(user.totp_secret, code))
    )
    
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
    response = JsonResponse({'authenticated': bool(session), 'username': session.get('u') if session else None})
    response['Cache-Control'] = 'no-store'
    return response


@require_GET
@require_admin
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
@require_admin
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
    response = JsonResponse(data)
    response['Cache-Control'] = 'no-store'
    return response


@csrf_exempt
@require_POST
@require_admin
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

    email_warning = None
    try:
        delivery = send_acceptance_email(submission, message)
        submission.notification_status = delivery['status']
        submission.notification_detail = delivery['detail']
        submission.notified_at = delivery['notified_at']
        submission.save(update_fields=['notification_status', 'notification_detail', 'notified_at', 'updated_at'])
        ReviewEvent.objects.create(submission=submission, event_type='acceptance_email_sent', detail=delivery['detail'])
    except Exception as email_error:
        email_warning = str(email_error)
        submission.notification_status = 'error'
        submission.notification_detail = {'error': email_warning}
        submission.save(update_fields=['notification_status', 'notification_detail', 'updated_at'])
        ReviewEvent.objects.create(submission=submission, event_type='acceptance_email_error', detail={'error': email_warning})

    return JsonResponse({'ok': True, 'admin_decision': submission.admin_decision, 'email_warning': email_warning})


@csrf_exempt
@require_POST
@require_admin
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

    email_warning = None
    try:
        delivery = send_rejection_email(submission, reason)
        submission.notification_status = delivery['status']
        submission.notification_detail = delivery['detail']
        submission.notified_at = delivery['notified_at']
        submission.save(update_fields=['notification_status', 'notification_detail', 'notified_at', 'updated_at'])
        ReviewEvent.objects.create(submission=submission, event_type='rejection_email_sent', detail=delivery['detail'])
    except Exception as email_error:
        email_warning = str(email_error)
        submission.notification_status = 'error'
        submission.notification_detail = {'error': email_warning}
        submission.save(update_fields=['notification_status', 'notification_detail', 'updated_at'])
        ReviewEvent.objects.create(submission=submission, event_type='rejection_email_error', detail={'error': email_warning})

    return JsonResponse({'ok': True, 'admin_decision': submission.admin_decision, 'email_warning': email_warning})


@csrf_exempt
@require_POST
@require_admin
def admin_submission_delete(request, submission_id):
    try:
        submission = Submission.objects.get(id=submission_id)
        submission.delete()
        return JsonResponse({'ok': True})
    except Submission.DoesNotExist:
        return JsonResponse({'detail': 'Submission not found'}, status=404)

@csrf_exempt
@require_POST
@require_admin
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
        email_warning = str(email_error)
        submission.notification_status = 'error'
        submission.notification_detail = {'error': email_warning}
        submission.save(update_fields=['notification_status', 'notification_detail', 'updated_at'])
        ReviewEvent.objects.create(submission=submission, event_type='custom_email_error', detail={'error': email_warning})

    return JsonResponse({'ok': True, 'email_warning': email_warning})


@require_GET
@require_admin
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

        return FileResponse(
            submission.manuscript_file.open('rb'),
            content_type=content_type,
            as_attachment=False, # Set to False so it opens in the browser if possible
            filename=submission.manuscript_filename
        )
    except Submission.DoesNotExist:
        return JsonResponse({'detail': 'Submission not found'}, status=404)

@csrf_exempt
@require_admin
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
            'password': smtp.password,
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
            return JsonResponse({'ok': True})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Method not allowed'}, status=405)

@csrf_exempt
@require_admin
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
        return JsonResponse({'error': str(e)}, status=400)
