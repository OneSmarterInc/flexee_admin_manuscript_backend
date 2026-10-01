from datetime import datetime, time, timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone
from django_q.models import Schedule

SCHEDULE_NAME = 'flexee-venue-discovery-daily'


class Command(BaseCommand):
    help = 'Install or refresh the single daily venue-discovery schedule for Django-Q (idempotent).'

    def add_arguments(self, parser):
        parser.add_argument('--hour', type=int, default=2, help='Hour of day (server time zone) to run, 0-23. Default 2.')
        parser.add_argument('--remove', action='store_true', help='Delete the schedule instead of installing it.')

    def handle(self, *args, **options):
        if options['remove']:
            deleted, _ = Schedule.objects.filter(name=SCHEDULE_NAME).delete()
            self.stdout.write(self.style.SUCCESS(f'Removed {deleted} venue-discovery schedule(s).'))
            return
        hour = max(0, min(int(options['hour']), 23))
        now = timezone.localtime()
        next_run = timezone.make_aware(datetime.combine(now.date(), time(hour=hour)))
        if next_run <= now:
            next_run += timedelta(days=1)
        # Remove accidental duplicates, then upsert exactly one schedule.
        Schedule.objects.filter(func='review.tasks.run_venue_discovery_task').exclude(name=SCHEDULE_NAME).delete()
        schedule, created = Schedule.objects.update_or_create(
            name=SCHEDULE_NAME,
            defaults={
                'func': 'review.tasks.run_venue_discovery_task',
                'schedule_type': Schedule.DAILY,
                'repeats': -1,
                'next_run': next_run,
            },
        )
        verb = 'Created' if created else 'Updated'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {schedule.name}: daily venue discovery at {hour:02d}:00 via qcluster (next run {schedule.next_run:%Y-%m-%d %H:%M}).'
        ))
