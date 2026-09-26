import hashlib
import hmac
import json
import os
import re
import secrets
from django.db import IntegrityError, transaction
from django.http import JsonResponse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .auth import (
    require_admin,
    require_author,
    issue_author_session,
    set_author_session_cookie,
    clear_author_session_cookie,
    read_author_session,
    hash_password,
    verify_password,
    remote_hash,
    check_org_access,
)
from .models import (
    Author,
    AuthorAuthEvent,
    EditorFeedback,
    EvidenceFinding,
    Manuscript,
    Organization,
    ReadinessAssessment,
    SubmissionTransfer,
    SubmissionRequirementFile,
    Venue,
    VenueAgentConfig,
    VenueMatch,
    VenueSubmission,
    ReviewJob,
)
from django_q.tasks import async_task
from .services.review_engine import word_count
from .services.author_agents import (
    AgentExecutionError,
    AgentInputError,
    load_manuscript_text,
    run_semantic_matching,
    run_semantic_readiness,
    run_venue_assessment,
)
from .services.email_service import _send as _send_email


ALLOWED_MANUSCRIPT_TYPES = {value for value, _ in Manuscript.TYPE_CHOICES}
ALLOWED_VENUE_TYPES = {value for value, _ in Venue.TYPE_CHOICES}
ALLOWED_REQUIREMENT_TYPES = {'text', 'textarea', 'url', 'checkbox', 'file'}
STRUCTURED_DESK_RULE_OPERATORS = {
    'word_count': {'>', '>=', '<', '<=', '==', '!='},
    'manuscript_type': {'==', '!=', 'in', 'not_in'},
    'disclosure': {'contains', 'not_contains', 'empty', 'not_empty'},
}


def _queue_unique_job(job_type, reference_id, task_path, *task_args):
    """Create at most one queued/processing job for a logical operation."""
    reference_id = str(reference_id)
    try:
        with transaction.atomic():
            existing = ReviewJob.objects.filter(
                job_type=job_type,
                reference_id=reference_id,
                status__in=['queued', 'processing'],
            ).order_by('-created_at').first()
            if existing:
                return existing, False
            job = ReviewJob.objects.create(
                job_type=job_type,
                reference_id=reference_id,
                status='queued',
            )
    except IntegrityError:
        # The partial unique constraint closes the race between the lookup and
        # create. Query after leaving the failed savepoint/transaction.
        existing = ReviewJob.objects.filter(
            job_type=job_type,
            reference_id=reference_id,
            status__in=['queued', 'processing'],
        ).order_by('-created_at').first()
        if existing is None:
            raise
        return existing, False

    transaction.on_commit(lambda: async_task(task_path, job.id, *task_args))
    return job, True


def _hash_author_token(token):
    return hashlib.sha256(str(token or '').encode('utf-8')).hexdigest()


def _author_access_error(request, manuscript):
    session = read_author_session(request)
    if session and session.get('aid') == str(manuscript.author_account_id):
        return None

    token = request.META.get('HTTP_X_MANUSCRIPT_TOKEN', '').strip()
    if not token:
        return JsonResponse(
            {'detail': 'Author authentication or token required', 'code': 'author_token_required'},
            status=401,
        )
    stored = str(manuscript.access_token_hash or '')
    if not stored or not hmac.compare_digest(stored, _hash_author_token(token)):
        return JsonResponse(
            {'detail': 'Author manuscript access token is invalid', 'code': 'author_token_invalid'},
            status=403,
        )
    return None


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


def _normalise_required_submission_items(value):
    items = _json_list(value)
    if len(items) > 30:
        raise ValueError('required_submission_items supports at most 30 items')

    normalised = []
    seen = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f'required_submission_items[{index}] must be an object')
        label = str(item.get('label', '')).strip()
        if not label:
            raise ValueError(f'required_submission_items[{index}].label is required')
        key = str(item.get('key', '')).strip() or slugify(label).replace('-', '_')
        key = re.sub(r'[^a-zA-Z0-9_-]+', '_', key).strip('_')[:120]
        if not key:
            raise ValueError(f'required_submission_items[{index}].key is invalid')
        if key in seen:
            raise ValueError(f'duplicate required submission item key: {key}')
        seen.add(key)

        item_type = str(item.get('type', 'text')).strip().lower()
        if item_type not in ALLOWED_REQUIREMENT_TYPES:
            raise ValueError(
                f'required_submission_items[{index}].type must be one of '
                + ', '.join(sorted(ALLOWED_REQUIREMENT_TYPES))
            )

        try:
            max_length = int(item.get('max_length', 4000))
        except (TypeError, ValueError):
            raise ValueError(f'required_submission_items[{index}].max_length must be an integer')
        max_length = max(1, min(max_length, 20000))

        normalised.append({
            'key': key,
            'label': label[:240],
            'type': item_type,
            'required': bool(item.get('required', True)),
            'help_text': str(item.get('help_text', '')).strip()[:500],
            'max_length': max_length,
        })
    return normalised


def _normalise_structured_desk_rules(value):
    rules = _json_list(value)
    if len(rules) > 30:
        raise ValueError('structured_desk_rejection_rules supports at most 30 rules')

    normalised = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise ValueError(f'structured_desk_rejection_rules[{index}] must be an object')
        field = str(rule.get('field', '')).strip()
        operator = str(rule.get('operator', '')).strip()
        if field not in STRUCTURED_DESK_RULE_OPERATORS:
            raise ValueError(
                f'structured_desk_rejection_rules[{index}].field must be one of '
                + ', '.join(sorted(STRUCTURED_DESK_RULE_OPERATORS))
            )
        if operator not in STRUCTURED_DESK_RULE_OPERATORS[field]:
            raise ValueError(
                f'operator {operator!r} is not supported for structured desk rule field {field!r}'
            )
        value_required = operator not in {'empty', 'not_empty'}
        rule_value = rule.get('value')
        if value_required and rule_value is None:
            raise ValueError(f'structured_desk_rejection_rules[{index}].value is required')
        if field == 'word_count' and value_required:
            try:
                rule_value = int(rule_value)
            except (TypeError, ValueError):
                raise ValueError(f'structured_desk_rejection_rules[{index}].value must be an integer')
        if operator in {'in', 'not_in'}:
            if not isinstance(rule_value, list) or not rule_value:
                raise ValueError(f'structured_desk_rejection_rules[{index}].value must be a non-empty list')
            rule_value = [str(item).strip() for item in rule_value if str(item).strip()]

        message = str(rule.get('message', '')).strip()
        if not message:
            raise ValueError(f'structured_desk_rejection_rules[{index}].message is required')
        normalised.append({
            'field': field,
            'operator': operator,
            'value': rule_value,
            'message': message[:500],
        })
    return normalised


def _structured_desk_rule_violations(manuscript, config, readiness):
    rules = config.structured_desk_rejection_rules or []
    if not rules:
        return []

    word_total = None
    if readiness and isinstance(readiness.summary, dict):
        word_total = readiness.summary.get('word_count')

    violations = []
    for rule in rules:
        field = rule.get('field')
        operator = rule.get('operator')
        expected = rule.get('value')
        triggered = False
        actual = None

        if field == 'word_count':
            actual = word_total
            if actual is None:
                continue
            actual = int(actual)
            expected = int(expected)
            triggered = {
                '>': actual > expected,
                '>=': actual >= expected,
                '<': actual < expected,
                '<=': actual <= expected,
                '==': actual == expected,
                '!=': actual != expected,
            }.get(operator, False)
        elif field == 'manuscript_type':
            actual = manuscript.manuscript_type
            if operator == '==':
                triggered = actual == str(expected)
            elif operator == '!=':
                triggered = actual != str(expected)
            elif operator == 'in':
                triggered = actual in {str(item) for item in expected or []}
            elif operator == 'not_in':
                triggered = actual not in {str(item) for item in expected or []}
        elif field == 'disclosure':
            actual = str(manuscript.disclosure or '')
            if operator == 'contains':
                triggered = str(expected).lower() in actual.lower()
            elif operator == 'not_contains':
                triggered = str(expected).lower() not in actual.lower()
            elif operator == 'empty':
                triggered = not actual.strip()
            elif operator == 'not_empty':
                triggered = bool(actual.strip())

        if triggered:
            violations.append({
                'field': field,
                'operator': operator,
                'value': expected,
                'actual': actual,
                'message': str(rule.get('message', '')).strip(),
            })
    return violations


def _submission_requirements_payload(item):
    config = item.venue_config
    specs = list((config.required_submission_items if config else []) or [])
    packet = item.packet if isinstance(item.packet, dict) else {}
    responses = packet.get('requirement_responses')
    if not isinstance(responses, dict):
        responses = {}
    file_map = {
        row.requirement_key: row
        for row in item.requirement_files.all()
    }

    output = []
    required_complete = True
    for spec in specs:
        key = str(spec.get('key', '')).strip()
        item_type = spec.get('type', 'text')
        value = responses.get(key)
        file_row = file_map.get(key)

        if item_type == 'file':
            completed = bool(file_row and file_row.file)
            display_value = None
        elif item_type == 'checkbox':
            completed = value is True
            display_value = bool(value)
        else:
            display_value = str(value or '').strip()
            completed = bool(display_value)

        required = bool(spec.get('required', True))
        if required and not completed:
            required_complete = False
        output.append({
            **spec,
            'value': display_value,
            'completed': completed,
            'file': {
                'name': file_row.original_filename,
                'bytes': file_row.file_bytes,
                'sha256': file_row.file_sha256,
                'uploaded_at': file_row.uploaded_at.isoformat(),
            } if file_row else None,
        })

    return {
        'configured': bool(specs),
        'complete': required_complete,
        'items': output,
    }


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
        'structured_desk_rejection_rules': config.structured_desk_rejection_rules,
        'required_submission_items': config.required_submission_items,
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
        'requirements': _submission_requirements_payload(item),
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
    author = None
    session = read_author_session(request)
    if session and session.get('aid'):
        try:
            author = Author.objects.get(id=session['aid'])
        except Author.DoesNotExist:
            pass

    if author and not author.email_verified:
        return JsonResponse({'detail': 'Email verification required before upload'}, status=403)

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
    access_token = secrets.token_urlsafe(32)

    item = Manuscript.objects.create(
        author_account=author,
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
        access_token_hash=_hash_author_token(access_token),
    )
    return JsonResponse({
        'manuscript': _manuscript_payload(item),
        'access_token': access_token,
    }, status=201)

def _send_author_verification(request, author):
    from django.core.signing import dumps

    token = dumps({'author_id': str(author.id)})
    verify_url = request.build_absolute_uri(f'/api/author/verify-email/?token={token}')
    _send_email(
        to=author.email,
        subject='Verify your author account',
        body=f'Please verify your email by clicking: {verify_url}',
    )


@csrf_exempt
@require_POST
def author_register(request):
    data = _json_body(request)
    email = str(data.get('email', '')).strip().lower()
    name = str(data.get('name', '')).strip()
    password = str(data.get('password', ''))

    if not email or not name or not password:
        return JsonResponse({'detail': 'Email, name, and password are required'}, status=400)
    
    if len(password) < 8:
        return JsonResponse({'detail': 'Password must be at least 8 characters'}, status=400)

    rh = remote_hash(request)
    from datetime import timedelta
    window_minutes = int(os.getenv('AUTHOR_REGISTER_WINDOW_MINUTES', '15'))
    max_regs = int(os.getenv('AUTHOR_REGISTER_MAX', '3'))
    recent_registrations = AuthorAuthEvent.objects.filter(
        remote_hash=rh, success=True, detail__action='register', occurred_at__gte=timezone.now() - timedelta(minutes=window_minutes)
    ).count()
    if recent_registrations >= max_regs:
        return JsonResponse({'detail': 'Too many registrations from this IP. Try again later.'}, status=429)

    if Author.objects.filter(email=email).exists():
        return JsonResponse({'detail': 'An account with this email already exists'}, status=409)

    author = Author.objects.create(
        email=email,
        name=name,
        password_hash=hash_password(password),
        email_verified=False
    )
    AuthorAuthEvent.objects.create(remote_hash=rh, success=True, detail={'action': 'register', 'email': email})

    verification_email_sent = True
    try:
        _send_author_verification(request, author)
    except Exception:
        verification_email_sent = False

    token, max_age = issue_author_session(author.id)
    response = JsonResponse({
        'id': str(author.id),
        'email': author.email,
        'name': author.name,
        'email_verified': author.email_verified,
        'verification_email_sent': verification_email_sent,
        'verification_warning': (
            None if verification_email_sent
            else 'Your account was created, but the verification email could not be sent. You can resend it from your author workspace.'
        ),
    }, status=201)
    set_author_session_cookie(response, token, max_age)
    return response


@csrf_exempt
@require_POST
def author_login(request):
    data = _json_body(request)
    email = str(data.get('email', '')).strip().lower()
    password = str(data.get('password', ''))

    if not email or not password:
        return JsonResponse({'detail': 'Email and password are required'}, status=400)

    rh = remote_hash(request)
    from datetime import timedelta
    window_minutes = int(os.getenv('AUTHOR_LOGIN_WINDOW_MINUTES', '15'))
    max_failures = int(os.getenv('AUTHOR_LOGIN_MAX_FAILURES', '10'))
    recent_failures = AuthorAuthEvent.objects.filter(
        remote_hash=rh, success=False, occurred_at__gte=timezone.now() - timedelta(minutes=window_minutes)
    ).count()
    if recent_failures >= max_failures:
        AuthorAuthEvent.objects.create(remote_hash=rh, success=False, detail={'reason': 'rate_limited', 'action': 'login'})
        return JsonResponse({'detail': 'Too many failed login attempts. Try again later.'}, status=429)

    try:
        author = Author.objects.get(email=email)
    except Author.DoesNotExist:
        AuthorAuthEvent.objects.create(remote_hash=rh, success=False, detail={'email': email, 'reason': 'invalid_credentials', 'action': 'login'})
        return JsonResponse({'detail': 'Invalid email or password'}, status=401)

    if not verify_password(password, author.password_hash):
        AuthorAuthEvent.objects.create(remote_hash=rh, success=False, detail={'email': email, 'reason': 'invalid_credentials', 'action': 'login'})
        return JsonResponse({'detail': 'Invalid email or password'}, status=401)

    AuthorAuthEvent.objects.create(remote_hash=rh, success=True, detail={'email': email, 'reason': 'ok', 'action': 'login'})

    token, max_age = issue_author_session(author.id)
    response = JsonResponse({
        'id': str(author.id),
        'email': author.email,
        'name': author.name,
        'email_verified': author.email_verified,
    })
    set_author_session_cookie(response, token, max_age)
    return response


@csrf_exempt
@require_POST
def author_logout(request):
    response = JsonResponse({'detail': 'Logged out'})
    clear_author_session_cookie(response)
    return response


@require_GET
def author_verify_email(request):
    token = request.GET.get('token')
    if not token:
        return JsonResponse({'detail': 'Token missing'}, status=400)
    
    from django.core.signing import loads, SignatureExpired, BadSignature
    try:
        data = loads(token, max_age=86400 * 7)
        author = Author.objects.get(id=data['author_id'])
        author.email_verified = True
        author.save(update_fields=['email_verified'])
        return JsonResponse({'detail': 'Email verified successfully'})
    except (SignatureExpired, BadSignature, Author.DoesNotExist):
        return JsonResponse({'detail': 'Invalid or expired token'}, status=400)


@csrf_exempt
@require_POST
@require_author
def author_resend_verification(request):
    try:
        author = Author.objects.get(id=request.flexee_author['aid'])
    except Author.DoesNotExist:
        return JsonResponse({'detail': 'Author not found'}, status=404)

    if author.email_verified:
        return JsonResponse({'ok': True, 'email_verified': True, 'detail': 'Email is already verified.'})

    from datetime import timedelta
    rh = remote_hash(request)
    window_minutes = int(os.getenv('AUTHOR_VERIFY_RESEND_WINDOW_MINUTES', '15'))
    max_attempts = int(os.getenv('AUTHOR_VERIFY_RESEND_MAX', '3'))
    recent = AuthorAuthEvent.objects.filter(
        remote_hash=rh,
        detail__action='resend_verification',
        occurred_at__gte=timezone.now() - timedelta(minutes=window_minutes),
    ).count()
    if recent >= max_attempts:
        return JsonResponse({'detail': 'Too many verification email requests. Try again later.'}, status=429)

    try:
        _send_author_verification(request, author)
    except Exception:
        AuthorAuthEvent.objects.create(
            remote_hash=rh,
            success=False,
            detail={'action': 'resend_verification', 'author_id': str(author.id)},
        )
        return JsonResponse({'detail': 'Verification email could not be sent. Please try again later.'}, status=503)

    AuthorAuthEvent.objects.create(
        remote_hash=rh,
        success=True,
        detail={'action': 'resend_verification', 'author_id': str(author.id)},
    )
    return JsonResponse({'ok': True, 'email_verified': False, 'detail': 'Verification email sent.'})


@require_GET
def author_session(request):
    session = read_author_session(request)
    if not session:
        return JsonResponse({'detail': 'Not logged in'}, status=401)
    
    try:
        author = Author.objects.get(id=session['aid'])
    except Author.DoesNotExist:
        response = JsonResponse({'detail': 'Author not found'}, status=401)
        clear_author_session_cookie(response)
        return response

    return JsonResponse({
        'id': str(author.id),
        'email': author.email,
        'name': author.name,
        'email_verified': author.email_verified,
    })

@require_GET
@require_author
def author_manuscripts_list(request):
    author_id = request.flexee_author['aid']
    items = Manuscript.objects.filter(author_account_id=author_id).order_by('-created_at')
    
    result = []
    for m in items:
        sub = m.venue_submissions.order_by('-created_at').first()
        payload = _manuscript_payload(m)
        if sub:
            payload['latest_submission'] = _submission_payload(sub)
        result.append(payload)
        
    return JsonResponse({'manuscripts': result})


@require_GET
def author_manuscript_detail(request, manuscript_id):
    try:
        item = Manuscript.objects.get(id=manuscript_id)
    except Manuscript.DoesNotExist:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)
    access_error = _author_access_error(request, item)
    if access_error:
        return access_error
    return JsonResponse({'manuscript': _manuscript_payload(item)})


@csrf_exempt
@require_POST
def author_run_readiness(request, manuscript_id):
    try:
        manuscript = Manuscript.objects.get(id=manuscript_id)
    except Manuscript.DoesNotExist:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)
    access_error = _author_access_error(request, manuscript)
    if access_error:
        return access_error

    assessment = ReadinessAssessment.objects.create(
        manuscript=manuscript,
        status='pending',
        engine_version='mechanical-v1',
    )

    try:
        text = load_manuscript_text(manuscript)
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


@csrf_exempt
@require_POST
def author_run_semantic_readiness(request, manuscript_id):
    try:
        manuscript = Manuscript.objects.get(id=manuscript_id)
    except Manuscript.DoesNotExist:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)
    access_error = _author_access_error(request, manuscript)
    if access_error:
        return access_error

    job, _ = _queue_unique_job(
        'semantic_readiness',
        manuscript.id,
        'review.tasks.run_semantic_readiness_task',
        manuscript.id,
    )

    return JsonResponse({'job_id': str(job.id), 'status': job.status}, status=202)


@require_GET
def author_readiness(request, manuscript_id):
    try:
        manuscript = Manuscript.objects.get(id=manuscript_id)
    except Manuscript.DoesNotExist:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)
    access_error = _author_access_error(request, manuscript)
    if access_error:
        return access_error
    item = manuscript.readiness_assessments.first()
    if not item:
        return JsonResponse({'detail': 'No readiness assessment exists for this manuscript'}, status=404)
    mechanical = manuscript.readiness_assessments.filter(
        engine_version__startswith='mechanical-'
    ).first()
    semantic = manuscript.readiness_assessments.filter(
        engine_version__startswith='author-agents-v1:semantic-readiness'
    ).first()
    return JsonResponse({
        'readiness': _readiness_payload(item),
        'mechanical_readiness': _readiness_payload(mechanical) if mechanical else None,
        'semantic_readiness': _readiness_payload(semantic) if semantic else None,
    })


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
    access_error = _author_access_error(request, manuscript)
    if access_error:
        return access_error

    latest_readiness = manuscript.readiness_assessments.filter(status='completed').first()
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
            manuscript_type_label = _normalise_label(
                dict(Manuscript.TYPE_CHOICES).get(manuscript.manuscript_type, manuscript.manuscript_type)
            )
            if accepted:
                if manuscript_type in accepted or manuscript_type_label in accepted:
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

            desk_violations = _structured_desk_rule_violations(
                manuscript,
                config,
                latest_readiness,
            )
            if desk_violations:
                eligibility = 'ineligible'
                for violation in desk_violations:
                    gaps.append(violation['message'])
                    evidence.append({
                        'source_type': 'venue_policy',
                        'source_locator': f'venue config v{config.version} · structured_desk_rejection_rules',
                        'claim': violation['message'],
                        'rule': violation,
                    })

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


@csrf_exempt
@require_POST
def author_run_semantic_matches(request, manuscript_id):
    try:
        manuscript = Manuscript.objects.get(id=manuscript_id)
    except Manuscript.DoesNotExist:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)
    access_error = _author_access_error(request, manuscript)
    if access_error:
        return access_error

    data = _json_body(request)
    venue_ids = data.get('venue_ids')
    if venue_ids is not None and not isinstance(venue_ids, list):
        return JsonResponse({'detail': 'venue_ids must be a JSON list when provided'}, status=400)

    job, _ = _queue_unique_job(
        'semantic_matches',
        manuscript.id,
        'review.tasks.run_semantic_matching_task',
        manuscript.id,
        venue_ids,
    )

    return JsonResponse({'job_id': str(job.id), 'status': job.status}, status=202)


@require_GET
def author_matches(request, manuscript_id):
    try:
        manuscript = Manuscript.objects.get(id=manuscript_id)
    except Manuscript.DoesNotExist:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)
    access_error = _author_access_error(request, manuscript)
    if access_error:
        return access_error
    items = manuscript.venue_matches.select_related('venue', 'venue__organization', 'venue_config').all()
    return JsonResponse({'matches': [_match_payload(item) for item in items]})


@csrf_exempt
@require_POST
def author_create_submission(request, manuscript_id):
    try:
        manuscript = Manuscript.objects.get(id=manuscript_id)
    except Manuscript.DoesNotExist:
        return JsonResponse({'detail': 'Manuscript not found'}, status=404)
    access_error = _author_access_error(request, manuscript)
    if access_error:
        return access_error

    data = _json_body(request)
    venue_id = data.get('venue_id')
    if not venue_id:
        return JsonResponse({'detail': 'venue_id is required'}, status=400)
    try:
        venue = Venue.objects.get(id=venue_id, active=True)
    except (Venue.DoesNotExist, ValueError):
        return JsonResponse({'detail': 'Active venue not found'}, status=404)

    match = manuscript.venue_matches.filter(venue=venue).first()
    if match and match.eligibility == 'ineligible':
        return JsonResponse(
            {
                'detail': 'This venue is not eligible for the current manuscript under its deterministic routing rules',
                'reasons': match.gaps,
            },
            status=409,
        )

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


@csrf_exempt
@require_POST
def author_run_venue_assessment(request, submission_id):
    try:
        submission = VenueSubmission.objects.select_related(
            'manuscript', 'venue', 'venue__organization', 'venue_config'
        ).get(id=submission_id)
    except VenueSubmission.DoesNotExist:
        return JsonResponse({'detail': 'Venue submission not found'}, status=404)
    access_error = _author_access_error(request, submission.manuscript)
    if access_error:
        return access_error

    try:
        import json
        from django.utils import timezone
        data = json.loads(request.body)
        override = data.get('override_anonymization', False)
        if override:
            packet = dict(submission.packet or {})
            packet['anonymization_override'] = True
            packet['anonymization_override_time'] = timezone.now().isoformat()
            submission.packet = packet
            submission.save(update_fields=['packet'])
    except Exception:
        pass

    job, _ = _queue_unique_job(
        'venue_assessment',
        submission.id,
        'review.tasks.run_venue_assessment_task',
        submission.id,
    )

    return JsonResponse({'job_id': str(job.id), 'status': job.status}, status=202)


@require_GET
def author_submission_detail(request, submission_id):
    try:
        item = VenueSubmission.objects.select_related(
            'venue', 'venue__organization', 'venue_config'
        ).prefetch_related('evidence_findings', 'requirement_files').get(id=submission_id)
    except VenueSubmission.DoesNotExist:
        return JsonResponse({'detail': 'Venue submission not found'}, status=404)
    access_error = _author_access_error(request, item.manuscript)
    if access_error:
        return access_error
    return JsonResponse({'submission': _submission_payload(item)})


@csrf_exempt
@require_POST
def author_save_submission_requirements(request, submission_id):
    try:
        item = VenueSubmission.objects.select_related(
            'manuscript', 'venue', 'venue_config'
        ).prefetch_related('requirement_files').get(id=submission_id)
    except VenueSubmission.DoesNotExist:
        return JsonResponse({'detail': 'Venue submission not found'}, status=404)
    access_error = _author_access_error(request, item.manuscript)
    if access_error:
        return access_error
    if item.status not in {'draft', 'packet_ready'}:
        return JsonResponse(
            {'detail': 'Submission requirements can only be changed before formal submission'},
            status=409,
        )

    state = _submission_requirements_payload(item)
    specs = {spec['key']: spec for spec in state['items']}
    data = _json_body(request)
    incoming = data.get('responses')
    if not isinstance(incoming, dict):
        return JsonResponse({'detail': 'responses must be a JSON object'}, status=400)

    packet = dict(item.packet or {})
    responses = packet.get('requirement_responses')
    if not isinstance(responses, dict):
        responses = {}

    for key, raw_value in incoming.items():
        spec = specs.get(str(key))
        if not spec:
            return JsonResponse({'detail': f'Unknown submission requirement: {key}'}, status=400)
        if spec['type'] == 'file':
            return JsonResponse(
                {'detail': f'{spec["label"]} must be uploaded as a file'},
                status=400,
            )
        if spec['type'] == 'checkbox':
            if not isinstance(raw_value, bool):
                return JsonResponse(
                    {'detail': f'{spec["label"]} must be true or false'},
                    status=400,
                )
            responses[spec['key']] = raw_value
        else:
            value = str(raw_value or '').strip()
            if len(value) > int(spec.get('max_length', 4000)):
                return JsonResponse(
                    {'detail': f'{spec["label"]} exceeds the configured maximum length'},
                    status=400,
                )
            responses[spec['key']] = value

    packet['requirement_responses'] = responses
    item.packet = packet
    item.save(update_fields=['packet', 'updated_at'])
    item = VenueSubmission.objects.select_related(
        'manuscript', 'venue', 'venue_config'
    ).prefetch_related('requirement_files').get(id=item.id)
    return JsonResponse({'requirements': _submission_requirements_payload(item)})


@csrf_exempt
@require_POST
def author_upload_submission_requirement(request, submission_id, requirement_key):
    try:
        item = VenueSubmission.objects.select_related(
            'manuscript', 'venue', 'venue_config'
        ).prefetch_related('requirement_files').get(id=submission_id)
    except VenueSubmission.DoesNotExist:
        return JsonResponse({'detail': 'Venue submission not found'}, status=404)
    access_error = _author_access_error(request, item.manuscript)
    if access_error:
        return access_error
    if item.status not in {'draft', 'packet_ready'}:
        return JsonResponse(
            {'detail': 'Submission requirement files can only be changed before formal submission'},
            status=409,
        )

    state = _submission_requirements_payload(item)
    spec = next(
        (row for row in state['items'] if row.get('key') == requirement_key),
        None,
    )
    if not spec:
        return JsonResponse({'detail': 'Submission requirement not found'}, status=404)
    if spec.get('type') != 'file':
        return JsonResponse({'detail': 'This requirement does not accept a file'}, status=400)

    uploaded = request.FILES.get('file')
    if not uploaded:
        return JsonResponse({'detail': 'file is required'}, status=400)

    max_bytes = int(os.getenv('MAX_SUBMISSION_ITEM_BYTES', str(10 * 1024 * 1024)))
    if uploaded.size > max_bytes:
        return JsonResponse(
            {'detail': f'Requirement file exceeds the {max_bytes}-byte upload limit'},
            status=413,
        )

    original_filename = os.path.basename(str(uploaded.name or 'attachment'))
    extension = os.path.splitext(original_filename)[1].lower()
    allowed_extensions = {
        '.pdf', '.doc', '.docx', '.txt', '.rtf',
        '.csv', '.xls', '.xlsx', '.png', '.jpg', '.jpeg',
    }
    if extension not in allowed_extensions:
        return JsonResponse(
            {'detail': 'Requirement files must be PDF, Office document, text, CSV, or image files'},
            status=400,
        )

    digest = hashlib.sha256()
    for chunk in uploaded.chunks():
        digest.update(chunk)
    uploaded.seek(0)

    existing = SubmissionRequirementFile.objects.filter(
        venue_submission=item,
        requirement_key=requirement_key,
    ).first()
    if existing and existing.file:
        existing.file.delete(save=False)

    row, _ = SubmissionRequirementFile.objects.update_or_create(
        venue_submission=item,
        requirement_key=requirement_key,
        defaults={
            'original_filename': original_filename[:500],
            'file': uploaded,
            'file_bytes': uploaded.size,
            'file_sha256': digest.hexdigest(),
        },
    )

    item = VenueSubmission.objects.select_related(
        'manuscript', 'venue', 'venue_config'
    ).prefetch_related('requirement_files').get(id=item.id)
    return JsonResponse({
        'file': {
            'name': row.original_filename,
            'bytes': row.file_bytes,
            'sha256': row.file_sha256,
        },
        'requirements': _submission_requirements_payload(item),
    })


@csrf_exempt
@require_POST
def author_submit_packet(request, submission_id):
    try:
        item = VenueSubmission.objects.select_related(
            'manuscript', 'venue', 'venue_config'
        ).prefetch_related('requirement_files').get(id=submission_id)
    except VenueSubmission.DoesNotExist:
        return JsonResponse({'detail': 'Venue submission not found'}, status=404)
    access_error = _author_access_error(request, item.manuscript)
    if access_error:
        return access_error

    if item.status != 'packet_ready':
        return JsonResponse(
            {'detail': 'Complete the venue assessment and prepare the packet before submission'},
            status=409,
        )

    requirements = _submission_requirements_payload(item)
    if not requirements['complete']:
        return JsonResponse(
            {
                'detail': 'Complete all required venue submission items before submission',
                'requirements': requirements,
            },
            status=409,
        )

    item.status = 'submitted'
    item.submitted_at = timezone.now()
    item.save(update_fields=['status', 'submitted_at', 'updated_at'])

    # ── Confirmation email to author ────────────────────────────────────────────
    manuscript = item.manuscript
    author_email = (manuscript.author_email or '').strip()
    if author_email:
        try:
            venue_name = item.venue.name if item.venue_id else 'the selected journal'
            subject = f'Submission Received – {manuscript.title}'
            body = (
                f'Dear {manuscript.author_name},\n\n'
                f'We have received your manuscript submission "{manuscript.title}" '
                f'to {venue_name}.\n\n'
                f'Your submission is now in the editorial queue. The editorial team will '
                f'review your work and get back to you with a decision. You can check the '
                f'status of your submission at any time by logging into the Author Workspace.\n\n'
                f'Submission reference: {submission_id}\n\n'
                f'Thank you for submitting to {venue_name}.\n\n'
                f'Best regards,\nFlexee Editorial Team'
            )
            _send_email(author_email, subject, body)
        except Exception:
            # Email errors must never block the submission from being recorded.
            pass

    return JsonResponse({'submission': _submission_payload(item)})


@csrf_exempt
@require_POST
@transaction.atomic
def author_transfer_submission(request, submission_id):
    try:
        source = VenueSubmission.objects.select_for_update().get(id=submission_id)
    except VenueSubmission.DoesNotExist:
        return JsonResponse({'detail': 'Venue submission not found'}, status=404)
    access_error = _author_access_error(request, source.manuscript)
    if access_error:
        return access_error

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
    if source.status not in {'rejected', 'withdrawn'}:
        return JsonResponse(
            {'detail': 'Transfer is available after a rejection or withdrawal'},
            status=409,
        )

    target_config = _active_config(target_venue)
    latest_readiness = source.manuscript.readiness_assessments.filter(status='completed').first()
    if target_config:
        violations = _structured_desk_rule_violations(
            source.manuscript,
            target_config,
            latest_readiness,
        )
        if violations:
            return JsonResponse(
                {
                    'detail': 'The target venue has deterministic desk-rejection rules that this manuscript does not satisfy',
                    'violations': violations,
                },
                status=409,
            )

    target = VenueSubmission.objects.create(
        manuscript=source.manuscript,
        venue=target_venue,
        venue_config=target_config,
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
        if not request.editor_user.platform_superuser:
            org_ids = request.editor_user.memberships.values_list('organization_id', flat=True)
            items = items.filter(organization_id__in=org_ids)
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
    organization_name = str(data.get('organization_name', '')).strip()
    if organization_id:
        try:
            organization = Organization.objects.get(id=organization_id)
        except (Organization.DoesNotExist, ValueError):
            return JsonResponse({'detail': 'Organization not found'}, status=404)
        if not check_org_access(request.editor_user, organization.id, ['owner']):
            return JsonResponse({'detail': 'Forbidden'}, status=403)
    elif organization_name:
        # Creating a new tenant is a platform operation. Authorize before the
        # insert so a rejected request cannot leave an orphan Organization row.
        if not request.editor_user.platform_superuser:
            return JsonResponse({'detail': 'Forbidden'}, status=403)
        organization = Organization.objects.create(
            name=organization_name,
            organization_type=str(data.get('organization_type', 'other')).strip() or 'other',
        )
    elif not request.editor_user.platform_superuser:
        return JsonResponse({'detail': 'organization_id is required'}, status=400)

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
        if not check_org_access(request.editor_user, venue.organization_id, ['owner', 'editor', 'viewer']):
            return JsonResponse({'detail': 'Forbidden'}, status=403)
        config = _active_config(venue)
        if not config:
            return JsonResponse({'detail': 'No venue agent configuration exists'}, status=404)
        return JsonResponse({'venue': _venue_payload(venue, include_config=False), 'config': _venue_config_payload(config)})
    if request.method != 'POST':
        return JsonResponse({'detail': 'Method not allowed'}, status=405)
    if not check_org_access(request.editor_user, venue.organization_id, ['owner']):
        return JsonResponse({'detail': 'Forbidden'}, status=403)

    data = _json_body(request)
    try:
        required_submission_items = _normalise_required_submission_items(
            data.get('required_submission_items', [])
        )
        structured_desk_rejection_rules = _normalise_structured_desk_rules(
            data.get('structured_desk_rejection_rules', [])
        )
    except ValueError as exc:
        return JsonResponse({'detail': str(exc)}, status=400)

    with transaction.atomic():
        venue = Venue.objects.select_for_update().get(id=venue.id)
        current = venue.agent_configs.order_by('-version').first()
        next_version = (current.version + 1) if current else 1
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
            structured_desk_rejection_rules=structured_desk_rejection_rules,
            required_submission_items=required_submission_items,
            deadlines=data.get('deadlines') if isinstance(data.get('deadlines'), dict) else {},
            submission_capacity=data.get('submission_capacity') if isinstance(data.get('submission_capacity'), dict) else {},
            current_demand=data.get('current_demand') if isinstance(data.get('current_demand'), dict) else {},
            config_notes=str(data.get('config_notes', '')).strip(),
        )
        venue.agent_configs.filter(active=True).exclude(id=config.id).update(active=False)

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
    if not check_org_access(request.editor_user, venue.organization_id, ['owner', 'editor']):
        return JsonResponse({'detail': 'Forbidden'}, status=403)

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


@require_GET
def author_job_status(request, job_id):
    try:
        job = ReviewJob.objects.get(id=job_id)
    except ReviewJob.DoesNotExist:
        return JsonResponse({'detail': 'Job not found'}, status=404)
        
    if job.job_type in ('semantic_readiness', 'semantic_matches'):
        try:
            item = Manuscript.objects.get(id=job.reference_id)
        except Manuscript.DoesNotExist:
            return JsonResponse({'detail': 'Job not found'}, status=404)
        access_error = _author_access_error(request, item)
        if access_error:
            return access_error
    elif job.job_type == 'venue_assessment':
        try:
            item = VenueSubmission.objects.select_related('manuscript').get(id=job.reference_id)
        except VenueSubmission.DoesNotExist:
            return JsonResponse({'detail': 'Job not found'}, status=404)
        access_error = _author_access_error(request, item.manuscript)
        if access_error:
            return access_error
    else:
        # Public-review jobs have their own UUID-based status endpoint and must
        # not be exposed through the author job-number endpoint.
        return JsonResponse({'detail': 'Job not found'}, status=404)

    public_error = None
    if job.status == 'failed':
        public_error = (
            'Background processing timed out. Please try again.'
            if 'timed out' in str(job.error_message or '').lower()
            else 'The background job could not be completed. Please try again.'
        )

    return JsonResponse({
        'id': str(job.id),
        'status': job.status,
        'progress': job.progress,
        'result_reference': job.reference_id,
        'error': public_error,
    })
