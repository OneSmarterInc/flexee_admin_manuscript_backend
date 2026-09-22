import json
import os
import zipfile
from io import BytesIO

import httpx
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .auth import require_admin
from .models import ReviewEvent, Submission
from .services.review_engine import extract_text, word_count


ANTHROPIC_API_URL = 'https://api.anthropic.com/v1/messages'
ANTHROPIC_VERSION = '2023-06-01'
DEFAULT_API_MODEL = 'claude-haiku-4-5-20251001'


def _json_body(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except Exception:
        return {}


def _extract_submission_text(submission):
    if not submission.manuscript_file:
        raise ValueError('No manuscript file is attached to this submission.')

    filename = submission.manuscript_filename or ''
    with submission.manuscript_file.open('rb') as fh:
        content = fh.read()

    if filename.lower().endswith('.zip'):
        parts = []
        with zipfile.ZipFile(BytesIO(content)) as zf:
            entries = [
                name for name in zf.namelist()
                if name.lower().endswith(('.docx', '.pdf', '.md'))
                and not name.startswith('__MACOSX')
                and not os.path.basename(name).startswith('.')
            ]
            if not entries:
                raise ValueError('No .docx, .pdf, or .md file found inside the ZIP archive.')
            for index, entry in enumerate(entries, start=1):
                base = os.path.basename(entry)
                try:
                    text = extract_text(zf.read(entry), base)
                except Exception:
                    text = ''
                if text.strip():
                    parts.append(f'# ZIP Document {index}: {base}\nSource path: {entry}\n\n{text.strip()}')
        combined = '\n\n---\n\n'.join(parts).strip()
        if not combined:
            raise ValueError('No extractable text found inside the ZIP archive.')
        return combined

    return extract_text(content, filename)


def _anthropic_summary(*, api_key, model, submission, text):
    max_chars = int(os.getenv('API_SUMMARY_MAX_CHARS', '180000'))
    truncated = len(text) > max_chars
    manuscript_text = text[:max_chars]

    prompt = f"""
You are helping an editor recover a manuscript review summary after the local model failed.
Use only the supplied manuscript text and metadata. Do not invent facts.

Return ONLY JSON with this shape:
{{
  "editor_summary": "Editor Summary\\n\\n1. Structural Findings\\n   - ...\\n\\n2. Rubric Findings\\n   - ...\\n\\n3. Key Gaps / Issues\\n   - ...\\n\\n4. Overall Review Conclusion\\n   - ...",
  "author_letter": "A short polite note to the author."
}}

Metadata:
- Title: {submission.title}
- Author: {submission.author_name}
- Type: {submission.kind}
- Declared simulation: {submission.declared_sim or 'N/A'}
- Disclosure: {submission.disclosure or 'N/A'}
- Text was truncated before API call: {'yes' if truncated else 'no'}

Manuscript text:
{manuscript_text}
""".strip()

    response = httpx.post(
        ANTHROPIC_API_URL,
        headers={
            'content-type': 'application/json',
            'x-api-key': api_key,
            'anthropic-version': ANTHROPIC_VERSION,
        },
        json={
            'model': model,
            'max_tokens': int(os.getenv('API_SUMMARY_MAX_TOKENS', '1800')),
            'temperature': 0.2,
            'messages': [{'role': 'user', 'content': prompt}],
        },
        timeout=float(os.getenv('API_SUMMARY_TIMEOUT', '300')),
    )
    if response.status_code >= 400:
        raise RuntimeError(f'API summary request failed ({response.status_code}): {response.text[:500]}')

    payload = response.json()
    content = payload.get('content') or []
    text_parts = [part.get('text', '') for part in content if isinstance(part, dict) and part.get('type') == 'text']
    raw = '\n'.join(text_parts).strip()
    if raw.startswith('```'):
        raw = raw.strip('`').strip()
        if raw.lower().startswith('json'):
            raw = raw[4:].strip()
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError('API summary response was not a JSON object.')
    editor_summary = str(parsed.get('editor_summary') or '').strip()
    author_letter = str(parsed.get('author_letter') or '').strip()
    if not editor_summary:
        raise RuntimeError('API summary response did not include editor_summary.')
    if not author_letter:
        author_letter = 'The editor summary was regenerated using an API fallback for human review.'
    return editor_summary, author_letter, truncated


@csrf_exempt
@require_POST
@require_admin
def admin_submission_api_summary(request, submission_id):
    try:
        submission = Submission.objects.get(id=submission_id)
    except Submission.DoesNotExist:
        return JsonResponse({'detail': 'Submission not found'}, status=404)

    data = _json_body(request)
    api_key = str(data.get('api_key') or os.getenv('ANTHROPIC_API_KEY', '')).strip()
    model = str(data.get('model') or os.getenv('ANTHROPIC_MODEL', DEFAULT_API_MODEL)).strip() or DEFAULT_API_MODEL
    if not api_key:
        return JsonResponse({'detail': 'API key is required. Paste a temporary key or configure ANTHROPIC_API_KEY.'}, status=400)

    try:
        source_text = _extract_submission_text(submission)
        editor_summary, author_letter, truncated = _anthropic_summary(
            api_key=api_key,
            model=model,
            submission=submission,
            text=source_text,
        )
        submission.status = 'completed'
        submission.completed_at = timezone.now()
        submission.model = f'{model} (api summary recovery)'
        submission.total_words = word_count(source_text)
        submission.editor_summary = editor_summary
        submission.author_letter = author_letter
        submission.error = None
        if not submission.decision:
            submission.decision = 'REFER_TO_HUMAN_WITH_FLAGS'
        record = submission.review_record if isinstance(submission.review_record, dict) else {}
        record['api_summary_recovery'] = {
            'model': model,
            'truncated': truncated,
            'total_words': submission.total_words,
            'generated_at': timezone.now().isoformat(),
        }
        submission.review_record = record
        submission.save(update_fields=[
            'status', 'completed_at', 'model', 'total_words', 'editor_summary',
            'author_letter', 'error', 'decision', 'review_record', 'updated_at',
        ])
        ReviewEvent.objects.create(
            submission=submission,
            event_type='api_summary_recovered',
            detail={'model': model, 'truncated': truncated, 'total_words': submission.total_words},
        )
        return JsonResponse({
            'ok': True,
            'id': str(submission.id),
            'status': submission.status,
            'decision': submission.decision,
            'model': submission.model,
            'total_words': submission.total_words,
            'editor_summary': submission.editor_summary,
            'author_letter': submission.author_letter,
        })
    except Exception as error:
        detail = {'type': error.__class__.__name__, 'message': str(error)}
        submission.error = detail
        submission.save(update_fields=['error', 'updated_at'])
        ReviewEvent.objects.create(submission=submission, event_type='api_summary_failed', detail=detail)
        return JsonResponse({'detail': str(error), 'error': detail}, status=502)
