import os
import subprocess
import sys
from pathlib import Path

from django.http import JsonResponse
from django.test import RequestFactory

from review.auth import set_author_session_cookie


ROOT = Path(__file__).resolve().parents[1]


def _production_env(tmp_path):
    env = os.environ.copy()
    env.update({
        'DJANGO_ENV': 'production',
        'DJANGO_SECRET_KEY': 'prod-test-' + ('aB3!' * 20),
        'DATABASE_URL': 'postgresql://test:test@localhost:5432/test',
        'DJANGO_ALLOWED_HOSTS': 'api.example.test',
        'FRONTEND_ORIGINS': 'https://app.example.test',
        'ADMIN_SESSION_SECRET': 'prod-session-' + ('xY7!' * 20),
        'PRIVATE_MEDIA_ROOT': str(tmp_path),
        'SECURE_SSL_REDIRECT': 'true',
        'SENTRY_DSN': '',
        'TRUSTED_PROXIES': '127.0.0.1',
    })
    return env


def test_production_deploy_check_has_no_security_warnings(tmp_path):
    result = subprocess.run(
        [sys.executable, 'manage.py', 'check', '--deploy'],
        cwd=str(ROOT),
        env=_production_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert 'security.W002' not in output
    assert 'security.W003' not in output
    assert 'security.W009' not in output
    assert 'System check identified no issues' in output


def test_author_session_cookie_is_secure_in_production(monkeypatch):
    monkeypatch.setenv('DJANGO_ENV', 'production')
    monkeypatch.setenv('COOKIE_SECURE', 'false')

    response = JsonResponse({'ok': True})
    set_author_session_cookie(response, 'token', 60)

    cookie = response.cookies['flxee_author_session']
    assert cookie['secure']
    assert cookie['httponly']
    assert cookie['samesite'] == 'Strict'
