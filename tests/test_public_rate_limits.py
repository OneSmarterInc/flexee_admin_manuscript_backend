"""8 October review, items 3.2 and 3.3: SameSite readiness check and the journal index rate limit."""
from datetime import timedelta
from unittest import mock

import pytest
from django.test import Client
from django.utils import timezone

from review.models import PublicRequestWindow
from review.public_rate import purge_old_windows
from tests.test_readiness_and_review_model import line_for, run_checker

SAMESITE_RULE = 'COOKIE_SAMESITE must be Strict, Lax or None'
ORIGIN_RULE = 'COOKIE_SAMESITE=None requires every allowed origin'


# 3.3: the public journal index is limited per network.

@pytest.mark.django_db
def test_journal_search_is_limited_per_network(monkeypatch):
    monkeypatch.setenv('JOURNAL_INDEX_REQUESTS_PER_WINDOW', '3')
    client = Client(REMOTE_ADDR='203.0.113.7')
    assert [client.get('/api/journals/').status_code for _ in range(3)] == [200, 200, 200]
    blocked = client.get('/api/journals/', {'page': 2})
    assert blocked.status_code == 429
    assert 0 < int(blocked['Retry-After']) <= 3600
    assert 'Too many requests' in blocked.json()['detail']
    # Another network is unaffected.
    assert Client(REMOTE_ADDR='198.51.100.9').get('/api/journals/').status_code == 200


@pytest.mark.django_db
def test_journal_pages_share_the_same_limit(monkeypatch):
    monkeypatch.setenv('JOURNAL_INDEX_REQUESTS_PER_WINDOW', '2')
    client = Client(REMOTE_ADDR='203.0.113.8')
    client.get('/api/journals/')
    assert client.get('/api/journals/v/no-such-journal/').status_code == 404  # counted, not blocked
    assert client.get('/api/journals/v/no-such-journal/').status_code == 429
    assert client.get('/api/journals/i/00000000-0000-0000-0000-000000000000/').status_code == 429


@pytest.mark.django_db
def test_default_limit_is_generous_and_uses_one_row_per_window():
    client = Client(REMOTE_ADDR='203.0.113.10')
    assert all(client.get('/api/journals/', {'page': n}).status_code == 200 for n in range(1, 51))
    window = PublicRequestWindow.objects.get(scope='journal_index')
    assert window.count == 50


@pytest.mark.django_db
def test_behind_a_trusted_proxy_each_visitor_is_counted_separately(monkeypatch):
    monkeypatch.setenv('JOURNAL_INDEX_REQUESTS_PER_WINDOW', '1')
    monkeypatch.setenv('TRUSTED_PROXIES', '127.0.0.1')
    first = Client(REMOTE_ADDR='127.0.0.1', HTTP_X_FORWARDED_FOR='203.0.113.20')
    second = Client(REMOTE_ADDR='127.0.0.1', HTTP_X_FORWARDED_FOR='203.0.113.21')
    assert first.get('/api/journals/').status_code == 200
    assert second.get('/api/journals/').status_code == 200
    assert first.get('/api/journals/').status_code == 429


@pytest.mark.django_db
def test_old_windows_are_purged():
    old = PublicRequestWindow.objects.create(scope='journal_index', remote_hash='a' * 64,
                                             window_start=timezone.now() - timedelta(days=2), count=9)
    current = PublicRequestWindow.objects.create(scope='journal_index', remote_hash='b' * 64,
                                                 window_start=timezone.now(), count=1)
    assert purge_old_windows() == 1
    assert list(PublicRequestWindow.objects.values_list('id', flat=True)) == [current.id]
    assert not PublicRequestWindow.objects.filter(id=old.id).exists()


# 3.2: verify_production_readiness checks COOKIE_SAMESITE.

def test_default_samesite_passes_and_skips_origin_rule():
    out = run_checker({'FRONTEND_ORIGINS': 'https://manuscript.flexee.org'})
    assert line_for(out, SAMESITE_RULE).startswith('[PASS]')
    assert line_for(out, ORIGIN_RULE) == ''


def test_unknown_samesite_value_fails():
    out = run_checker({'COOKIE_SAMESITE': 'Loose', 'FRONTEND_ORIGINS': 'https://manuscript.flexee.org'})
    assert line_for(out, SAMESITE_RULE).startswith('[FAIL]')


def test_samesite_none_with_exact_origins_passes():
    out = run_checker({'COOKIE_SAMESITE': 'None', 'ADMIN_ALLOWED_ORIGINS': '',
                       'FRONTEND_ORIGINS': 'https://manuscript.flexee.org,https://admin.flexee.org:8443'})
    assert line_for(out, ORIGIN_RULE).startswith('[PASS]')


@pytest.mark.parametrize('origin', [
    'https://*.vercel.app', 'http://manuscript.flexee.org', 'https://manuscript.flexee.org/app',
    'https://localhost:5173', 'https://127.0.0.1', 'null',
])
def test_samesite_none_with_a_loose_origin_fails(origin):
    out = run_checker({'COOKIE_SAMESITE': 'none', 'ADMIN_ALLOWED_ORIGINS': '',
                       'FRONTEND_ORIGINS': f'https://manuscript.flexee.org,{origin}'})
    line = line_for(out, ORIGIN_RULE)
    assert line.startswith('[FAIL]') and origin in line


def test_samesite_none_also_checks_legacy_admin_origins():
    out = run_checker({'COOKIE_SAMESITE': 'None', 'FRONTEND_ORIGINS': 'https://manuscript.flexee.org',
                       'ADMIN_ALLOWED_ORIGINS': 'http://admin.flexee.org'})
    assert line_for(out, ORIGIN_RULE).startswith('[FAIL]')


def test_empty_trusted_proxies_warns():
    production = {'DJANGO_ENV': 'production'}
    assert '[WARN] TRUSTED_PROXIES is empty' in run_checker({**production, 'TRUSTED_PROXIES': ''})
    assert 'TRUSTED_PROXIES is empty' not in run_checker({**production, 'TRUSTED_PROXIES': '127.0.0.1'})
    assert 'TRUSTED_PROXIES is empty' not in run_checker({'DJANGO_ENV': 'development', 'TRUSTED_PROXIES': ''})


# SMTP configured in the Admin UI satisfies the readiness check.

SMTP_RULE = 'SMTP must be configured for real production email'


@pytest.mark.django_db
def test_smtp_saved_in_admin_passes_readiness():
    from review.models import SMTPSettings
    assert line_for(run_checker({'SMTP_HOST': ''}), SMTP_RULE).startswith('[FAIL]')
    SMTPSettings.objects.create(host='smtp.example.org', port=587)
    assert line_for(run_checker({'SMTP_HOST': ''}), SMTP_RULE).startswith('[PASS]')


@pytest.mark.django_db
def test_smtp_host_in_env_still_passes_readiness():
    assert line_for(run_checker({'SMTP_HOST': 'smtp.example.org'}), SMTP_RULE).startswith('[PASS]')
