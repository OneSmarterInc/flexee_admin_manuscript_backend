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

# Production error monitoring is opt-in: no DSN means no events leave the app.
# The monitoring layer strips request bodies, query strings, cookies, user data,
# breadcrumb payloads, exception messages, and local variables before sending.
SENTRY_DSN = os.getenv('SENTRY_DSN', '').strip()
SENTRY_ENVIRONMENT = os.getenv('SENTRY_ENVIRONMENT', ENVIRONMENT).strip() or ENVIRONMENT
SENTRY_RELEASE = (
    os.getenv('SENTRY_RELEASE', '').strip()
    or os.getenv('RELEASE_SHA', '').strip()
)
try:
    SENTRY_TRACES_SAMPLE_RATE = float(os.getenv('SENTRY_TRACES_SAMPLE_RATE', '0'))
except (TypeError, ValueError):
    SENTRY_TRACES_SAMPLE_RATE = 0.0
SENTRY_TRACES_SAMPLE_RATE = max(0.0, min(SENTRY_TRACES_SAMPLE_RATE, 1.0))

from review.monitoring import initialize_sentry
SENTRY_ENABLED = initialize_sentry(
    dsn=SENTRY_DSN,
    environment=SENTRY_ENVIRONMENT,
    release=SENTRY_RELEASE,
    traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
)

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    if PRODUCTION:
        raise RuntimeError('DJANGO_SECRET_KEY must be configured in production')
    SECRET_KEY = 'local-development-only-change-me'

DEBUG = os.getenv('DJANGO_DEBUG', 'false').lower() in {'1', 'true', 'yes', 'on'}
if PRODUCTION:
    DEBUG = False

_allowed_hosts_raw = os.getenv('DJANGO_ALLOWED_HOSTS', '').strip()
if PRODUCTION and not _allowed_hosts_raw:
    raise RuntimeError('DJANGO_ALLOWED_HOSTS must be explicitly configured in production')
if not _allowed_hosts_raw:
    _allowed_hosts_raw = '127.0.0.1,localhost'
ALLOWED_HOSTS = [x.strip() for x in _allowed_hosts_raw.split(',') if x.strip()]
if PRODUCTION and '*' in ALLOWED_HOSTS:
    raise RuntimeError('DJANGO_ALLOWED_HOSTS may not contain * in production')

if PRODUCTION:
    frontend_origins = [
        x.strip()
        for x in os.getenv('FRONTEND_ORIGINS', '').split(',')
        if x.strip()
    ]
    if not frontend_origins:
        raise RuntimeError('FRONTEND_ORIGINS must be explicitly configured in production')
    insecure_origins = [origin for origin in frontend_origins if not origin.startswith('https://')]
    if insecure_origins:
        raise RuntimeError('FRONTEND_ORIGINS must use https:// in production')
    if not os.getenv('ADMIN_SESSION_SECRET', '').strip():
        raise RuntimeError('ADMIN_SESSION_SECRET must be configured in production')
    if '*' in {x.strip() for x in os.getenv('TRUSTED_PROXIES', '').split(',') if x.strip()}:
        raise RuntimeError('TRUSTED_PROXIES may not contain * in production')

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
    if not SECURE_SSL_REDIRECT:
        raise RuntimeError('SECURE_SSL_REDIRECT must remain enabled in production')
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.getenv('SECURE_HSTS_SECONDS', '31536000'))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = 'same-origin'
    X_FRAME_OPTIONS = 'DENY'
    if os.getenv('TRUST_X_FORWARDED_PROTO', 'false').lower() in {'1', 'true', 'yes', 'on'}:
        SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
else:
    SECURE_SSL_REDIRECT = False
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
    SECURE_HSTS_SECONDS = 0
    X_FRAME_OPTIONS = 'SAMEORIGIN'

MAX_MANUSCRIPT_BYTES = int(os.getenv('MAX_MANUSCRIPT_BYTES', str(20 * 1024 * 1024)))
MAX_SUBMISSION_ITEM_BYTES = int(os.getenv('MAX_SUBMISSION_ITEM_BYTES', str(10 * 1024 * 1024)))
if MAX_MANUSCRIPT_BYTES <= 0 or MAX_SUBMISSION_ITEM_BYTES <= 0:
    raise RuntimeError('Upload limits must be positive integers')

DATA_UPLOAD_MAX_MEMORY_SIZE = max(MAX_MANUSCRIPT_BYTES, MAX_SUBMISSION_ITEM_BYTES) + 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = DATA_UPLOAD_MAX_MEMORY_SIZE

_private_media_raw = os.getenv('PRIVATE_MEDIA_ROOT', '').strip()
if PRODUCTION and not _private_media_raw:
    raise RuntimeError(
        'PRIVATE_MEDIA_ROOT must be configured in production and must point outside the application source tree'
    )
MEDIA_ROOT = Path(_private_media_raw).expanduser().resolve() if _private_media_raw else (BASE_DIR / 'media').resolve()
if PRODUCTION:
    base_resolved = BASE_DIR.resolve()
    try:
        MEDIA_ROOT.relative_to(base_resolved)
    except ValueError:
        pass
    else:
        raise RuntimeError('PRIVATE_MEDIA_ROOT must be outside the application source tree in production')

# Private uploads are delivered only through authenticated Django endpoints.
# Do not configure Nginx/Apache/S3 public access for this path.
MEDIA_URL = '/__private_media__/'

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
