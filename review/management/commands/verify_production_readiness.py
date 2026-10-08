import os
import importlib.util
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from review.ai_usage import budget_limits, pricing_for
from review.auth import allowed_frontend_origins
from review.services.ai_provider import anthropic_model
from review.storage_quota import storage_limits


EXACT_ORIGIN = re.compile(r'https://[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+(:\d{1,5})?')
LOCAL_HOSTS = ('localhost', '127.', '0.0.0.0', '[::1]')


def _exact_public_origin(origin):
    value = origin.strip().lower()
    if not EXACT_ORIGIN.fullmatch(value):
        return False
    host = value[len('https://'):]
    return not host.startswith(LOCAL_HOSTS)


class Command(BaseCommand):
    help = 'Validate production-only settings required for an external scholarly-network deployment.'

    def handle(self, *args, **options):
        errors = []
        checks = []
        warnings = []

        def require(condition, message):
            checks.append((bool(condition), message))
            if not condition:
                errors.append(message)

        def warn(message):
            warnings.append(message)

        def configured(name):
            value = os.getenv(name, '').strip()
            return bool(value) and 'CHANGE_ME' not in value.upper()

        require(
            os.getenv('DJANGO_ENV', '').strip().lower() == 'production',
            'DJANGO_ENV must be production.',
        )
        require(configured('DJANGO_SECRET_KEY'), 'DJANGO_SECRET_KEY must be a real production secret, not a template placeholder.')
        require(configured('ADMIN_SESSION_SECRET'), 'ADMIN_SESSION_SECRET must be a real production secret, not a template placeholder.')
        require(configured('DATABASE_URL'), 'DATABASE_URL must be a real production PostgreSQL URL, not a template placeholder.')
        require(settings.DEBUG is False, 'Django DEBUG must be false.')
        require(bool(settings.ALLOWED_HOSTS) and '*' not in settings.ALLOWED_HOSTS, 'DJANGO_ALLOWED_HOSTS must be explicit and may not contain *.')
        require(settings.SESSION_COOKIE_SECURE is True, 'Django session cookies must be Secure.')
        require(settings.CSRF_COOKIE_SECURE is True, 'Django CSRF cookies must be Secure.')
        require(os.getenv('COOKIE_SECURE', '').strip().lower() in {'1', 'true', 'yes', 'on'}, 'COOKIE_SECURE must be true for the custom author session cookie.')
        require(settings.SECURE_SSL_REDIRECT is True, 'SECURE_SSL_REDIRECT must be enabled.')
        require(settings.DATABASES['default']['ENGINE'] == 'django.db.backends.postgresql', 'Production must use PostgreSQL.')

        origins = [x.strip() for x in os.getenv('FRONTEND_ORIGINS', '').split(',') if x.strip()]
        require(bool(origins) and all(x.startswith('https://') for x in origins), 'FRONTEND_ORIGINS must contain only explicit https:// origins.')

        # SameSite=None sends the session cookies on cross-site requests, so the Origin check is then the
        # only thing standing between another site and an unsafe request. That is fine only while every
        # allowed origin is exact.
        samesite = os.getenv('COOKIE_SAMESITE', 'Strict').strip()
        require(
            samesite.capitalize() in {'Strict', 'Lax', 'None'},
            'COOKIE_SAMESITE must be Strict, Lax or None (anything else silently falls back to Strict).',
        )
        if samesite.capitalize() == 'None':
            loose = sorted(origin for origin in allowed_frontend_origins() if not _exact_public_origin(origin))
            require(
                not loose,
                'COOKIE_SAMESITE=None requires every allowed origin (FRONTEND_ORIGINS and ADMIN_ALLOWED_ORIGINS) '
                'to be an exact public https:// origin with no wildcard, path or localhost'
                + (f'; loose: {", ".join(loose)}.' if loose else '.'),
            )

        production = os.getenv('DJANGO_ENV', '').strip().lower() == 'production'
        if production and not [p for p in os.getenv('TRUSTED_PROXIES', '').split(',') if p.strip()]:
            warn(
                'TRUSTED_PROXIES is empty. Behind nginx every visitor then shares one address, so the public '
                'rate limits (journal search, claims, sign-in) count all traffic together. Set it to 127.0.0.1.'
            )

        private_media = os.getenv('PRIVATE_MEDIA_ROOT', '').strip()
        require(bool(private_media), 'PRIVATE_MEDIA_ROOT must be configured.')
        if private_media:
            try:
                Path(private_media).expanduser().resolve().relative_to(settings.BASE_DIR.resolve())
                outside_source = False
            except ValueError:
                outside_source = True
            require(outside_source, 'PRIVATE_MEDIA_ROOT must be outside the application source tree.')

        require(configured('SMTP_HOST'), 'SMTP_HOST must be configured for real production email.')
        require(bool(os.getenv('NOTIFY_FROM_EMAIL', '').strip()), 'NOTIFY_FROM_EMAIL must be configured.')
        require(configured('SENTRY_DSN'), 'SENTRY_DSN must be configured for production error reporting.')
        require(bool(os.getenv('BACKUP_ROOT', '').strip()), 'BACKUP_ROOT must be configured for durable backups.')

        provider = os.getenv('AI_PROVIDER', '').strip().lower()
        require(
            provider in {'anthropic', 'ollama', 'shared_qwen'},
            'AI_PROVIDER must be explicitly set to anthropic, ollama or shared_qwen in production.',
        )
        truthy = {'1', 'true', 'yes', 'on'}
        cloud_fallback = os.getenv('ENABLE_CLOUD_FALLBACK', 'false').strip().lower() in truthy
        uses_anthropic = provider == 'anthropic' or cloud_fallback
        if provider == 'ollama':
            model = os.getenv('OLLAMA_MODEL', '').strip()
            require(bool(model), 'OLLAMA_MODEL must be configured when AI_PROVIDER=ollama.')
            require(
                model.lower() != 'qwen2.5:0.5b-instruct',
                'qwen2.5:0.5b-instruct is a development model and may not be used for production editorial judgment.',
            )
        elif provider == 'shared_qwen':
            require(
                os.getenv('SHARED_QWEN_QUEUE_ENABLED', '').strip().lower() in truthy,
                'SHARED_QWEN_QUEUE_ENABLED must be true when AI_PROVIDER=shared_qwen.',
            )
            redis_name = 'SHARED_QWEN_REDIS_URL' if os.getenv('SHARED_QWEN_REDIS_URL', '').strip() else 'REDIS_URL'
            redis_url = os.getenv(redis_name, '').strip()
            require(
                configured(redis_name) and redis_url.startswith(('redis://', 'rediss://', 'unix://')),
                'SHARED_QWEN_REDIS_URL must be a real redis:// or rediss:// URL when AI_PROVIDER=shared_qwen.',
            )
            require(
                importlib.util.find_spec('redis') is not None,
                'The redis Python package must be installed when AI_PROVIDER=shared_qwen.',
            )
        if uses_anthropic:
            reason = 'AI_PROVIDER=anthropic' if provider == 'anthropic' else 'ENABLE_CLOUD_FALLBACK=true'
            require(configured('ANTHROPIC_API_KEY'), f'ANTHROPIC_API_KEY must be configured when {reason}.')
            require(
                importlib.util.find_spec('anthropic') is not None,
                f'The anthropic Python SDK must be installed when {reason}.',
            )
        if os.getenv('AI_PROVIDER_FALLBACK', '').strip():
            warn(
                'AI_PROVIDER_FALLBACK is not read by the application. '
                'Use ENABLE_CLOUD_FALLBACK=true to send oversized reviews to Anthropic.'
            )
        budgets = budget_limits()
        require(budgets['enabled'], 'AI_COST_ENFORCEMENT_ENABLED must be true in production.')
        require(
            budgets['daily_cost_limit_usd'] > 0 or budgets['monthly_cost_limit_usd'] > 0,
            'At least one positive AI daily or monthly cost ceiling must be configured.',
        )
        if uses_anthropic:
            model = anthropic_model()
            require(pricing_for('anthropic', model)['priced'], 'Anthropic input and output pricing must be configured for cost enforcement.')
            if 'haiku' in model.lower():
                warn(
                    f'ANTHROPIC_MODEL is {model}. Review findings, match reasons and the editorial brief '
                    'should use a Sonnet-class model.'
                )
        limits = storage_limits()
        require(limits['author_limit_bytes'] > 0, 'AUTHOR_STORAGE_LIMIT_BYTES must be positive.')
        require(limits['total_limit_bytes'] >= limits['author_limit_bytes'], 'TOTAL_STORAGE_LIMIT_BYTES must be at least the per-author storage limit.')

        for passed, message in checks:
            marker = 'PASS' if passed else 'FAIL'
            self.stdout.write(f'[{marker}] {message}')
        for message in warnings:
            self.stdout.write(self.style.WARNING(f'[WARN] {message}'))

        if errors:
            raise CommandError(f'Production readiness failed with {len(errors)} blocking configuration issue(s).')

        self.stdout.write(self.style.SUCCESS('Production configuration readiness checks passed.'))
