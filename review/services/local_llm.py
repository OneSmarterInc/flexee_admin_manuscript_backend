import os
import re

import httpx


DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:0.5b-instruct"
DEFAULT_OLLAMA_NUM_CTX = 4096
DEFAULT_OLLAMA_NUM_PREDICT = 2000


def _as_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _strip_thinking(text):
    value = str(text or "")
    value = re.sub(r"<think>.*?</think>", "", value, flags=re.I | re.S)
    return value.strip()


def estimate_prompt_tokens(prompt):
    """Cheap tokenizer guard used before sending text to Ollama.

    Ollama counts both the prompt and the response budget inside num_ctx.  This
    approximate chars/4 estimate is intentionally conservative enough to stop
    obviously over-long manuscripts before Ollama silently drops the beginning
    of the prompt, where the rubric and decision rules live.
    """
    return max(1, (len(str(prompt or "")) + 3) // 4)


def assert_prompt_fits_context(prompt, *, num_ctx, num_predict):
    available_prompt_tokens = int(num_ctx) - int(num_predict)
    estimated_prompt_tokens = estimate_prompt_tokens(prompt)
    if available_prompt_tokens <= 0:
        raise RuntimeError(
            "Ollama context is misconfigured: OLLAMA_NUM_CTX must be larger "
            "than OLLAMA_NUM_PREDICT."
        )
    if estimated_prompt_tokens > available_prompt_tokens:
        raise RuntimeError(
            "Manuscript review prompt is too large for the configured local "
            f"Ollama context. Estimated prompt tokens: {estimated_prompt_tokens:,}; "
            f"available prompt budget: {available_prompt_tokens:,} "
            f"(num_ctx={num_ctx:,}, num_predict={num_predict:,}). "
            "Do not send this to the local model because Ollama may truncate "
            "the rubric and decision rules. Use a larger-context provider or "
            "chunked review before judging this manuscript."
        )


def ollama_chat_json(prompt, *, max_tokens=None, timeout=None, num_ctx=None):
    """Call the local Ollama Qwen2.5 endpoint and return its JSON-mode content.

    No cloud credentials are used. The caller remains responsible for validating
    the returned application-level schema.
    """
    base_url = os.getenv("OLLAMA_BASE_URL", DEFAULT_OLLAMA_URL).rstrip("/")
    model = os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL).strip() or DEFAULT_OLLAMA_MODEL
    num_ctx = int(num_ctx if num_ctx is not None else os.getenv("OLLAMA_NUM_CTX", str(DEFAULT_OLLAMA_NUM_CTX)))
    num_predict = int(
        max_tokens if max_tokens is not None else os.getenv("OLLAMA_NUM_PREDICT", str(DEFAULT_OLLAMA_NUM_PREDICT))
    )
    temperature = float(os.getenv("OLLAMA_TEMPERATURE", "0.2"))
    request_timeout = float(
        timeout if timeout is not None else os.getenv("OLLAMA_TIMEOUT", "300")
    )

    assert_prompt_fits_context(prompt, num_ctx=num_ctx, num_predict=num_predict)

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": "5m",
        "format": "json",
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_predict": num_predict,
        },
    }

    try:
        response = httpx.post(
            f"{base_url}/api/chat",
            headers={"content-type": "application/json"},
            json=payload,
            timeout=request_timeout,
        )
    except httpx.RequestError as exc:
        raise RuntimeError(
            f"Could not connect to Ollama at {base_url}. "
            f"Start Ollama and pull {model} first. Original error: {exc}"
        ) from exc

    if response.status_code >= 400:
        detail = response.text[:500]
        raise RuntimeError(
            f"Ollama request failed ({response.status_code}) for model {model}: {detail}"
        )

    try:
        payload = response.json()
        content = payload.get("message", {}).get("content", "")
    except (ValueError, AttributeError) as exc:
        raise RuntimeError("Ollama returned an invalid JSON response") from exc

    content = _strip_thinking(content)
    if not content:
        raise RuntimeError(f"Ollama returned an empty response for model {model}")

    return model, content
