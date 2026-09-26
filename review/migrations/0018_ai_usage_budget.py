from decimal import Decimal
import uuid

from django.db import migrations, models


def create_budget_state(apps, schema_editor):
    BudgetState = apps.get_model('review', 'AIBudgetState')
    BudgetState.objects.get_or_create(key='global')


class Migration(migrations.Migration):

    dependencies = [
        ('review', '0017_audit_event'),
    ]

    operations = [
        migrations.CreateModel(
            name='AIBudgetState',
            fields=[
                ('key', models.CharField(default='global', editable=False, max_length=32, primary_key=True, serialize=False)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'AI budget state',
                'verbose_name_plural': 'AI budget state',
            },
        ),
        migrations.CreateModel(
            name='AIUsageEvent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('provider', models.CharField(db_index=True, max_length=40)),
                ('model', models.CharField(blank=True, db_index=True, max_length=200)),
                ('operation', models.CharField(db_index=True, default='ai_chat', max_length=100)),
                ('status', models.CharField(choices=[('reserved', 'Reserved'), ('completed', 'Completed'), ('failed', 'Failed'), ('blocked', 'Blocked')], db_index=True, max_length=20)),
                ('input_tokens', models.BigIntegerField(default=0)),
                ('output_tokens', models.BigIntegerField(default=0)),
                ('total_tokens', models.BigIntegerField(default=0)),
                ('estimated_max_cost_usd', models.DecimalField(decimal_places=6, default=Decimal('0'), max_digits=14)),
                ('actual_cost_usd', models.DecimalField(decimal_places=6, default=Decimal('0'), max_digits=14)),
                ('priced', models.BooleanField(default=False)),
                ('usage_estimated', models.BooleanField(default=False)),
                ('error_type', models.CharField(blank=True, max_length=100)),
            ],
            options={
                'ordering': ['-created_at', '-id'],
            },
        ),
        migrations.AddIndex(
            model_name='aiusageevent',
            index=models.Index(fields=['provider', '-created_at'], name='review_ai_provider_time_idx'),
        ),
        migrations.AddIndex(
            model_name='aiusageevent',
            index=models.Index(fields=['status', '-created_at'], name='review_ai_status_time_idx'),
        ),
        migrations.AddIndex(
            model_name='aiusageevent',
            index=models.Index(fields=['operation', '-created_at'], name='review_ai_operation_time_idx'),
        ),
        migrations.RunPython(create_budget_state, migrations.RunPython.noop),
    ]
