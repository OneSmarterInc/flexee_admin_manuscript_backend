"""Audit item 28: the SMTP password is stored encrypted. Existing passwords are encrypted in place."""
import review.secret_fields
from django.db import migrations


def encrypt_existing(apps, schema_editor):
    SMTPSettings = apps.get_model('review', 'SMTPSettings')
    for row in SMTPSettings.objects.all():
        row.save(update_fields=['password'])  # the field encrypts on save; plain values are read as they are


def decrypt_existing(apps, schema_editor):
    from review.secret_fields import decrypt_secret
    with schema_editor.connection.cursor() as cursor:
        cursor.execute('SELECT id, password FROM review_smtpsettings')
        for row_id, stored in cursor.fetchall():
            cursor.execute('UPDATE review_smtpsettings SET password = %s WHERE id = %s',
                           [decrypt_secret(stored) or '', row_id])


class Migration(migrations.Migration):

    dependencies = [
        ("review", "0041_editoruser_totp_last_step"),
    ]

    operations = [
        migrations.AlterField(
            model_name="smtpsettings",
            name="password",
            field=review.secret_fields.EncryptedCharField(blank=True, max_length=600),
        ),
        migrations.RunPython(encrypt_existing, decrypt_existing),
    ]
