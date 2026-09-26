from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('review', '0018_ai_usage_budget'),
    ]

    operations = [
        migrations.AddField(
            model_name='submissiontransfer',
            name='share_review_history',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='submissiontransfer',
            name='review_history_consented_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
