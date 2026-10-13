"""Secrets kept in the database are stored encrypted (audit item 28).

EncryptedCharField encrypts on save and decrypts on load, so code that reads `smtp.password` keeps
working. Values are Fernet tokens with an `enc:v1:` prefix; a value without the prefix (stored before
this field existed) is read as it is and encrypted the next time it is saved.

The key is SECRET_FIELD_KEY from the environment (a Fernet key), or else derived from Django's
SECRET_KEY. Changing either key makes stored secrets unreadable, so re-enter the SMTP password after
a key change.
"""
import base64
import hashlib
import os

from django.conf import settings
from django.db import models

PREFIX = 'enc:v1:'


def _fernet():
    from cryptography.fernet import Fernet
    key = os.getenv('SECRET_FIELD_KEY', '').strip()
    if not key:
        digest = hashlib.sha256(b'flexee.secret-field:' + settings.SECRET_KEY.encode('utf-8')).digest()
        key = base64.urlsafe_b64encode(digest).decode('ascii')
    return Fernet(key.encode('ascii'))


def encrypt_secret(value):
    if not value or str(value).startswith(PREFIX):
        return value
    return PREFIX + _fernet().encrypt(str(value).encode('utf-8')).decode('ascii')


def decrypt_secret(value):
    if not value or not str(value).startswith(PREFIX):
        return value
    from cryptography.fernet import InvalidToken
    try:
        return _fernet().decrypt(str(value)[len(PREFIX):].encode('ascii')).decode('utf-8')
    except InvalidToken:
        return ''  # the key changed: the secret has to be entered again


class EncryptedCharField(models.CharField):
    def from_db_value(self, value, expression, connection):
        return decrypt_secret(value)

    def to_python(self, value):
        return decrypt_secret(super().to_python(value))

    def get_prep_value(self, value):
        return encrypt_secret(super().get_prep_value(value))
