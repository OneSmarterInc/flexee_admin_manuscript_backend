from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('review', '0015_venue_requirements_and_desk_rules'),
    ]

    operations = [
        migrations.AddField(
            model_name='venueagentconfig',
            name='retention_days',
            field=models.PositiveIntegerField(
                blank=True,
                help_text='Days to retain venue submission content after formal submission. Blank disables automatic expiry.',
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='manuscript',
            name='content_purged_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='venuesubmission',
            name='retention_expires_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='venuesubmission',
            name='retention_purged_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
