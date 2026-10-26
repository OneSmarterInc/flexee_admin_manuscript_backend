"""Store original ABDC JQL source rows separately from verified venue facts."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('review', '0043_version_links_required')]

    operations = [
        migrations.CreateModel(
            name='ABDCJournal',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('edition', models.PositiveSmallIntegerField(default=2025, db_index=True)),
                ('source_row', models.PositiveIntegerField()),
                ('title', models.CharField(max_length=500)),
                ('publisher', models.CharField(blank=True, max_length=300)),
                ('issn', models.CharField(blank=True, max_length=30)),
                ('issn_online', models.CharField(blank=True, max_length=30)),
                ('year_inception', models.PositiveSmallIntegerField(blank=True, null=True)),
                ('for_code', models.CharField(blank=True, max_length=30)),
                ('rating', models.CharField(max_length=2, choices=[('A*', 'A*'), ('A', 'A'), ('B', 'B'), ('C', 'C')], db_index=True)),
                ('source_url', models.URLField(max_length=1000)),
                ('source_file', models.CharField(max_length=255)),
                ('match_status', models.CharField(max_length=24, db_index=True, choices=[('matched','Matched'), ('pending_review','Pending review'), ('unmatched','Unmatched')], default='unmatched')),
                ('match_method', models.CharField(max_length=32, blank=True)),
                ('candidates', models.JSONField(default=list, blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('indexed_venue', models.ForeignKey(to='review.indexedvenue', null=True, blank=True,
                    related_name='abdc_ratings', on_delete=django.db.models.deletion.SET_NULL)),
            ],
            options={'ordering': ['title'],
                     'constraints': [models.UniqueConstraint(fields=['edition','source_row'], name='unique_abdc_edition_source_row')]},
        ),
    ]
