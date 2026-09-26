from django.db import migrations, models
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ('review', '0016_per_venue_retention'),
    ]

    operations = [
        migrations.CreateModel(
            name='AuditEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('occurred_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('actor_id', models.UUIDField(blank=True, db_index=True, null=True)),
                ('actor_email', models.EmailField(blank=True, db_index=True, max_length=320)),
                ('actor_role', models.CharField(blank=True, max_length=40)),
                ('action', models.CharField(db_index=True, max_length=120)),
                ('resource_type', models.CharField(blank=True, db_index=True, max_length=80)),
                ('resource_id', models.CharField(blank=True, db_index=True, max_length=100)),
                ('organization_id', models.UUIDField(blank=True, db_index=True, null=True)),
                ('venue_id', models.UUIDField(blank=True, db_index=True, null=True)),
                ('venue_submission_id', models.UUIDField(blank=True, db_index=True, null=True)),
                ('manuscript_id', models.UUIDField(blank=True, db_index=True, null=True)),
                ('remote_hash', models.CharField(blank=True, max_length=64)),
                ('detail', models.JSONField(blank=True, default=dict)),
            ],
            options={
                'ordering': ['-occurred_at', '-id'],
            },
        ),
        migrations.AddIndex(
            model_name='auditevent',
            index=models.Index(fields=['organization_id', '-occurred_at'], name='review_audit_org_time_idx'),
        ),
        migrations.AddIndex(
            model_name='auditevent',
            index=models.Index(fields=['venue_id', '-occurred_at'], name='review_audit_venue_time_idx'),
        ),
        migrations.AddIndex(
            model_name='auditevent',
            index=models.Index(fields=['venue_submission_id', '-occurred_at'], name='review_audit_sub_time_idx'),
        ),
    ]
