import re
import uuid
from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from .auth import require_author
from .json_object import json_object
from .models import AuthorAISettings
from .services.author_ai import DEFAULT_MODEL, MODEL_CHOICES


def public_settings(row):
    return {'provider': row.provider if row else 'default',
            'model': row.model if row else DEFAULT_MODEL,
            'has_key': bool(row and row.api_key), 'default_model': DEFAULT_MODEL,
            'models': MODEL_CHOICES}


@never_cache
@require_http_methods(['GET', 'POST', 'DELETE'])
@require_author
@sensitive_variables('data', 'key', 'row')
def author_ai_settings(request):
    # No author IDs accepted from body, URL or manuscript tokens.
    author_id = request.flexee_author['aid']
    if request.method == 'GET':
        return JsonResponse(public_settings(AuthorAISettings.objects.filter(author_id=author_id).first()))
    if request.method == 'DELETE':
        AuthorAISettings.objects.filter(author_id=author_id).delete()
        return JsonResponse(public_settings(None))
    if len(request.body) > 8192:
        return JsonResponse({'detail': 'Settings request is too large.'}, status=400)
    try:
        data = json_object(request.body)
    except (ValueError, TypeError):
        return JsonResponse({'detail': 'Invalid settings.'}, status=400)
    if set(data) - {'provider', 'model', 'api_key'}:
        return JsonResponse({'detail': 'Unsupported settings fields.'}, status=400)
    provider = data.get('provider')
    # The dashboard submits only a provider and key; select its curated default.
    # Explicit model values remain supported for older clients.
    model = data.get('model', next((c['model'] for c in MODEL_CHOICES if c['provider'] == provider), ''))
    key = data.get('api_key', '')
    if provider not in ('default', 'openai', 'claude', 'gemini', 'grok', 'groq', 'deepseek'):
        return JsonResponse({'detail': 'Choose a supported provider.'}, status=400)
    if not isinstance(key, str) or len(key) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        return JsonResponse({'detail': 'Enter a valid API key without spaces.'}, status=400)
    if provider != 'default' and (not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}', model)):
        return JsonResponse({'detail': 'Enter the provider model ID.'}, status=400)
    with transaction.atomic():
        # Serialize settings updates for this account, including first creation.
        from .models import Author
        Author.objects.select_for_update().get(pk=author_id)
        row = AuthorAISettings.objects.filter(author_id=author_id).first()
        if provider == 'default':
            if row: row.delete()
            return JsonResponse(public_settings(None))
        if not any(c['provider'] == provider and c['model'] == model for c in MODEL_CHOICES) and not (row and row.provider == provider and row.model == model):
            return JsonResponse({'detail': 'Choose a supported model from the menu.'}, status=400)
        if not key and (not row or row.provider != provider or not row.api_key):
            return JsonResponse({'detail': 'An API key is required for this provider.'}, status=400)
        row = row or AuthorAISettings(author_id=author_id)
        row.provider = provider
        row.model = model
        if key: row.api_key = key
        row.revision = uuid.uuid4()
        row.save()
    return JsonResponse(public_settings(row))
