from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('review', '0045_journal_rule_check')]
    operations = [migrations.CreateModel(name='RevokedSession', fields=[
        ('token_hash', models.CharField(max_length=64, primary_key=True, serialize=False)),
        ('expires_at', models.DateTimeField(db_index=True)),
    ])]
