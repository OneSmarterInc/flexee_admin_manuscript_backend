import json
import os

from django.core.management.base import BaseCommand, CommandError

from review.ai_usage import ai_usage_snapshot, budget_limits, pricing_for


class Command(BaseCommand):
    help = 'Show AI usage/cost totals and verify whether cloud cost ceilings are configured.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--json',
            action='store_true',
            help='Print the complete AI usage snapshot as JSON.',
        )
        parser.add_argument(
            '--fail-if-unbounded',
            action='store_true',
            help='Exit non-zero if the configured cloud provider is not protected by a monetary ceiling.',
        )

    def handle(self, *args, **options):
        snapshot = ai_usage_snapshot()
        provider = os.getenv('AI_PROVIDER', 'auto').strip().lower()
        anthropic_model = os.getenv(
            'ANTHROPIC_MODEL',
            'claude-haiku-4-5-20251001',
        ).strip()
        anthropic_pricing = pricing_for('anthropic', anthropic_model)
        limits = budget_limits()

        cloud_possible = (
            provider == 'anthropic'
            or (provider == 'auto' and bool(os.getenv('ANTHROPIC_API_KEY', '').strip()))
        )
        has_limit = (
            limits['daily_cost_limit_usd'] > 0
            or limits['monthly_cost_limit_usd'] > 0
        )
        bounded = (
            not cloud_possible
            or (
                limits['enabled']
                and has_limit
                and anthropic_pricing['priced']
            )
        )

        if options['json']:
            payload = {
                **snapshot,
                'configured_provider': provider,
                'cloud_provider_possible': cloud_possible,
                'cloud_cost_ceiling_ready': bounded,
                'anthropic_model_priced': anthropic_pricing['priced'],
            }
            self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
        else:
            self.stdout.write(f"Configured provider: {provider}")
            self.stdout.write(f"Cloud provider possible: {cloud_possible}")
            self.stdout.write(f"Cost enforcement enabled: {limits['enabled']}")
            self.stdout.write(
                f"Daily cost limit USD: {snapshot['daily_cost_limit_usd']}"
            )
            self.stdout.write(
                f"Monthly cost limit USD: {snapshot['monthly_cost_limit_usd']}"
            )
            self.stdout.write(
                f"Today: calls={snapshot['today']['calls']} "
                f"tokens={snapshot['today']['total_tokens']} "
                f"cost_usd={snapshot['today']['cost_usd']}"
            )
            self.stdout.write(
                f"Month: calls={snapshot['month']['calls']} "
                f"tokens={snapshot['month']['total_tokens']} "
                f"cost_usd={snapshot['month']['cost_usd']}"
            )
            self.stdout.write(f"Cloud cost ceiling ready: {bounded}")

        if options['fail_if_unbounded'] and not bounded:
            raise CommandError(
                'Cloud AI is available but a complete monetary cost ceiling is not configured. '
                'Enable AI_COST_ENFORCEMENT_ENABLED, configure a daily and/or monthly limit, '
                'and configure pricing for the Anthropic model.'
            )
