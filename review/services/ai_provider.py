"""
AI provider abstraction for the Flexee author agents pipeline.

Provider selection is controlled by the AI_PROVIDER env var:
  - "anthropic"  → uses the Anthropic Messages API (claude-haiku or configured model)
  - "ollama"     → uses a local Ollama instance (qwen2.5 or configured model)
  - "auto"       → tries Anthropic first, falls back to Ollama, then to the
                   deterministic fallback
  - "mock"       → returns deterministic fake JSON for CI / offline testing

The caller always gets (model_name: str, json_text: str) or a RuntimeError.
"""
from __future__ import annotations

import json
import os
import re

from .local_llm import ollama_chat_json


AI_PROVIDER = os.getenv('AI_PROVIDER', 'auto').strip().lower()


# ---------------------------------------------------------------------------
# Anthropic provider
# ---------------------------------------------------------------------------

def _anthropic_chat_json(prompt: str, *, max_tokens: int = 1100, timeout: float = 120.0) -> tuple[str, str]:
    """
    Call the Anthropic Messages API and return (model_name, json_text).
    Raises RuntimeError if Anthropic is unavailable or returns bad JSON.
    """
    try:
        import anthropic as _anthropic_sdk
    except ImportError as exc:
        raise RuntimeError(
            'The anthropic Python package is not installed. '
            'Run: pip install anthropic'
        ) from exc

    api_key = os.getenv('ANTHROPIC_API_KEY', '').strip()
    if not api_key:
        raise RuntimeError(
            'ANTHROPIC_API_KEY is not set. Cannot use Anthropic provider.'
        )

    model = os.getenv('ANTHROPIC_MODEL', 'claude-haiku-4-5-20251001').strip()

    client = _anthropic_sdk.Anthropic(api_key=api_key, timeout=timeout)

    system = (
        'You are an expert scholarly manuscript analyst. '
        'You always respond with valid JSON exactly matching the requested schema. '
        'Do not add prose before or after the JSON object. '
        'Do not wrap JSON in markdown code fences.'
    )

    try:
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{'role': 'user', 'content': prompt}],
        )
    except Exception as exc:
        raise RuntimeError(f'Anthropic API call failed: {exc}') from exc

    raw = ''
    for block in message.content:
        if hasattr(block, 'text'):
            raw += block.text

    # Strip any accidental markdown fences the model may add
    raw = _strip_fences(raw.strip())
    if not raw:
        raise RuntimeError('Anthropic returned an empty response.')
    return model, raw


def _strip_fences(text: str) -> str:
    """Remove ```json ... ``` wrapping that some models add."""
    text = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.I)
    text = re.sub(r'\s*```\s*$', '', text)
    return text.strip()


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def ai_chat_json(prompt: str, *, max_tokens: int = 1100, timeout: float = 240.0, force_provider: str = None) -> tuple[str, str]:
    """
    Dispatch to the configured AI provider and return (model_name, json_text).

    Raises RuntimeError if all configured providers fail.
    """
    provider = force_provider or AI_PROVIDER

    if provider == 'mock':
        return 'mock', '{}'

    if provider == 'anthropic':
        return _anthropic_chat_json(prompt, max_tokens=max_tokens, timeout=timeout)

    if provider == 'ollama':
        return ollama_chat_json(prompt, max_tokens=max_tokens, timeout=timeout)

    # "auto": try Anthropic first (cloud), fall back to Ollama (local)
    errors: list[str] = []

    api_key = os.getenv('ANTHROPIC_API_KEY', '').strip()
    if api_key:
        try:
            return _anthropic_chat_json(prompt, max_tokens=max_tokens, timeout=timeout)
        except Exception as exc:
            errors.append(f'Anthropic: {exc}')

    try:
        return ollama_chat_json(prompt, max_tokens=max_tokens, timeout=timeout)
    except Exception as exc:
        errors.append(f'Ollama: {exc}')

    raise RuntimeError(
        'All AI providers failed. '
        + ' | '.join(errors)
    )


def ai_available() -> bool:
    """Return True if at least one AI provider is likely reachable."""
    provider = AI_PROVIDER
    if provider == 'mock':
        return True
    if provider == 'anthropic':
        return bool(os.getenv('ANTHROPIC_API_KEY', '').strip())
    if provider == 'ollama':
        try:
            import httpx
            base = os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/')
            r = httpx.get(f'{base}/api/tags', timeout=3.0)
            return r.status_code == 200
        except Exception:
            return False
    # auto: either Anthropic key or Ollama reachable
    if os.getenv('ANTHROPIC_API_KEY', '').strip():
        return True
    try:
        import httpx
        base = os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/')
        r = httpx.get(f'{base}/api/tags', timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False
