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
    cost_controls_enabled,
    fail_ai_call,
    reserve_ai_call,
)
from .local_llm import estimate_prompt_tokens, ollama_chat_json, shared_qwen_chat_json


# Review, readiness, match reasons and the editorial brief: one structured call
# each, against criteria we supply, with the answer validated against the text it
# cites. The 12 October production E2E measured Haiku at about $0.003 and 36
# seconds per manuscript on that path, naming specific gaps rather than restating
# the venue type, so Haiku is the review default.
#
# Venue discovery is the opposite kind of work: multi-step agentic browsing where
# the model chooses what to search and which pages to open. It keeps a
# Sonnet-class model through VENUE_DISCOVERY_MODEL. The two defaults differ on
# purpose; do not align them.
DEFAULT_ANTHROPIC_MODEL = 'claude-haiku-5-5'


def anthropic_model() -> str:
    return os.getenv('ANTHROPIC_MODEL', '').strip() or DEFAULT_ANTHROPIC_MODEL


def anthropic_min_output_tokens() -> int:
    """Floor on max_tokens for Anthropic calls (ANTHROPIC_MIN_OUTPUT_TOKENS, default 4000).

    The review path's budgets (650 to 1,100 tokens) were sized for the small local model. Claude
    writes fuller JSON, and a cut-off answer is invalid JSON, so the whole step falls back. Output is
    billed only for tokens actually written, so a higher ceiling costs nothing extra by itself."""
    try:
        return max(256, min(int(os.getenv('ANTHROPIC_MIN_OUTPUT_TOKENS', '4000')), 32000))
    except ValueError:
        return 4000


def anthropic_thinking_disabled() -> bool:
    """True unless ANTHROPIC_THINKING=on. The review path asks for short JSON answers."""
    return os.getenv('ANTHROPIC_THINKING', 'off').strip().lower() not in {'1', 'true', 'yes', 'on'}


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

    model = anthropic_model()
    client = _anthropic_sdk.Anthropic(api_key=api_key, timeout=timeout)

    request = {
        'model': model,
        'max_tokens': max_tokens,
        'system': ANTHROPIC_SYSTEM,
        'messages': [{'role': 'user', 'content': prompt}],
    }
    if anthropic_thinking_disabled():
        # Claude 5.5 models think by default, and thinking tokens count toward max_tokens: with our
        # small JSON budgets the answer can stop after the thinking block, before any text.
        request['thinking'] = {'type': 'disabled'}
    try:
        message = client.messages.create(**request)
    except Exception as exc:
        raise RuntimeError(f'Anthropic API call failed: {exc}') from exc

    # Select text blocks by type: a response can begin with thinking blocks.
    raw = ''.join(getattr(block, 'text', '') or '' for block in message.content
                  if getattr(block, 'type', 'text') == 'text')

    raw = _strip_fences(raw.strip())
    stop_reason = getattr(message, 'stop_reason', None)
    if stop_reason == 'refusal' or not raw:
        # Review 4, Y7: Anthropic billed for this call even though there is no answer to use â€” a reply that
        # is all thinking and stopped at max_tokens, or a refusal. The usage it reported travels with the
        # exception so the failure is recorded at its real cost instead of zero.
        detail = ('Anthropic declined this request (stop_reason: refusal).' if stop_reason == 'refusal'
                  else f'Anthropic returned an empty response (stop_reason: {stop_reason}).')
        error = RuntimeError(detail)
        billed = getattr(message, 'usage', None)
        error.billed_input_tokens = int(getattr(billed, 'input_tokens', 0) or 0)
        error.billed_output_tokens = int(getattr(billed, 'output_tokens', 0) or 0)
        raise error

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
    text = re.sub(r'^```(?:json)?\s*', '', text.strip(), flags=re.I)
    text = re.sub(r'\s*```\s*$', '', text)
    return text.strip()

def _model_hint(provider: str) -> str:
    if provider == 'anthropic':
        return anthropic_model()
    if provider == 'ollama':
        return os.getenv('OLLAMA_MODEL', 'qwen2.5:0.5b-instruct').strip()
    if provider == 'shared_qwen':
        return 'qwen2.5-shared'
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
    if provider == 'anthropic':
        max_tokens = max(int(max_tokens or 0), anthropic_min_output_tokens())
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
    try:
        event = reserve_ai_call(
            provider=provider,
            model=model_hint,
            operation=operation,
            estimated_input_tokens=estimated_input_tokens,
            max_output_tokens=max_tokens,
        )
    except (AIBudgetExceeded, AIUsageConfigurationError):
        raise
    except Exception:
        # Usage persistence is observational while enforcement is disabled.
        # When enforcement is enabled, database/accounting failures fail closed.
        if cost_controls_enabled():
            raise
        event = None

    try:
        if provider == 'anthropic':
            result = _anthropic_chat_json(
                prompt,
                max_tokens=max_tokens,
                timeout=timeout,
                return_usage=True,
            )
        elif provider == 'ollama':
            from .matching_schema import operation_schema
            output_options = ({'response_schema': operation_schema(operation)}
                              if operation_schema(operation) is not None else {})
            result = ollama_chat_json(
                prompt,
                **output_options,
                max_tokens=max_tokens,
                timeout=timeout,
                return_usage=True,
            )
        elif provider == 'shared_qwen':
            result = shared_qwen_chat_json(
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
        try:
            complete_ai_call(
                event,
                provider=provider,
                model=model,
                operation=operation,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                usage_estimated=usage_estimated,
            )
        except Exception:
            if cost_controls_enabled():
                raise
        return model, raw
    except Exception as exc:
        try:
            fail_ai_call(
                event,
                exc,
                provider=provider,
                model=model_hint,
                operation=operation,
                estimated_input_tokens=estimated_input_tokens,
            )
        except Exception:
            pass
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
    from .author_ai import current_selection, custom_chat
    selection = current_selection()
    if selection is not None:
        if selection.provider != 'default':
            return custom_chat(selection, prompt, max_tokens, timeout)
        from .matching_schema import operation_schema
        return ollama_chat_json(prompt, max_tokens=max_tokens, timeout=timeout,
                                model_override=selection.model,
                                response_schema=operation_schema(operation))
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

    if provider == 'shared_qwen':
        return _tracked_call(
            'shared_qwen', prompt, max_tokens=max_tokens, timeout=timeout, operation=operation
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
    from .author_ai import current_selection
    selection = current_selection()
    if selection is not None:
        # Route failures explicitly at call time; do not silently use another provider.
        return True
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
    if provider == 'shared_qwen':
        # The shared worker answers jobs from a Redis queue: reachable when the queue is on and Redis answers.
        if os.getenv('SHARED_QWEN_QUEUE_ENABLED', 'false').strip().lower() not in {'1', 'true', 'yes', 'on'}:
            return False
        try:
            import redis
            url = os.getenv('SHARED_QWEN_REDIS_URL') or os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0')
            return bool(redis.from_url(url, socket_connect_timeout=3, socket_timeout=3).ping())
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
