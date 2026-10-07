from django.core.management.base import BaseCommand

from review.discovery_schedule import default_timezone
from review.index_schedule import set_index_schedule


class Command(BaseCommand):
    help = ('Install (or remove) the venue index schedules: a monthly catalogue refresh on the 1st, '
            'and a daily catch-up for Crossref/DOAJ checks. Idempotent.')

    def add_arguments(self, parser):
        parser.add_argument('--timezone', default='', help='e.g. Asia/Kolkata. Default: VENUE_DISCOVERY_TIMEZONE.')
        parser.add_argument('--remove', action='store_true', help='Delete both schedules.')

    def handle(self, *args, **options):
        if options['remove']:
            set_index_schedule(enabled=False)
            self.stdout.write(self.style.SUCCESS('Removed the venue index schedules.'))
            return
        state = set_index_schedule(enabled=True, tz_name=options['timezone'] or default_timezone())
        self.stdout.write(self.style.SUCCESS(
            f"Venue index: monthly refresh next at {state['next_full_refresh']}, "
            f"daily checks next at {state['next_daily_checks']} (via qcluster)."))
