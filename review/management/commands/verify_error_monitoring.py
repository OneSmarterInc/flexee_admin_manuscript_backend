from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from review.monitoring import capture_message, flush_sentry


class Command(BaseCommand):
    help = 'Show Sentry monitoring status and optionally send a privacy-safe verification event.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--send-event',
            action='store_true',
            help='Send one synthetic monitoring event and flush it before exiting.',
        )

    def handle(self, *args, **options):
        enabled = bool(getattr(settings, 'SENTRY_ENABLED', False))
        environment = getattr(settings, 'SENTRY_ENVIRONMENT', 'unknown')
        release = getattr(settings, 'SENTRY_RELEASE', '') or '(not set)'
        traces_rate = getattr(settings, 'SENTRY_TRACES_SAMPLE_RATE', 0.0)

        self.stdout.write(f'Sentry enabled: {enabled}')
        self.stdout.write(f'Environment: {environment}')
        self.stdout.write(f'Release: {release}')
        self.stdout.write(f'Traces sample rate: {traces_rate}')
        self.stdout.write('Privacy mode: request bodies/query strings/cookies/user data/local variables redacted')

        if not options['send_event']:
            return

        if not enabled:
            raise CommandError('Sentry is disabled. Configure SENTRY_DSN before sending a verification event.')

        event_id = capture_message(
            'Flexee production monitoring verification',
            component='operations',
            operation='sentry_verification',
            level='info',
            tags={'verification': 'true'},
        )
        flush_sentry(timeout=5.0)
        self.stdout.write(self.style.SUCCESS(f'Verification event sent: {event_id}'))
