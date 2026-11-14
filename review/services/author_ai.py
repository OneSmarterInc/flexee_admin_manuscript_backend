"""Request/task-local author routing. No credentials in environment variables or queues."""
from contextvars import ContextVar
from contextlib import contextmanager
from functools import wraps
from dataclasses import dataclass, field
import json
import httpx
from django.views.decorators.debug import sensitive_variables

DEFAULT_MODEL = 'qwen3:1.7b'

# Curated text models; friendly names are exposed to the dashboard.
MODEL_CHOICES = [
    {'provider': p, 'model': m, 'name': n} for p, m, n in [
        ('default', DEFAULT_MODEL, 'Qwen 1.7b (default, local)'),
        ('openai', 'gpt-5-mini', 'OpenAI GPT-5 Mini'),
        ('gemini', 'gemini-4-argon', 'Gemini 4 Argon'),
        ('gemini', 'gemini-4-carbon', 'Gemini 4 Carbon'),
        ('claude', 'claude-haiku-5-5', 'Claude Haiku 5.5'),
        ('claude', 'claude-sonnet-5-5', 'Claude Sonnet 5.5'),
        ('claude', 'claude-opus-5-5', 'Claude Opus 5.5'),
        ('grok', 'grok-4.7', 'Grok 4.7'),
        ('groq', 'openai/gpt-oss-120b', 'Groq GPT-OSS 120B'),
        ('deepseek', 'deepseek-flash', 'DeepSeek Flash'),
        ('deepseek', 'deepseek-v4-pro', 'DeepSeek V4 Pro'),
    ]
]

_current = ContextVar('author_ai_selection', default=None)

class AuthorProviderConfigurationError(RuntimeError):
    """A credential/configuration error that must not be retried automatically."""


@dataclass(frozen=True)
class Selection:
    author_id: str
    provider: str = 'default'
    model: str = DEFAULT_MODEL
    key: str = field(default='', repr=False)
    revision: str = ''


def current_selection():
    return _current.get()


def selection_identity():
    s = current_selection()
    return [s.author_id, s.provider, s.model, s.revision] if s else None


@contextmanager
def author_scope(author_id):
    from ..models import AuthorAISettings
    row = AuthorAISettings.objects.filter(author_id=author_id).first() if author_id else None
    selection = (Selection(str(author_id), row.provider, row.model, row.api_key, str(row.revision))
                 if row else Selection(str(author_id or '')))
    token = _current.set(selection)
    try:
        yield selection
    finally:
        _current.reset(token)


def author_analysis(fn):
    @wraps(fn)
    def wrapped(subject, *args, **kwargs):
        manuscript = getattr(subject, 'manuscript', subject)
        with author_scope(manuscript.author_account_id):
            return fn(subject, *args, **kwargs)
    return wrapped


@sensitive_variables()
def custom_chat(selection, prompt, max_tokens, timeout):
    """Only fixed official endpoints; never fall back to platform/another user's credentials."""
    if not selection.key:
        raise RuntimeError('Your AI provider key is missing. Update AI settings in your dashboard.')
    headers = {'Content-Type': 'application/json'}
    messages = [{'role': 'user', 'content': prompt + '\nReturn a complete JSON object only.'}]
    limit = max(2048, max_tokens)
    if selection.provider == 'claude':
        url = 'https://api.anthropic.com/v1/messages'
        headers.update({'x-api-key': selection.key, 'anthropic-version': '2023-06-01'})
        body = {'model': selection.model, 'messages': messages, 'max_tokens': limit}
    else:
        urls = {'groq': 'https://api.groq.com/openai/v1/chat/completions',
                'grok': 'https://api.x.ai/v1/chat/completions',
                'deepseek': 'https://api.deepseek.com/chat/completions',
                'openai': 'https://api.openai.com/v1/chat/completions',
                'gemini': 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'}
        if selection.provider not in urls:
            raise RuntimeError('Unsupported author AI provider.')
        url = urls[selection.provider]
        headers['Authorization'] = 'Bearer ' + selection.key
        body = {'model': selection.model, 'messages': messages, 'response_format': {'type': 'json_object'}}
        body['max_completion_tokens' if selection.provider == 'openai' else 'max_tokens'] = limit
    if selection.provider == 'openai' and selection.model == 'gpt-5-mini':
        body['reasoning_effort'] = 'minimal'
    if selection.provider == 'groq':
        body['reasoning_effort'] = 'low'
        body['include_reasoning'] = False
    # Suppress provider exception bodies: they may contain credentials or manuscript text.
    try:
        response = httpx.post(url, headers=headers, json=body, timeout=timeout, follow_redirects=False)
    except Exception:
        raise RuntimeError('Your AI provider could not be reached. Please try again.') from None
    if response.status_code in (400, 401, 403, 404):
        raise AuthorProviderConfigurationError(f'Your selected AI provider rejected the request (HTTP {response.status_code}). Update your API key or provider settings before retrying.')
    if response.status_code >= 300:
        raise RuntimeError(f'Your AI provider rejected the request (HTTP {response.status_code}). Check your key, model and quota.')
    try:
        data = response.json()
        if selection.provider == 'claude':
            if data.get('stop_reason') == 'max_tokens':
                raise ValueError('truncated')
            raw = ''.join(v['text'] for v in data['content'] if v.get('type') == 'text')
        else:
            choice = data['choices'][0]
            if choice.get('finish_reason') != 'stop':
                raise ValueError('incomplete')
            raw = choice['message']['content']
        if not isinstance(json.loads(raw), dict):
            raise ValueError('not an object')
    except Exception:
        raise RuntimeError('Your AI provider returned an incomplete or invalid JSON response.') from None
    return selection.model, raw
