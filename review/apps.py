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


class ReviewConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'review'

    def ready(self):
        post_migrate.connect(
            register_sweeper_schedule,
            sender=self
        )
