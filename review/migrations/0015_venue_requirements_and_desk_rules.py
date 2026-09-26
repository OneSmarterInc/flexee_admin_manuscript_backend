import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('review', '0014_reviewjob_unique_active_job'),
    ]

    operations = [
        migrations.AddField(
            model_name='venueagentconfig',
            name='required_submission_items',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='venueagentconfig',
            name='structured_desk_rejection_rules',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.CreateModel(
            name='SubmissionRequirementFile',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('requirement_key', models.SlugField(max_length=120)),
                ('original_filename', models.CharField(max_length=500)),
                ('file', models.FileField(upload_to='venue_requirement_files/')),
                ('file_bytes', models.BigIntegerField(default=0)),
                ('file_sha256', models.CharField(max_length=64)),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('venue_submission', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='requirement_files', to='review.venuesubmission')),
            ],
            options={
                'ordering': ['requirement_key'],
            },
        ),
        migrations.AddConstraint(
            model_name='submissionrequirementfile',
            constraint=models.UniqueConstraint(fields=('venue_submission', 'requirement_key'), name='review_unique_requirement_file'),
        ),
    ]
