from django.core.management.base import BaseCommand

from review.discovery_schedule import default_timezone, set_schedule


class Command(BaseCommand):
    help = 'Install or refresh the single daily venue-discovery schedule for Django-Q (idempotent).'

    def add_arguments(self, parser):
        parser.add_argument('--hour', type=int, default=2, help='Hour of day, 0-23. Default 2.')
        parser.add_argument('--minute', type=int, default=0, help='Minute, 0-59. Default 0.')
        parser.add_argument('--timezone', default='', help='Time zone, e.g. Asia/Kolkata. Default: server/VENUE_DISCOVERY_TIMEZONE.')
        parser.add_argument('--remove', action='store_true', help='Delete the schedule instead of installing it.')

    def handle(self, *args, **options):
        if options['remove']:
            set_schedule(enabled=False)
            self.stdout.write(self.style.SUCCESS('Removed the daily venue-discovery schedule.'))
            return
        hour = max(0, min(int(options['hour']), 23))
        minute = max(0, min(int(options['minute']), 59))
        state = set_schedule(enabled=True, hour=hour, minute=minute, tz_name=options['timezone'] or default_timezone())
        self.stdout.write(self.style.SUCCESS(
            f"Daily venue discovery at {state['time']} {state['timezone']} via qcluster (next run {state['next_run']})."
        ))
