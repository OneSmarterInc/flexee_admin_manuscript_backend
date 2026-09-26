import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from review.ai_usage import budget_limits, pricing_for
from review.storage_quota import storage_limits


class Command(BaseCommand):
    help = 'Validate production-only settings required for an external scholarly-network deployment.'

    def handle(self, *args, **options):
        errors = []
        checks = []

        def require(condition, message):
            checks.append((bool(condition), message))
            if not condition:
                errors.append(message)

        require(
            os.getenv('DJANGO_ENV', '').strip().lower() == 'production',
            'DJANGO_ENV must be production.',
        )
        require(settings.DEBUG is False, 'Django DEBUG must be false.')
        require(bool(settings.ALLOWED_HOSTS) and '*' not in settings.ALLOWED_HOSTS, 'DJANGO_ALLOWED_HOSTS must be explicit and may not contain *.')

        origins = [x.strip() for x in os.getenv('FRONTEND_ORIGINS', '').split(',') if x.strip()]
        require(bool(origins) and all(x.startswith('https://') for x in origins), 'FRONTEND_ORIGINS must contain only explicit https:// origins.')

        private_media = os.getenv('PRIVATE_MEDIA_ROOT', '').strip()
        require(bool(private_media), 'PRIVATE_MEDIA_ROOT must be configured.')
        if private_media:
            try:
                Path(private_media).expanduser().resolve().relative_to(settings.BASE_DIR.resolve())
                outside_source = False
            except ValueError:
                outside_source = True
            require(outside_source, 'PRIVATE_MEDIA_ROOT must be outside the application source tree.')

        require(bool(os.getenv('SMTP_HOST', '').strip()), 'SMTP_HOST must be configured for real production email.')
        require(bool(os.getenv('NOTIFY_FROM_EMAIL', '').strip()), 'NOTIFY_FROM_EMAIL must be configured.')
        require(bool(os.getenv('SENTRY_DSN', '').strip()), 'SENTRY_DSN must be configured for production error reporting.')
        require(bool(os.getenv('BACKUP_ROOT', '').strip()), 'BACKUP_ROOT must be configured for durable backups.')

        provider = os.getenv('AI_PROVIDER', '').strip().lower()
        require(provider in {'anthropic', 'ollama'}, 'AI_PROVIDER must be explicitly set to anthropic or ollama in production.')
        if provider == 'anthropic':
            require(bool(os.getenv('ANTHROPIC_API_KEY', '').strip()), 'ANTHROPIC_API_KEY must be configured when AI_PROVIDER=anthropic.')
        elif provider == 'ollama':
            model = os.getenv('OLLAMA_MODEL', '').strip()
            require(bool(model), 'OLLAMA_MODEL must be configured when AI_PROVIDER=ollama.')
            require(
                model.lower() != 'qwen2.5:0.5b-instruct',
                'qwen2.5:0.5b-instruct is a development model and may not be used for production editorial judgment.',
            )

        budgets = budget_limits()
        require(budgets['enabled'], 'AI_COST_ENFORCEMENT_ENABLED must be true in production.')
        require(
            budgets['daily_cost_limit_usd'] > 0 or budgets['monthly_cost_limit_usd'] > 0,
            'At least one positive AI daily or monthly cost ceiling must be configured.',
        )
        if provider == 'anthropic':
            model = os.getenv('ANTHROPIC_MODEL', 'claude-haiku-4-5-20251001').strip()
            require(pricing_for(provider, model)['priced'], 'Anthropic input and output pricing must be configured for cost enforcement.')

        limits = storage_limits()
        require(limits['author_limit_bytes'] > 0, 'AUTHOR_STORAGE_LIMIT_BYTES must be positive.')
        require(limits['total_limit_bytes'] >= limits['author_limit_bytes'], 'TOTAL_STORAGE_LIMIT_BYTES must be at least the per-author storage limit.')

        for passed, message in checks:
            marker = 'PASS' if passed else 'FAIL'
            self.stdout.write(f'[{marker}] {message}')

        if errors:
            raise CommandError(f'Production readiness failed with {len(errors)} blocking configuration issue(s).')

        self.stdout.write(self.style.SUCCESS('Production configuration readiness checks passed.'))
