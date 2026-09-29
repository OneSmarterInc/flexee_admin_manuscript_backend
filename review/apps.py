from django.apps import AppConfig
from django.db.models.signals import post_migrate


def register_sweeper_schedule(sender, **kwargs):
    from django_q.models import Schedule

    Schedule.objects.get_or_create(
        func='review.tasks.sweep_stuck_jobs_task',
        defaults={
            'schedule_type': Schedule.HOURLY,
            'repeats': -1
        }
    )


def register_retention_schedule(sender, **kwargs):
    from django.core.management import call_command

    call_command('install_retention_schedule', verbosity=0)


class ReviewConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'review'

    def ready(self):
        post_migrate.connect(
            register_sweeper_schedule,
            sender=self
        )
        post_migrate.connect(
            register_retention_schedule,
            sender=self
        )