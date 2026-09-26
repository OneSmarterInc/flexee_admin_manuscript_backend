from django.db import migrations, models
from django.db.models import Count
from django.utils import timezone


def collapse_duplicate_active_jobs(apps, schema_editor):
    ReviewJob = apps.get_model('review', 'ReviewJob')
    groups = (
        ReviewJob.objects.filter(status__in=['queued', 'processing'])
        .values('job_type', 'reference_id')
        .annotate(total=Count('id'))
        .filter(total__gt=1)
    )
    for group in groups.iterator():
        jobs = ReviewJob.objects.filter(
            job_type=group['job_type'],
            reference_id=group['reference_id'],
            status__in=['queued', 'processing'],
        ).order_by('-created_at', '-id')
        keep = jobs.first()
        if keep is None:
            continue
        jobs.exclude(id=keep.id).update(
            status='failed',
            error_message='Superseded while enforcing one active job per operation.',
            completed_at=timezone.now(),
        )


class Migration(migrations.Migration):
    dependencies = [
        ('review', '0013_reviewjob'),
    ]

    operations = [
        migrations.RunPython(collapse_duplicate_active_jobs, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='reviewjob',
            constraint=models.UniqueConstraint(
                fields=('job_type', 'reference_id'),
                condition=models.Q(status__in=['queued', 'processing']),
                name='review_unique_active_job',
            ),
        ),
    ]
