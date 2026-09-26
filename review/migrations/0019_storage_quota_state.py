from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('review', '0018_ai_usage_budget'),
    ]

    operations = [
        migrations.CreateModel(
            name='StorageQuotaState',
            fields=[
                ('key', models.CharField(default='global', editable=False, max_length=32, primary_key=True, serialize=False)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'storage quota state',
                'verbose_name_plural': 'storage quota state',
            },
        ),
    ]
