"""Fixed-window rate limits for public, read-only endpoints.

Each network (the same keyed `remote_hash` the claim and login limits use) gets `limit` requests per
`window_seconds` for a scope. The counter lives in the database, so every gunicorn worker shares it.
"""
import os
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import JsonResponse
from django.utils import timezone

from .auth import remote_hash


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def journal_index_limits():
    """(requests, window seconds) for the public journal index. Generous: authors page through it."""
    return max(1, _env_int('JOURNAL_INDEX_REQUESTS_PER_WINDOW', 600)), max(60, _env_int('JOURNAL_INDEX_WINDOW_SECONDS', 3600))


def _window_start(now, window_seconds):
    epoch = int(now.timestamp())
    return now - timedelta(seconds=epoch % window_seconds, microseconds=now.microsecond)


def hit(request, scope, limit, window_seconds):
    """Count one request. Returns (allowed, retry_after_seconds)."""
    from .models import PublicRequestWindow

    now = timezone.now()
    start = _window_start(now, window_seconds)
    network = remote_hash(request)
    rows = PublicRequestWindow.objects.filter(scope=scope, remote_hash=network, window_start=start)
    if not rows.update(count=F('count') + 1):
        try:
            with transaction.atomic():
                PublicRequestWindow.objects.create(scope=scope, remote_hash=network, window_start=start, count=1)
        except IntegrityError:  # another worker created the row first
            rows.update(count=F('count') + 1)
    count = rows.values_list('count', flat=True).first() or 0
    if count > limit:
        retry_after = max(1, int((start + timedelta(seconds=window_seconds) - now).total_seconds()))
        return False, retry_after
    return True, 0


def limited_response(retry_after):
    response = JsonResponse({'detail': 'Too many requests from here. Try again later.'}, status=429)
    response['Retry-After'] = str(retry_after)
    return response


def purge_old_windows(older_than=timedelta(days=1)):
    from .models import PublicRequestWindow
    deleted, _ = PublicRequestWindow.objects.filter(window_start__lt=timezone.now() - older_than).delete()
    return deleted
