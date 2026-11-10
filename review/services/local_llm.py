import json
import os
import re
import time
import uuid

import redis


DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:0.5b-instruct"
DEFAULT_OLLAMA_NUM_CTX = 4096
DEFAULT_OLLAMA_NUM_PREDICT = 2000
SHARED_QWEN_MAX_CTX = 8192  # the most the shared worker accepts


def effective_context(num_ctx, num_predict):
    """Review 2, X7: the context a call will really get. With the shared Qwen queue on, the worker caps the
    context at 8192 and the output at DEFAULT_OLLAMA_NUM_PREDICT, so prompts must be sized for that."""
    if _shared_qwen_settings()['enabled'] and os.getenv('AI_PROVIDER', 'ollama').strip().lower() != 'anthropic':
        return min(int(num_ctx), SHARED_QWEN_MAX_CTX), min(int(num_predict), DEFAULT_OLLAMA_NUM_PREDICT)
    return int(num_ctx), int(num_predict)


def _shared_qwen_settings():
    """Bug L6: read the shared-Qwen settings when they are used, not once at import, so the worker, the
    readiness check and the tests all see the same environment."""
    return {
        'enabled': os.getenv("SHARED_QWEN_QUEUE_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"},
        'redis_url': os.getenv("SHARED_QWEN_REDIS_URL", os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")),
        'queue': os.getenv("SHARED_QWEN_QUEUE", "ai_queue:flexee"),
        'result_prefix': os.getenv("SHARED_QWEN_RESULT_PREFIX", "ai_result:"),
        'job_ttl': int(os.getenv("SHARED_QWEN_JOB_TTL", "3600")),
        'wait_timeout': float(os.getenv("SHARED_QWEN_WAIT_TIMEOUT", "900")),
        'poll_interval': float(os.getenv("SHARED_QWEN_POLL_INTERVAL", "0.5")),
    }


def _strip_thinking(text):
    value = str(text or "")
    value = re.sub(r"<think>.*?</think>", "", value, flags=re.I | re.S)
    return value.strip()


_CODE_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?\s*```$", re.S)


def _json_payload(text):
    """Return the JSON text inside a reply that wraps it in Markdown fences or prose.

    The shared worker has no JSON mode (Ollama's format=json), so the model often
    answers with a ```json block. Callers parse with json.loads, so unwrap it here.
    Text that holds no JSON object comes back unchanged and still fails at the caller.
    """
    value = str(text or "").strip()
    fenced = _CODE_FENCE.match(value)
    if fenced:
        value = fenced.group(1).strip()
    try:
        json.loads(value)
        return value
    except ValueError:
        pass
    start, end = value.find("{"), value.rfind("}")
    if start != -1 and end > start:
        candidate = value[start:end + 1]
        try:
            json.loads(candidate)
            return candidate
        except ValueError:
            pass
    return value


def estimate_prompt_tokens(prompt):
    return max(1, (len(str(prompt or "")) + 3) // 4)


def request_context_size(prompt, configured, output_tokens):
    """Allocate a smaller KV cache for short prompts; never truncate input here."""
    if os.getenv('OLLAMA_ADAPTIVE_CONTEXT', 'true').lower() != 'true':
        return configured
    # Double the existing token estimate plus a framing margin, keeping the
    # configured context as the ceiling. Larger inputs retain that full ceiling.
    needed = 2 * estimate_prompt_tokens(prompt) + output_tokens + 512
    for size in (4096, 8192, 16384, 32768):
        if size >= needed:
            return min(configured, size)
    return configured


def assert_prompt_fits_context(prompt, *, num_ctx, num_predict):
    available_prompt_tokens = int(num_ctx) - int(num_predict)
    estimated_prompt_tokens = estimate_prompt_tokens(prompt)
    if available_prompt_tokens <= 0:
        raise RuntimeError(
            "Local LLM context is misconfigured: num_ctx must be larger than num_predict."
        )
    if estimated_prompt_tokens > available_prompt_tokens:
        raise RuntimeError(
            "Manuscript review prompt is too large for the configured local "
            f"context. Estimated prompt tokens: {estimated_prompt_tokens:,}; "
            f"available prompt budget: {available_prompt_tokens:,}."
        )


def _abandon_shared_job(client, settings, payload, job_id):
    """Take a job Flexee has stopped waiting for off the shared queue, and tell a worker holding it to stop.

    Neither call may raise: the caller is already failing, and the original error is the useful one.
    """
    try:
        client.lrem(settings['queue'], 0, payload)       # still queued: it never runs
    except Exception:                                    # noqa: BLE001 - best effort, never mask the real error
        pass
    try:                                                 # already popped: the worker can check this and stop
        client.setex(f"{settings['result_prefix']}cancelled:{job_id}", max(60, int(settings['job_ttl'])), "1")
    except Exception:                                    # noqa: BLE001
        pass


def _forget_shared_result(client, settings, job_id):
    try:
        client.delete(f"{settings['result_prefix']}{job_id}")
    except Exception:                                    # noqa: BLE001
        pass


def _wait_for_shared_result(client, job_id, timeout):
    settings = _shared_qwen_settings()
    result_key = f"{settings['result_prefix']}{job_id}"
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        raw = client.get(result_key)
        if raw:
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Shared Qwen worker returned invalid JSON.") from exc

            if not payload.get("ok"):
                raise RuntimeError(payload.get("error") or "Shared Qwen worker failed.")

            return payload.get("result") or {}

        time.sleep(settings['poll_interval'])

    raise TimeoutError(
        f"Shared Qwen worker did not return job {job_id} within {timeout:.0f} seconds."
    )


def shared_qwen_chat_json(
    prompt,
    *,
    max_tokens=None,
    timeout=None,
    num_ctx=None,
    return_usage=False,
):
    settings = _shared_qwen_settings()
    if not settings['enabled']:
        raise RuntimeError("Shared Qwen queue is disabled.")

    max_tokens = min(
        int(
            max_tokens if max_tokens is not None else os.getenv(
                "OLLAMA_NUM_PREDICT", str(DEFAULT_OLLAMA_NUM_PREDICT)
            )
        ),
        DEFAULT_OLLAMA_NUM_PREDICT,
    )
    num_ctx = max(
        512,
        min(
            int(
                num_ctx if num_ctx is not None else os.getenv(
                    "OLLAMA_NUM_CTX", str(DEFAULT_OLLAMA_NUM_CTX)
                )
            ),
            SHARED_QWEN_MAX_CTX,
        ),
    )
    request_timeout = float(timeout if timeout is not None else settings['wait_timeout'])
    assert_prompt_fits_context(prompt, num_ctx=num_ctx, num_predict=max_tokens)

    # Audit item 38: a Redis that stops answering must fail fast, not hang the worker. Results are
    # polled with short GETs, so a 10-second socket timeout never cuts a normal wait short.
    client = redis.from_url(settings['redis_url'], decode_responses=True, socket_connect_timeout=5,
                            socket_timeout=10)
    client.ping()

    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "project": "flexee",
        "priority": "high",
        "prompt": str(prompt),
        "max_tokens": max_tokens,
        "num_ctx": num_ctx,
        "temperature": float(os.getenv("OLLAMA_TEMPERATURE", "0.2")),
        # Review 4, Y12: the deadline travels with the job, so a worker that reaches it late can drop it
        # instead of spending GPU minutes on an answer nobody is waiting for any more.
        "expires_at": time.time() + request_timeout,
        "job_ttl": settings['job_ttl'],
    }
    payload = json.dumps(job)

    client.rpush(settings['queue'], payload)

    try:
        result = _wait_for_shared_result(client, job_id, request_timeout)
    except BaseException:
        # Review 4, Y12: give up on the queue entry as well as on the wait. Until this, every timed-out
        # readiness chunk stayed queued and the shared worker — which other projects share — still ran all
        # of them to completion. One stuck readiness run left up to 16 abandoned prompts behind it.
        _abandon_shared_job(client, settings, payload, job_id)
        raise
    finally:
        _forget_shared_result(client, settings, job_id)

    model = str(result.get("model") or "qwen2.5-shared")
    content = _json_payload(_strip_thinking(result.get("content", "")))
    if not content:
        raise RuntimeError("Shared Qwen worker returned an empty response.")

    if return_usage:
        return model, content, {
            "input_tokens": int(result.get("input_tokens") or estimate_prompt_tokens(prompt)),
            "output_tokens": int(result.get("output_tokens") or estimate_prompt_tokens(content)),
            "usage_estimated": bool(result.get("usage_estimated", True)),
        }

    return model, content


def ollama_chat_json(
    prompt,
    *,
    max_tokens=None,
    timeout=None,
    num_ctx=None,
    return_usage=False,
):
    if _shared_qwen_settings()['enabled']:
        return shared_qwen_chat_json(
            prompt,
            max_tokens=max_tokens,
            timeout=timeout,
            num_ctx=num_ctx,
            return_usage=return_usage,
        )

    import httpx

    base_url = os.getenv("OLLAMA_BASE_URL", DEFAULT_OLLAMA_URL).rstrip("/")
    model = os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL).strip() or DEFAULT_OLLAMA_MODEL
    num_ctx = int(
        num_ctx if num_ctx is not None else os.getenv(
            "OLLAMA_NUM_CTX", str(DEFAULT_OLLAMA_NUM_CTX)
        )
    )
    num_predict = int(
        max_tokens if max_tokens is not None else os.getenv(
            "OLLAMA_NUM_PREDICT", str(DEFAULT_OLLAMA_NUM_PREDICT)
        )
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
        "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "15m"),
        "format": "json",
        "options": {
            "temperature": temperature,
            "num_ctx": request_context_size(prompt, num_ctx, num_predict),
            "num_predict": num_predict,
        },
    }

    # Thinking models (Qwen3, DeepSeek-R1...) skip their reasoning step unless OLLAMA_THINK=true:
    # much faster, same JSON answer. Models without the option get the request without it.
    if os.getenv("OLLAMA_THINK", "false").strip().lower() not in {"1", "true", "yes", "on"}:
        payload["think"] = False

    def _post(body):
        return httpx.post(
            f"{base_url}/api/chat",
            headers={"content-type": "application/json"},
            json=body,
            timeout=request_timeout,
        )

    try:
        response = _post(payload)
        if response.status_code == 400 and "think" in payload and "think" in response.text.lower():
            payload = {key: value for key, value in payload.items() if key != "think"}
            response = _post(payload)
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

    if return_usage:
        usage = {
            "input_tokens": int(
                payload.get("prompt_eval_count") or estimate_prompt_tokens(prompt)
            ),
            "output_tokens": int(
                payload.get("eval_count") or estimate_prompt_tokens(content)
            ),
            "usage_estimated": not bool(
                payload.get("prompt_eval_count") or payload.get("eval_count")
            ),
        }
        return model, content, usage

    return model, content
