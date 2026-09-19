import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env(path):
    if not path.exists():
        return
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_env(BASE_DIR / '.env')

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'local-dev-only-change-me')
DEBUG = os.getenv('DJANGO_DEBUG', 'true').lower() in {'1', 'true', 'yes', 'on'}
ALLOWED_HOSTS = [x.strip() for x in os.getenv('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost').split(',') if x.strip()]

INSTALLED_APPS = [
    'review',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'review.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
]

ROOT_URLCONF = 'config.urls'
TEMPLATES = []
WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

if 'DATABASE_URL' in os.environ:
    import dj_database_url
    _require_ssl = not DEBUG  # SSL required in production, not needed locally
    DATABASES = {
        'default': dj_database_url.config(conn_max_age=600, ssl_require=_require_ssl)
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
            'OPTIONS': {'timeout': 20},
        }
    }

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

DATA_UPLOAD_MAX_MEMORY_SIZE = int(os.getenv('MAX_MANUSCRIPT_BYTES', str(20 * 1024 * 1024))) + 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = DATA_UPLOAD_MAX_MEMORY_SIZE

MEDIA_ROOT = BASE_DIR / 'media'
MEDIA_URL = '/media/'

EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
if os.getenv('SMTP_HOST', '').strip():
    EMAIL_HOST = os.getenv('SMTP_HOST', '')
    EMAIL_PORT = int(os.getenv('SMTP_PORT', '587'))
    EMAIL_HOST_USER = os.getenv('SMTP_USERNAME', '')
    EMAIL_HOST_PASSWORD = os.getenv('SMTP_PASSWORD', '')
    EMAIL_USE_TLS = os.getenv('SMTP_USE_TLS', 'true').lower() in {'1', 'true', 'yes', 'on'}
    EMAIL_USE_SSL = os.getenv('SMTP_USE_SSL', 'false').lower() in {'1', 'true', 'yes', 'on'}

DEFAULT_FROM_EMAIL = os.getenv('NOTIFY_FROM_EMAIL', 'editor@flexee.org')
