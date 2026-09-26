from __future__ import annotations

import os
from copy import deepcopy
from urllib.parse import urlsplit, urlunsplit


SENSITIVE_KEYS = {
    'authorization',
    'cookie',
    'cookies',
    'csrfmiddlewaretoken',
    'password',
    'passwd',
    'secret',
    'token',
    'access_token',
    'refresh_token',
    'api_key',
    'apikey',
    'dsn',
    'smtp_password',
    'admin_totp_secret',
    'admin_session_secret',
}

_ENABLED = False


def _float_env(name: str, default: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(0.0, min(value, 1.0))


def _strip_url_query(value):
    if not isinstance(value, str) or not value:
        return value
    try:
        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
    except Exception:
        return value.split('?', 1)[0]


def _redact_mapping(value):
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if key_text in SENSITIVE_KEYS or any(
                fragment in key_text
                for fragment in ('password', 'secret', 'token', 'authorization', 'cookie')
            ):
                cleaned[key] = '[redacted]'
            else:
                cleaned[key] = _redact_mapping(item)
        return cleaned
    if isinstance(value, list):
        return [_redact_mapping(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_mapping(item) for item in value)
    return value


def scrub_sentry_event(event, hint=None):
    """Remove request/user/manuscript-adjacent payloads before an event leaves the app."""
    if not isinstance(event, dict):
        return event

    event = deepcopy(event)

    request = event.get('request')
    if isinstance(request, dict):
        request.pop('data', None)
        request.pop('cookies', None)
        request.pop('query_string', None)
        request.pop('env', None)
        request.pop('headers', None)
        if 'url' in request:
            request['url'] = _strip_url_query(request.get('url'))

    # This application handles manuscript/author data. Keep monitoring
    # intentionally anonymous even if an integration tries to attach a user.
    event.pop('user', None)

    # Local variables and breadcrumb payloads can contain manuscript text,
    # author email addresses, generated prompts, or SMTP material.
    breadcrumbs = event.get('breadcrumbs')
    if isinstance(breadcrumbs, dict) and isinstance(breadcrumbs.get('values'), list):
        safe_values = []
        for crumb in breadcrumbs['values']:
            if not isinstance(crumb, dict):
                continue
            safe = {
                key: value
                for key, value in crumb.items()
                if key in {'timestamp', 'type', 'category', 'level'}
            }
            safe_values.append(safe)
        event['breadcrumbs'] = {'values': safe_values}
    else:
        event.pop('breadcrumbs', None)

    exception = event.get('exception')
    if isinstance(exception, dict):
        values = exception.get('values')
        if isinstance(values, list):
            for value in values:
                if not isinstance(value, dict):
                    continue
                # Preserve exception type + stack location, but never ship an
                # exception message that might quote manuscript or email data.
                if 'value' in value:
                    value['value'] = '[redacted]'
                stack = value.get('stacktrace')
                if isinstance(stack, dict):
                    frames = stack.get('frames')
                    if isinstance(frames, list):
                        for frame in frames:
                            if isinstance(frame, dict):
                                frame.pop('vars', None)

    event.pop('extra', None)

    if isinstance(event.get('contexts'), dict):
        event['contexts'] = _redact_mapping(event['contexts'])
    if isinstance(event.get('tags'), dict):
        event['tags'] = _redact_mapping(event['tags'])

    return event


def initialize_sentry(
    *,
    dsn: str,
    environment: str,
    release: str = '',
    traces_sample_rate: float | None = None,
):
    """Initialize Sentry only when an explicit DSN is configured."""
    global _ENABLED

    dsn = str(dsn or '').strip()
    if not dsn:
        _ENABLED = False
        return False

    import sentry_sdk

    if traces_sample_rate is None:
        traces_sample_rate = _float_env('SENTRY_TRACES_SAMPLE_RATE', 0.0)

    sentry_sdk.init(
        dsn=dsn,
        environment=str(environment or 'unknown'),
        release=(str(release).strip() or None),
        send_default_pii=False,
        max_request_body_size='never',
        include_local_variables=False,
        attach_stacktrace=True,
        traces_sample_rate=max(0.0, min(float(traces_sample_rate), 1.0)),
        profiles_sample_rate=0.0,
        before_send=scrub_sentry_event,
        before_send_transaction=scrub_sentry_event,
    )
    _ENABLED = True
    return True


def is_sentry_enabled():
    return bool(_ENABLED)


def _scope_tags(scope, *, component: str, operation: str, tags=None):
    scope.set_tag('component', str(component or 'unknown'))
    scope.set_tag('operation', str(operation or 'unknown'))
    for key, value in (tags or {}).items():
        if value is None:
            continue
        scope.set_tag(str(key), str(value)[:200])


def capture_exception(exc, *, component: str, operation: str, tags=None):
    """Capture a handled exception without attaching request bodies or domain payloads."""
    if not _ENABLED:
        return None

    import sentry_sdk

    with sentry_sdk.push_scope() as scope:
        _scope_tags(scope, component=component, operation=operation, tags=tags)
        scope.set_tag('error_type', exc.__class__.__name__)
        return sentry_sdk.capture_exception(exc)


def capture_message(message: str, *, component: str, operation: str, level='error', tags=None):
    """Capture a sanitized operational signal such as a worker timeout."""
    if not _ENABLED:
        return None

    import sentry_sdk

    with sentry_sdk.push_scope() as scope:
        _scope_tags(scope, component=component, operation=operation, tags=tags)
        return sentry_sdk.capture_message(str(message)[:200], level=level)


def flush_sentry(timeout=2.0):
    if not _ENABLED:
        return
    import sentry_sdk
    sentry_sdk.flush(timeout=timeout)
