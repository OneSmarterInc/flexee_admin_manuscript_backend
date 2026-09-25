from django.apps import AppConfig


class ReviewConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'review'

    def ready(self):
        try:
            from django_q.models import Schedule
            Schedule.objects.get_or_create(
                func='review.tasks.sweep_stuck_jobs_task',
                defaults={
                    'schedule_type': Schedule.HOURLY,
                    'repeats': -1
                }
            )
        except Exception:
            pass
