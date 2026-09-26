import json

from django.core.management.base import BaseCommand, CommandError

from review.queue_health import emit_queue_health_alert, queue_health_snapshot


class Command(BaseCommand):
    help = 'Check application queue health and optionally emit a deduplicated production alert.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--alert',
            action='store_true',
            help='Emit a Sentry/audit alert when the queue is degraded or critical.',
        )
        parser.add_argument(
            '--force-alert',
            action='store_true',
            help='Bypass the alert cooldown. Implies --alert.',
        )
        parser.add_argument(
            '--fail-on-unhealthy',
            action='store_true',
            help='Exit non-zero when queue status is degraded or critical.',
        )
        parser.add_argument(
            '--json',
            action='store_true',
            help='Print the full queue-health snapshot as JSON.',
        )

    def handle(self, *args, **options):
        snapshot = queue_health_snapshot()

        if options['json']:
            self.stdout.write(json.dumps(snapshot, indent=2, sort_keys=True))
        else:
            self.stdout.write(f"Queue status: {snapshot['status']}")
            self.stdout.write(
                f"Queued={snapshot['queued_jobs']} "
                f"Processing={snapshot['processing_jobs']} "
                f"RecentFailed={snapshot['recent_failed_jobs']} "
                f"RecentCompleted={snapshot['recent_completed_jobs']}"
            )
            self.stdout.write(
                f"OldestQueuedSeconds={snapshot['oldest_queued_job_age_seconds']} "
                f"OldestProcessingSeconds={snapshot['oldest_processing_job_age_seconds']}"
            )
            if snapshot['issues']:
                for issue in snapshot['issues']:
                    self.stdout.write(
                        f"[{issue['severity'].upper()}] {issue['code']}: "
                        f"value={issue['value']} threshold={issue['threshold']}"
                    )
            else:
                self.stdout.write(self.style.SUCCESS('No queue health issues detected.'))

        if options['alert'] or options['force_alert']:
            result = emit_queue_health_alert(
                snapshot,
                force=bool(options['force_alert']),
            )
            self.stdout.write(
                f"Alert emitted={result['emitted']} reason={result['reason']}"
            )

        if options['fail_on_unhealthy'] and snapshot['status'] != 'healthy':
            raise CommandError(f"Queue health is {snapshot['status']}.")
