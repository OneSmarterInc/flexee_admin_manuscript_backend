from __future__ import annotations

import os
from contextlib import contextmanager

from django.db import transaction
from django.db.models import Sum

from .models import Author, Manuscript, StorageQuotaState, SubmissionRequirementFile


DEFAULT_AUTHOR_STORAGE_LIMIT_BYTES = 100 * 1024 * 1024
DEFAULT_TOTAL_STORAGE_LIMIT_BYTES = 10 * 1024 * 1024 * 1024


class StorageQuotaExceeded(RuntimeError):
    def __init__(self, message, *, scope, limit_bytes, used_bytes, requested_bytes):
        super().__init__(message)
        self.scope = scope
        self.limit_bytes = int(limit_bytes)
        self.used_bytes = int(used_bytes)
        self.requested_bytes = int(requested_bytes)

    def payload(self):
        return {
            'detail': str(self),
            'code': 'storage_quota_exceeded',
            'scope': self.scope,
            'limit_bytes': self.limit_bytes,
            'used_bytes': self.used_bytes,
            'requested_bytes': self.requested_bytes,
        }


def _positive_env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == '':
        return int(default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return int(default)
    return max(1, value)


def storage_limits():
    return {
        'author_limit_bytes': _positive_env_int(
            'AUTHOR_STORAGE_LIMIT_BYTES',
            DEFAULT_AUTHOR_STORAGE_LIMIT_BYTES,
        ),
        'total_limit_bytes': _positive_env_int(
            'TOTAL_STORAGE_LIMIT_BYTES',
            DEFAULT_TOTAL_STORAGE_LIMIT_BYTES,
        ),
    }


def _requirement_usage(queryset):
    return int(queryset.aggregate(total=Sum('file_bytes'))['total'] or 0)


def _manuscript_usage(queryset):
    return int(queryset.aggregate(total=Sum('manuscript_bytes'))['total'] or 0)


def storage_usage(*, author=None):
    manuscripts = Manuscript.objects.filter(content_purged_at__isnull=True)
    requirements = SubmissionRequirementFile.objects.filter(
        venue_submission__retention_purged_at__isnull=True,
    )

    if author is not None:
        manuscripts = manuscripts.filter(author_account=author)
        requirements = requirements.filter(
            venue_submission__manuscript__author_account=author,
        )

    return _manuscript_usage(manuscripts) + _requirement_usage(requirements)


@contextmanager
def storage_quota_guard(*, author=None, incoming_bytes: int, replacing_bytes: int = 0):
    incoming_bytes = max(0, int(incoming_bytes or 0))
    replacing_bytes = max(0, int(replacing_bytes or 0))
    net_new_bytes = max(0, incoming_bytes - replacing_bytes)
    limits = storage_limits()

    with transaction.atomic():
        StorageQuotaState.objects.get_or_create(key='global')
        StorageQuotaState.objects.select_for_update().get(key='global')

        locked_author = None
        if author is not None:
            locked_author = Author.objects.select_for_update().get(pk=author.pk)

        total_used = storage_usage()
        total_limit = limits['total_limit_bytes']
        if total_used + net_new_bytes > total_limit:
            raise StorageQuotaExceeded(
                'The service storage limit would be exceeded by this upload.',
                scope='global',
                limit_bytes=total_limit,
                used_bytes=total_used,
                requested_bytes=net_new_bytes,
            )

        if locked_author is not None:
            author_used = storage_usage(author=locked_author)
            author_limit = limits['author_limit_bytes']
            if author_used + net_new_bytes > author_limit:
                raise StorageQuotaExceeded(
                    'Your account storage limit would be exceeded by this upload.',
                    scope='author',
                    limit_bytes=author_limit,
                    used_bytes=author_used,
                    requested_bytes=net_new_bytes,
                )

        yield
