from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('review', '0008_author_network_foundation'),
    ]

    operations = [
        migrations.AddField(
            model_name='manuscript',
            name='access_token_hash',
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
    ]
