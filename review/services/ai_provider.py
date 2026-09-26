"""
AI provider abstraction for the Flexee author agents pipeline.

All provider calls flow through this module so token usage, estimated cost, and
configured budget ceilings are enforced consistently.
"""
from __future__ import annotations

import os
import re

from ..ai_usage import (
    AIBudgetExceeded,
    AIUsageConfigurationError,
    complete_ai_call,
    fail_ai_call,
    reserve_ai_call,
)
from .local_llm import estimate_prompt_tokens, ollama_chat_json


ANTHROPIC_SYSTEM = (
    'You are an expert scholarly manuscript analyst. '
    'You always respond with valid JSON exactly matching the requested schema. '
    'Do not add prose before or after the JSON object. '
    'Do not wrap JSON in markdown code fences.'
)


def _anthropic_chat_json(
    prompt: str,
    *,
    max_tokens: int = 1100,
    timeout: float = 120.0,
    return_usage: bool = False,
):
    """Call Anthropic Messages and optionally return provider token usage."""
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

    try:
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=ANTHROPIC_SYSTEM,
            messages=[{'role': 'user', 'content': prompt}],
        )
    except Exception as exc:
        raise RuntimeError(f'Anthropic API call failed: {exc}') from exc

    raw = ''
    for block in message.content:
        if hasattr(block, 'text'):
            raw += block.text

    raw = _strip_fences(raw.strip())
    if not raw:
        raise RuntimeError('Anthropic returned an empty response.')

    if return_usage:
        usage = getattr(message, 'usage', None)
        input_tokens = int(getattr(usage, 'input_tokens', 0) or 0)
        output_tokens = int(getattr(usage, 'output_tokens', 0) or 0)
        estimated = not bool(input_tokens or output_tokens)
        if estimated:
            input_tokens = estimate_prompt_tokens(ANTHROPIC_SYSTEM + '\n' + prompt)
            output_tokens = estimate_prompt_tokens(raw)
        return model, raw, {
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'usage_estimated': estimated,
        }
    return model, raw


def _strip_fences(text: str) -> str:
    text = re.sub(r'^```(?:json)?\\s*', '', text.strip(), flags=re.I)
    text = re.sub(r'\\s*```\\s*$', '', text)
    return text.strip()

def _model_hint(provider: str) -> str:
    if provider == 'anthropic':
        return os.getenv('ANTHROPIC_MODEL', 'claude-haiku-4-5-20251001').strip()
    if provider == 'ollama':
        return os.getenv('OLLAMA_MODEL', 'qwen2.5:0.5b-instruct').strip()
    if provider == 'mock':
        return 'mock'
    return 'unknown'


def _normalise_result(result, prompt):
    if not isinstance(result, tuple) or len(result) not in {2, 3}:
        raise RuntimeError('AI provider returned an invalid result shape.')
    model, raw = result[0], result[1]
    if len(result) == 3 and isinstance(result[2], dict):
        usage = result[2]
        input_tokens = int(usage.get('input_tokens') or 0)
        output_tokens = int(usage.get('output_tokens') or 0)
        usage_estimated = bool(usage.get('usage_estimated', False))
        if not input_tokens:
            input_tokens = estimate_prompt_tokens(prompt)
            usage_estimated = True
        if not output_tokens:
            output_tokens = estimate_prompt_tokens(raw)
            usage_estimated = True
    else:
        input_tokens = estimate_prompt_tokens(prompt)
        output_tokens = estimate_prompt_tokens(raw)
        usage_estimated = True
    return str(model), str(raw), input_tokens, output_tokens, usage_estimated


def _tracked_call(provider, prompt, *, max_tokens, timeout, operation):
    model_hint = _model_hint(provider)
    prompt_for_estimate = (
        ANTHROPIC_SYSTEM + '\n' + prompt if provider == 'anthropic' else prompt
    )
    estimated_input_tokens = estimate_prompt_tokens(prompt_for_estimate)
    if provider == 'anthropic':
        # Reserve against a conservative upper bound before the billable call.
        # Token count cannot exceed the number of UTF-8 bytes represented by
        # the prompt, so this intentionally over-reserves and releases the
        # difference when provider usage is finalized.
        estimated_input_tokens = max(
            estimated_input_tokens,
            len(prompt_for_estimate.encode('utf-8')),
        )
    event = reserve_ai_call(
        provider=provider,
        model=model_hint,
        operation=operation,
        estimated_input_tokens=estimated_input_tokens,
        max_output_tokens=max_tokens,
    )

    try:
        if provider == 'anthropic':
            result = _anthropic_chat_json(
                prompt,
                max_tokens=max_tokens,
                timeout=timeout,
                return_usage=True,
            )
        elif provider == 'ollama':
            result = ollama_chat_json(
                prompt,
                max_tokens=max_tokens,
                timeout=timeout,
                return_usage=True,
            )
        else:
            result = ('mock', '{}', {
                'input_tokens': estimate_prompt_tokens(prompt),
                'output_tokens': 1,
                'usage_estimated': True,
            })

        model, raw, input_tokens, output_tokens, usage_estimated = _normalise_result(
            result, prompt_for_estimate
        )
        complete_ai_call(
            event,
            provider=provider,
            model=model,
            operation=operation,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_estimated=usage_estimated,
        )
        return model, raw
    except Exception as exc:
        fail_ai_call(event, exc)
        raise


def ai_chat_json(
    prompt: str,
    *,
    max_tokens: int = 1100,
    timeout: float = 240.0,
    force_provider: str = None,
    operation: str = 'ai_chat',
) -> tuple[str, str]:
    """
    Dispatch to the configured provider while recording usage and applying
    transactional cost reservations before billable cloud calls.
    """
    provider = (force_provider or os.getenv('AI_PROVIDER', 'auto')).strip().lower()

    if provider == 'mock':
        return _tracked_call(
            'mock', prompt, max_tokens=max_tokens, timeout=timeout, operation=operation
        )

    if provider == 'anthropic':
        return _tracked_call(
            'anthropic', prompt, max_tokens=max_tokens, timeout=timeout, operation=operation
        )

    if provider == 'ollama':
        return _tracked_call(
            'ollama', prompt, max_tokens=max_tokens, timeout=timeout, operation=operation
        )

    errors: list[str] = []
    api_key = os.getenv('ANTHROPIC_API_KEY', '').strip()
    if api_key:
        try:
            return _tracked_call(
                'anthropic',
                prompt,
                max_tokens=max_tokens,
                timeout=timeout,
                operation=operation,
            )
        except (AIBudgetExceeded, AIUsageConfigurationError) as exc:
            # "auto" remains available by falling back to the local model once
            # the cloud budget is exhausted or deliberately unpriced.
            errors.append(f'Anthropic budget: {exc}')
        except Exception as exc:
            errors.append(f'Anthropic: {exc}')

    try:
        return _tracked_call(
            'ollama', prompt, max_tokens=max_tokens, timeout=timeout, operation=operation
        )
    except Exception as exc:
        errors.append(f'Ollama: {exc}')

    raise RuntimeError('All AI providers failed. ' + ' | '.join(errors))


def ai_available() -> bool:
    """Return True if at least one AI provider is likely reachable."""
    provider = os.getenv('AI_PROVIDER', 'auto').strip().lower()
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
    if os.getenv('ANTHROPIC_API_KEY', '').strip():
        return True
    try:
        import httpx
        base = os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/')
        r = httpx.get(f'{base}/api/tags', timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False
, '', text)
    return text.strip()


def _model_hint(provider: str) -> str:
    if provider == 'anthropic':
        return os.getenv('ANTHROPIC_MODEL', 'claude-haiku-4-5-20251001').strip()
    if provider == 'ollama':
        return os.getenv('OLLAMA_MODEL', 'qwen2.5:0.5b-instruct').strip()
    if provider == 'mock':
        return 'mock'
    return 'unknown'


def _normalise_result(result, prompt):
    if not isinstance(result, tuple) or len(result) not in {2, 3}:
        raise RuntimeError('AI provider returned an invalid result shape.')
    model, raw = result[0], result[1]
    if len(result) == 3 and isinstance(result[2], dict):
        usage = result[2]
        input_tokens = int(usage.get('input_tokens') or 0)
        output_tokens = int(usage.get('output_tokens') or 0)
        usage_estimated = bool(usage.get('usage_estimated', False))
        if not input_tokens:
            input_tokens = estimate_prompt_tokens(prompt)
            usage_estimated = True
        if not output_tokens:
            output_tokens = estimate_prompt_tokens(raw)
            usage_estimated = True
    else:
        input_tokens = estimate_prompt_tokens(prompt)
        output_tokens = estimate_prompt_tokens(raw)
        usage_estimated = True
    return str(model), str(raw), input_tokens, output_tokens, usage_estimated


def _tracked_call(provider, prompt, *, max_tokens, timeout, operation):
    model_hint = _model_hint(provider)
    prompt_for_estimate = (
        ANTHROPIC_SYSTEM + '\n' + prompt if provider == 'anthropic' else prompt
    )
    estimated_input_tokens = estimate_prompt_tokens(prompt_for_estimate)
    event = reserve_ai_call(
        provider=provider,
        model=model_hint,
        operation=operation,
        estimated_input_tokens=estimated_input_tokens,
        max_output_tokens=max_tokens,
    )

    try:
        if provider == 'anthropic':
            result = _anthropic_chat_json(
                prompt,
                max_tokens=max_tokens,
                timeout=timeout,
                return_usage=True,
            )
        elif provider == 'ollama':
            result = ollama_chat_json(
                prompt,
                max_tokens=max_tokens,
                timeout=timeout,
                return_usage=True,
            )
        else:
            result = ('mock', '{}', {
                'input_tokens': estimate_prompt_tokens(prompt),
                'output_tokens': 1,
                'usage_estimated': True,
            })

        model, raw, input_tokens, output_tokens, usage_estimated = _normalise_result(
            result, prompt_for_estimate
        )
        complete_ai_call(
            event,
            provider=provider,
            model=model,
            operation=operation,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_estimated=usage_estimated,
        )
        return model, raw
    except Exception as exc:
        fail_ai_call(event, exc)
        raise


def ai_chat_json(
    prompt: str,
    *,
    max_tokens: int = 1100,
    timeout: float = 240.0,
    force_provider: str = None,
    operation: str = 'ai_chat',
) -> tuple[str, str]:
    """
    Dispatch to the configured provider while recording usage and applying
    transactional cost reservations before billable cloud calls.
    """
    provider = (force_provider or os.getenv('AI_PROVIDER', 'auto')).strip().lower()

    if provider == 'mock':
        return _tracked_call(
            'mock', prompt, max_tokens=max_tokens, timeout=timeout, operation=operation
        )

    if provider == 'anthropic':
        return _tracked_call(
            'anthropic', prompt, max_tokens=max_tokens, timeout=timeout, operation=operation
        )

    if provider == 'ollama':
        return _tracked_call(
            'ollama', prompt, max_tokens=max_tokens, timeout=timeout, operation=operation
        )

    errors: list[str] = []
    api_key = os.getenv('ANTHROPIC_API_KEY', '').strip()
    if api_key:
        try:
            return _tracked_call(
                'anthropic',
                prompt,
                max_tokens=max_tokens,
                timeout=timeout,
                operation=operation,
            )
        except (AIBudgetExceeded, AIUsageConfigurationError) as exc:
            # "auto" remains available by falling back to the local model once
            # the cloud budget is exhausted or deliberately unpriced.
            errors.append(f'Anthropic budget: {exc}')
        except Exception as exc:
            errors.append(f'Anthropic: {exc}')

    try:
        return _tracked_call(
            'ollama', prompt, max_tokens=max_tokens, timeout=timeout, operation=operation
        )
    except Exception as exc:
        errors.append(f'Ollama: {exc}')

    raise RuntimeError('All AI providers failed. ' + ' | '.join(errors))


def ai_available() -> bool:
    """Return True if at least one AI provider is likely reachable."""
    provider = os.getenv('AI_PROVIDER', 'auto').strip().lower()
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
    if os.getenv('ANTHROPIC_API_KEY', '').strip():
        return True
    try:
        import httpx
        base = os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/')
        r = httpx.get(f'{base}/api/tags', timeout=3.0)
        return r.status_code == 200
    except Exception:
        return False
