import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from unittest.mock import patch
import pytest
from django.test import Client
from django.db import connection
from review.models import Author, AuthorAISettings
from review.auth import issue_author_session
from review.services.author_ai import author_scope, current_selection, selection_identity, Selection, custom_chat, _current
from review.services.ai_provider import ai_chat_json

@pytest.fixture
def accounts(db):
    users = [Author.objects.create(email=f'key{i}@example.com', name=f'Author {i}') for i in range(2)]
    clients = []
    for user in users:
        client = Client()
        client.cookies['flxee_author_session'] = issue_author_session(user.id)[0]
        clients.append(client)
    return users, clients


def save(client, **data):
    return client.post('/api/author/ai-settings/', data=json.dumps(data), content_type='application/json')


def test_private_encrypted_write_only_settings(accounts):
    users, clients = accounts
    r = save(clients[0], provider='openai', model='gpt-5-mini', api_key='secret-alpha')
    assert r.status_code == 200 and r.json()['has_key']
    assert 'secret-alpha' not in r.content.decode() and 'api_key' not in r.json()
    assert clients[1].get('/api/author/ai-settings/').json()['has_key'] is False
    with connection.cursor() as cur:
        cur.execute('SELECT api_key FROM review_authoraisettings')
        stored = cur.fetchone()[0]
    assert stored.startswith('enc:v1:') and 'secret-alpha' not in stored
    assert save(clients[1], provider='openai', model='test', api_key='beta', author_id=str(users[0].id)).status_code == 400
    assert clients[1].delete('/api/author/ai-settings/').status_code == 200
    assert AuthorAISettings.objects.get(author=users[0]).api_key == 'secret-alpha'
    assert save(clients[0], provider='claude', model='test', api_key='').status_code == 400
    assert save(clients[0], provider='openai', model='gpt-5-mini', api_key='').status_code == 200
    assert clients[0].delete('/api/author/ai-settings/').json()['provider'] == 'default'
    assert not AuthorAISettings.objects.exists()


def test_auth_and_csrf(accounts):
    users, clients = accounts
    assert Client().get('/api/author/ai-settings/').status_code == 401
    c = Client(enforce_csrf_checks=True)
    c.cookies['flxee_author_session'] = issue_author_session(users[0].id)[0]
    assert save(c, provider='default').status_code == 403


def test_account_routing_and_context_reset(accounts):
    users, _ = accounts
    for i, user in enumerate(users):
        AuthorAISettings.objects.create(author=user, provider='openai', model=f'model-{i}', api_key=f'key-{i}')
    seen = []
    def fake(s, *args):
        seen.append((s.model, s.key))
        return s.model, '{}'
    with patch('review.services.author_ai.custom_chat', side_effect=fake), patch('review.services.ai_provider._tracked_call') as global_call:
        with author_scope(users[0].id):
            identity = selection_identity()
            ai_chat_json('JSON', force_provider='anthropic')
            with author_scope(users[1].id): ai_chat_json('JSON')
            assert selection_identity() == identity
        assert current_selection() is None
        global_call.assert_not_called()
    assert seen == [('model-0','key-0'),('model-1','key-1')]
    with pytest.raises(ValueError):
        with author_scope(users[0].id): raise ValueError('test')
    assert current_selection() is None


def test_default_is_qwen_even_if_global_is_llama(accounts, monkeypatch):
    users, _ = accounts
    monkeypatch.setenv('OLLAMA_MODEL', 'llama3.2:1b')
    with patch('review.services.ai_provider.ollama_chat_json', return_value=('qwen3:1.7b','{}')) as call:
        with author_scope(users[0].id): ai_chat_json('JSON')
    assert call.call_args.kwargs['model_override'] == 'qwen3:1.7b'


def test_concurrent_contexts_do_not_share_keys():
    def run(key):
        token = _current.set(Selection(key, 'openai', 'test', key))
        try:
            return current_selection().key
        finally: _current.reset(token)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run,['alpha','beta'])) == ['alpha','beta']
    assert current_selection() is None

@pytest.mark.parametrize('provider', ['openai','claude','gemini','grok','groq','deepseek'])
def test_custom_provider_uses_own_key_and_no_redirect(provider):
    response = type('Response', (), {'status_code':200, 'json':lambda self: (
        {'stop_reason':'end_turn','content':[{'type':'text','text':'{}'}]} if provider == 'claude'
        else {'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]})})()
    with patch('httpx.post', return_value=response) as call:
        assert custom_chat(Selection('a',provider,'model','private-key'),'JSON',700,30) == ('model','{}')
    headers=call.call_args.kwargs['headers']
    assert 'private-key' in (headers.get('Authorization','')+headers.get('x-api-key',''))
    assert call.call_args.kwargs['follow_redirects'] is False


def test_provider_errors_do_not_expose_keys_or_fallback():
    response=type('Response', (), {'status_code':401})()
    with patch('httpx.post', return_value=response):
        with pytest.raises(RuntimeError) as err:
            custom_chat(Selection('a','openai','model','secret'),'JSON',700,30)
    assert 'secret' not in str(err.value)


def test_model_catalog_and_pair_validation(accounts):
    _, clients = accounts
    catalog = clients[0].get('/api/author/ai-settings/').json()['models']
    assert len(catalog) == 11
    for item in catalog:
        response = save(clients[0], provider=item['provider'], model=item['model'], api_key='own-key')
        assert response.status_code == 200
    assert save(clients[0], provider='gemini', model='grok-4.7', api_key='own-key').status_code == 400


@pytest.mark.parametrize('provider', ['openai', 'claude', 'gemini', 'grok', 'groq', 'deepseek'])
def test_provider_only_selects_internal_default(accounts, provider):
    _, clients = accounts
    response = save(clients[0], provider=provider, api_key='private-key')
    assert response.status_code == 200
    assert response.json()['provider'] == provider
    assert response.json()['model']
    assert 'private-key' not in response.content.decode()


def test_groq_routes_to_groq_not_xai(accounts):
    users, clients = accounts
    assert save(clients[0], provider='groq', api_key='groq-test-key').status_code == 200
    assert clients[1].get('/api/author/ai-settings/').json()['has_key'] is False
    response = type('Response', (), {'status_code': 200, 'json': lambda self: {
        'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok": true}'}}]}})()
    with patch('httpx.post', return_value=response) as call:
        with author_scope(users[0].id):
            model, raw = ai_chat_json('Return JSON')
    assert model == 'openai/gpt-oss-120b'
    assert json.loads(raw) == {'ok': True}
    assert call.call_args.args[0] == 'https://api.groq.com/openai/v1/chat/completions'
    assert call.call_args.kwargs['headers']['Authorization'] == 'Bearer groq-test-key'
    assert call.call_args.kwargs['json']['reasoning_effort'] == 'low'
    assert call.call_args.kwargs['json']['include_reasoning'] is False
    assert save(clients[0], provider='grok', api_key='').status_code == 400
