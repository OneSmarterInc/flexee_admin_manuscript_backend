from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone
from django_q.models import Schedule


class Command(BaseCommand):
    help = 'Install or refresh the hourly per-venue retention purge schedule for Django-Q.'

    def handle(self, *args, **options):
        schedule, created = Schedule.objects.update_or_create(
            name='flexee-retention-sweep',
            defaults={
                'func': 'review.tasks.sweep_retention_task',
                'schedule_type': Schedule.HOURLY,
                'repeats': -1,
                'next_run': timezone.now() + timedelta(hours=1),
            },
        )
        verb = 'Created' if created else 'Updated'
        self.stdout.write(
            self.style.SUCCESS(
                f'{verb} {schedule.name}: hourly retention sweep via qcluster.'
            )
        )
