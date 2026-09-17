from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name='Submission',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('status', models.CharField(choices=[('processing','Processing'),('completed','Completed'),('failed','Failed')], db_index=True, default='processing', max_length=20)),
                ('kind', models.CharField(choices=[('book','Book'),('article','Article')], max_length=20)),
                ('author_name', models.CharField(max_length=200)),
                ('author_email', models.EmailField(blank=True, max_length=320)),
                ('coauthors', models.TextField(blank=True)),
                ('title', models.CharField(max_length=500)),
                ('declared_sim', models.CharField(blank=True, max_length=300)),
                ('disclosure', models.TextField()),
                ('notes', models.TextField(blank=True)),
                ('attestation', models.BooleanField(default=True)),
                ('manuscript_filename', models.CharField(max_length=500)),
                ('manuscript_bytes', models.BigIntegerField()),
                ('manuscript_sha256', models.CharField(max_length=64)),
                ('decision', models.CharField(blank=True, choices=[('PASS_TO_HUMAN','Pass to human'),('REFER_TO_HUMAN_WITH_FLAGS','Refer with flags'),('RETURN_TO_AUTHOR','Return to author')], max_length=40)),
                ('model', models.CharField(blank=True, max_length=200)),
                ('total_words', models.IntegerField(blank=True, null=True)),
                ('review_record', models.JSONField(blank=True, null=True)),
                ('editor_summary', models.TextField(blank=True)),
                ('author_letter', models.TextField(blank=True)),
                ('error', models.JSONField(blank=True, null=True)),
                ('notification_status', models.CharField(blank=True, max_length=30)),
                ('notification_detail', models.JSONField(blank=True, null=True)),
                ('notified_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={'ordering': ['-created_at']},
        ),
        migrations.CreateModel(
            name='AdminAuthEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('occurred_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('remote_hash', models.CharField(db_index=True, max_length=64)),
                ('success', models.BooleanField()),
                ('detail', models.JSONField(blank=True, default=dict)),
            ],
            options={'ordering': ['-occurred_at']},
        ),
        migrations.CreateModel(
            name='ReviewEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('event_type', models.CharField(max_length=100)),
                ('detail', models.JSONField(blank=True, default=dict)),
                ('submission', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='events', to='review.submission')),
            ],
            options={'ordering': ['created_at', 'id']},
        ),
        migrations.AddIndex(model_name='submission', index=models.Index(fields=['status', '-created_at'], name='review_status_created_idx')),
        migrations.AddIndex(model_name='submission', index=models.Index(fields=['author_email', '-created_at'], name='review_email_created_idx')),
    ]
