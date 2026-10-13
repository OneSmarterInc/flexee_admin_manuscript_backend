"""12 October audit, cleanup: items 19 to 22, the rest of 38 and safe parts of 32."""
import importlib
import os
from datetime import timedelta
from unittest import mock

import pytest
from django.apps import apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client
from django.utils import timezone

from review import manuscript_versions as versions
from review.auth import issue_session
from review.models import (AdminAuthEvent, AuthorAuthEvent, EditorUser, IndexedVenue, Manuscript, ManuscriptVersion,
                           ReadinessAssessment)
from review.services import venue_discovery as vd
from review.services import venue_index as vi
from tests.test_author_dashboard_actions import author_client, env, make_manuscript  # noqa: F401
from tests.test_venue_index import MIS, OPS, FakeHttp, crossref_payload, run_full


# ---------------------------------------------------------------------------
# 19: the version backfill can be rolled back and run again
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
def test_the_backfill_rolls_back_cleanly_and_runs_again():
    from tests.migration_state import BEFORE_REQUIRED_VERSIONS, make_old_manuscript, remove_old_manuscript, schema_at
    migration = importlib.import_module('review.migrations.0038_backfill_manuscript_versions')
    with schema_at(BEFORE_REQUIRED_VERSIONS) as old:
        Version = old.get_model('review', 'ManuscriptVersion')
        Readiness = old.get_model('review', 'ReadinessAssessment')
        m = make_old_manuscript(old)
        migration.forwards(old, None)
        migration.backwards(old, None)
        assert not Version.objects.filter(manuscript_id=m.id).exists()
        assert Readiness.objects.get(manuscript_id=m.id).version_id is None
        migration.forwards(old, None)          # before the fix: a second version 1 (unique error)
        m.refresh_from_db()
        assert Version.objects.filter(manuscript_id=m.id).count() == 1
        assert Version.objects.get(id=m.current_version_id).number == 1
        remove_old_manuscript(old, m.id)


# ---------------------------------------------------------------------------
# 20: a failed new version leaves no file behind
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_failed_new_version_removes_its_file():
    _client, author = author_client()
    m = make_manuscript(author)
    seen = []

    def boom(manuscript, version):
        seen.append((version.file.storage, version.file.name))
        raise RuntimeError('database error after the file was written')
    with mock.patch.object(versions, '_attach_new_version', side_effect=boom):
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                locked = Manuscript.objects.select_for_update().get(id=m.id)
                versions.create_new_version(locked, upload=SimpleUploadedFile('v2.md', b'# New text\n'),
                                            content_bytes=11, sha256='x' * 64, change_note='')
    storage, name = seen[0]
    assert name and not storage.exists(name)


# ---------------------------------------------------------------------------
# 22: a malformed manuscript id is a 400
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_admin_plans_with_a_bad_id_is_a_400(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'x')
    EditorUser.objects.create(email='root@x.org', password_hash='x', platform_superuser=True)
    client = Client()
    client.cookies['flxee_admin_session'] = issue_session('root@x.org')[0]
    assert client.get('/api/admin/plans/', {'manuscript': 'not-a-uuid'}).status_code == 400
    assert client.get('/api/admin/plans/').status_code == 200


# ---------------------------------------------------------------------------
# 32 (part)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_test_origin_bypass_never_works_in_production(monkeypatch):
    monkeypatch.setenv('ADMIN_SESSION_SECRET', 'x')
    monkeypatch.setenv('TEST_BYPASS_ORIGIN', '1')
    EditorUser.objects.create(email='root@x.org', password_hash='x', platform_superuser=True)
    client = Client()
    client.cookies['flxee_admin_session'] = issue_session('root@x.org')[0]
    with mock.patch.dict(os.environ, {'DJANGO_ENV': 'production'}):
        refused = client.post('/api/admin/smtp/', data='{}', content_type='application/json',
                              HTTP_ORIGIN='https://evil.example')
    assert refused.status_code == 403
    allowed = client.post('/api/admin/smtp/', data='{}', content_type='application/json',
                          HTTP_ORIGIN='https://evil.example')
    assert allowed.status_code != 403            # outside production, tests still may skip the check


@pytest.mark.django_db
def test_old_sign_in_counters_are_purged():
    from review.tasks import purge_old_auth_events
    old = AdminAuthEvent.objects.create(remote_hash='a', success=False)
    AdminAuthEvent.objects.filter(id=old.id).update(occurred_at=timezone.now() - timedelta(days=400))
    recent = AdminAuthEvent.objects.create(remote_hash='b', success=False)
    old_author = AuthorAuthEvent.objects.create(remote_hash='c', success=False)
    AuthorAuthEvent.objects.filter(id=old_author.id).update(occurred_at=timezone.now() - timedelta(days=400))
    purge_old_auth_events(timezone.now())
    assert list(AdminAuthEvent.objects.values_list('id', flat=True)) == [recent.id]
    assert not AuthorAuthEvent.objects.exists()


# ---------------------------------------------------------------------------
# 38 (rest)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_one_odd_catalogue_value_fails_one_journal_not_the_run():
    bad = crossref_payload(MIS['issn'])
    bad['message']['counts']['total-dois'] = 'many'
    http = FakeHttp([[MIS, OPS]], crossref={MIS['issn_l']: bad, OPS['issn_l']: crossref_payload(OPS['issn'])})
    run = run_full(http)
    assert run.status == 'completed'
    assert IndexedVenue.objects.get(title=OPS['display_name']).enriched_at is not None
    assert 'Unexpected catalogue data' in IndexedVenue.objects.get(title=MIS['display_name']).last_error


def test_a_page_that_drips_slowly_is_cut_off(monkeypatch):
    import httpx
    monkeypatch.setattr(vd, '_resolve', lambda host: {'93.184.216.34'})
    monkeypatch.setenv('VENUE_DISCOVERY_PER_DOMAIN_DELAY_SECONDS', '0')
    ticks = iter([0, 1000, 1000, 1000, 1000])
    monkeypatch.setattr(vd.time, 'monotonic', lambda: next(ticks))
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text='<html>' + 'x' * 1000 + '</html>', headers={'content-type': 'text/html'})))
    fetcher = vd.SafeFetcher(vd.DiscoveryConfig.from_env(), client=client)
    with pytest.raises(vd.DiscoveryFetchError, match='Timed out reading'):
        fetcher._get('https://j.example/guide')


def test_the_shared_qwen_redis_client_has_timeouts(monkeypatch):
    from review.services import local_llm
    seen = {}

    class Stop(Exception):
        pass

    def fake_from_url(url, **kwargs):
        seen.update(kwargs)
        raise Stop()
    monkeypatch.setattr(local_llm, 'SHARED_QWEN_QUEUE_ENABLED', True)
    monkeypatch.setattr(local_llm.redis, 'from_url', fake_from_url)
    with pytest.raises(Stop):
        local_llm.shared_qwen_chat_json('short prompt', max_tokens=50)
    assert seen.get('socket_timeout') and seen.get('socket_connect_timeout')


@pytest.mark.django_db
def test_only_one_index_run_starts_at_a_time():
    first, created = vi.start_index_run(mode='rules', trigger='command')
    second, created_again = vi.start_index_run(mode='rules', trigger='schedule')
    assert created and not created_again and second.id == first.id
