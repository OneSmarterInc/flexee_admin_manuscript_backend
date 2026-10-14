"""Low findings of the 13 October bug review (backend: L1 to L6). Each test failed before its fix."""
import json

import pytest
from django.test import Client

from review.auth import COOKIE_NAME, hash_password, issue_session
from review.models import EditorUser, IndexedVenue, Submission, Venue
from tests.test_author_dashboard_actions import author_client, env, make_manuscript, make_venue  # noqa: F401


def _post(client, path, body):
    return client.post(path, data=body if isinstance(body, str) else json.dumps(body), content_type='application/json')


def _superuser():
    user = EditorUser.objects.create(email='root@example.com', password_hash=hash_password('x' * 14),
                                     platform_superuser=True)
    client = Client(raise_request_exception=False)
    client.cookies[COOKIE_NAME] = issue_session(user.email)[0]
    return client


@pytest.mark.django_db
def test_l1_a_malformed_venue_id_is_not_found_not_a_server_error():
    client, author = author_client()
    client.raise_request_exception = False
    m = make_manuscript(author)
    assert _post(client, f'/api/author/manuscripts/{m.id}/submissions/', {'venue_id': 'not-a-uuid'}).status_code == 404


@pytest.mark.django_db
def test_l1_malformed_ids_in_editor_feedback_are_refused():
    client = _superuser()
    venue = make_venue('FNJ', 'fnj')
    r = _post(client, f'/api/admin/venues/{venue.id}/feedback/', {'assessment_field': 'aims_scope',
                                                                   'venue_submission_id': 'zzz'})
    assert r.status_code == 404
    r = _post(client, f'/api/admin/venues/{venue.id}/feedback/draft-config/', {'feedback_ids': ['zzz']})
    assert r.status_code in (400, 409)


@pytest.mark.django_db
def test_l2_a_json_array_body_is_a_bad_request():
    client = Client(raise_request_exception=False)
    for path in ['/api/author/login/', '/api/author/register/', '/api/author/password-reset/',
                 '/api/admin/login/', '/api/admin/verify-password/']:
        assert _post(client, path, '[]').status_code < 500, path
    assert _post(client, '/api/author/login/', '[]').status_code == 400
    author_session, _author = author_client()
    author_session.raise_request_exception = False
    assert _post(author_session, '/api/author/change-password/', '[1]').status_code == 400


@pytest.mark.django_db
def test_l3_an_open_access_search_lists_each_venue_once():
    venue = make_venue('FNJ', 'fnj')
    Venue.objects.filter(id=venue.id).update(trust_tier=Venue.TIER_CLAIMED)
    for i in range(2):
        IndexedVenue.objects.create(title=f'FNJ {i}', venue=venue, open_access=True, openalex_id=f'S{i}')
    body = Client().get('/api/journals/?open_access=1').json()
    assert len(body['items']) == 1


@pytest.mark.django_db
def test_l4_downloading_a_missing_legacy_file_is_not_found():
    client = _superuser()
    s = Submission.objects.create(status='completed', kind='article', author_name='a', title='t',
                                  manuscript_filename='x.pdf', manuscript_file='submissions/missing.pdf',
                                  manuscript_bytes=1, manuscript_sha256='0')
    assert client.get(f'/api/admin/submissions/{s.id}/download/').status_code == 404


@pytest.mark.django_db
def test_l5_a_non_number_limit_falls_back_to_the_default():
    assert _superuser().get('/api/admin/submissions/?limit=abc').status_code == 200


def test_l6_shared_qwen_settings_are_read_when_used(monkeypatch):
    from review.services import local_llm
    monkeypatch.setenv('SHARED_QWEN_QUEUE_ENABLED', 'true')
    monkeypatch.setenv('SHARED_QWEN_REDIS_URL', 'redis://other:6379/1')
    settings = local_llm._shared_qwen_settings()
    assert settings['enabled'] is True and settings['redis_url'] == 'redis://other:6379/1'
    monkeypatch.setenv('SHARED_QWEN_QUEUE_ENABLED', 'false')
    with pytest.raises(RuntimeError, match='disabled'):
        local_llm.shared_qwen_chat_json('{}', max_tokens=10)
