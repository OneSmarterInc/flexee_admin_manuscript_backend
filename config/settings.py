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
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(BASE_DIR / '.env')

ENVIRONMENT = os.getenv('DJANGO_ENV', 'development').lower()
PRODUCTION = ENVIRONMENT == 'production'

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    if PRODUCTION:
        raise RuntimeError('DJANGO_SECRET_KEY must be configured in production')
    SECRET_KEY = 'local-development-only-change-me'

DEBUG = os.getenv('DJANGO_DEBUG', 'false').lower() in {'1', 'true', 'yes', 'on'}
if PRODUCTION:
    DEBUG = False

ALLOWED_HOSTS = [x.strip() for x in os.getenv('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost').split(',') if x.strip()]

INSTALLED_APPS = ['review', 'django_q']

Q_CLUSTER = {
    'name': 'flexee_q',
    'workers': 4,
    'recycle': 500,
    'timeout': 1800,  # 30 mins
    'retry': 1860,
    'compress': True,
    'save_limit': 250,
    'queue_limit': 500,
    'cpu_affinity': 1,
    'label': 'Django Q',
    'orm': 'default'
}

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'review.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
]

ROOT_URLCONF = 'config.urls'
TEMPLATES = []
WSGI_APPLICATION = 'config.wsgi.application'
ASGI_APPLICATION = 'config.asgi.application'

import sys
TESTING = 'pytest' in sys.modules or 'test' in sys.argv

if os.getenv('DATABASE_URL') and not TESTING:
    import dj_database_url
    DATABASES = {'default': dj_database_url.config(conn_max_age=600, ssl_require=PRODUCTION)}
elif PRODUCTION:
    raise RuntimeError('DATABASE_URL is required in production')
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

if PRODUCTION:
    SECURE_SSL_REDIRECT = os.getenv('SECURE_SSL_REDIRECT', 'true').lower() in {'1', 'true', 'yes', 'on'}
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', '31536000'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    X_FRAME_OPTIONS = 'DENY'
else:
    SECURE_SSL_REDIRECT = False
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
    SECURE_HSTS_SECONDS = 0
    X_FRAME_OPTIONS = 'SAMEORIGIN'

DATA_UPLOAD_MAX_MEMORY_SIZE = int(os.getenv('MAX_MANUSCRIPT_BYTES', str(20 * 1024 * 1024))) + 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = DATA_UPLOAD_MAX_MEMORY_SIZE
MEDIA_ROOT = BASE_DIR / 'media'
MEDIA_URL = '/media/'

if os.getenv('SMTP_HOST', '').strip():
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_HOST = os.getenv('SMTP_HOST', '')
    EMAIL_PORT = int(os.getenv('SMTP_PORT', '587'))
    EMAIL_HOST_USER = os.getenv('SMTP_USERNAME', '')
    EMAIL_HOST_PASSWORD = os.getenv('SMTP_PASSWORD', '')
    EMAIL_USE_TLS = os.getenv('SMTP_USE_TLS', 'true').lower() in {'1', 'true', 'yes', 'on'}
    EMAIL_USE_SSL = os.getenv('SMTP_USE_SSL', 'false').lower() in {'1', 'true', 'yes', 'on'}
else:
    EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'


DEFAULT_FROM_EMAIL = os.getenv('NOTIFY_FROM_EMAIL', 'editor@flexee.org')
