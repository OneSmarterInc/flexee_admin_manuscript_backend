from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('review', '0019_storage_quota_state'),
    ]

    operations = [
        migrations.AddField(
            model_name='editorfeedback',
            name='venue_config',
            field=models.ForeignKey(
                blank=True,
                help_text='Venue Agent configuration the editor was correcting.',
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='editor_feedback',
                to='review.venueagentconfig',
            ),
        ),
        migrations.AddField(
            model_name='editorfeedback',
            name='applied_to_config',
            field=models.ForeignKey(
                blank=True,
                help_text='Inactive draft configuration created from this feedback, if any.',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='feedback_sources',
                to='review.venueagentconfig',
            ),
        ),
    ]
