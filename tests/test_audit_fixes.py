from unittest.mock import MagicMock, patch

import pytest

from review.services import local_llm


def ollama_response(status=200, content='{"ok": true}', text=''):
    response = MagicMock()
    response.status_code = status
    response.text = text
    response.json.return_value = {'message': {'content': content}}
    return response


def test_local_model_requests_turn_thinking_off(monkeypatch):
    monkeypatch.delenv('OLLAMA_THINK', raising=False)
    monkeypatch.setattr(local_llm, 'SHARED_QWEN_QUEUE_ENABLED', False)
    with patch('httpx.post', return_value=ollama_response()) as post:
        model, content = local_llm.ollama_chat_json('Return JSON.', max_tokens=100, num_ctx=2048)
    assert content == '{"ok": true}'
    assert post.call_args.kwargs['json']['think'] is False


def test_models_without_a_thinking_option_are_asked_again_without_it(monkeypatch):
    monkeypatch.delenv('OLLAMA_THINK', raising=False)
    monkeypatch.setattr(local_llm, 'SHARED_QWEN_QUEUE_ENABLED', False)
    rejected = ollama_response(status=400, text='{"error":"\\"qwen2.5\\" does not support thinking"}')
    with patch('httpx.post', side_effect=[rejected, ollama_response()]) as post:
        model, content = local_llm.ollama_chat_json('Return JSON.', max_tokens=100, num_ctx=2048)
    assert content == '{"ok": true}' and post.call_count == 2
    assert 'think' not in post.call_args_list[1].kwargs['json']


def test_thinking_can_be_turned_back_on(monkeypatch):
    monkeypatch.setenv('OLLAMA_THINK', 'true')
    monkeypatch.setattr(local_llm, 'SHARED_QWEN_QUEUE_ENABLED', False)
    with patch('httpx.post', return_value=ollama_response()) as post:
        local_llm.ollama_chat_json('Return JSON.', max_tokens=100, num_ctx=2048)
    assert 'think' not in post.call_args.kwargs['json']


def test_other_400_errors_are_not_retried(monkeypatch):
    monkeypatch.delenv('OLLAMA_THINK', raising=False)
    monkeypatch.setattr(local_llm, 'SHARED_QWEN_QUEUE_ENABLED', False)
    with patch('httpx.post', return_value=ollama_response(status=400, text='bad request')) as post:
        with pytest.raises(RuntimeError, match='400'):
            local_llm.ollama_chat_json('Return JSON.', max_tokens=100, num_ctx=2048)
    assert post.call_count == 1


def test_missed_schedules_are_not_replayed():
    from django.conf import settings
    assert settings.Q_CLUSTER['catch_up'] is False


def test_discovery_model_without_thinking_option_is_asked_again(monkeypatch):
    import httpx
    from review.services import venue_discovery as vd
    monkeypatch.setenv('VENUE_DISCOVERY_OLLAMA_MODEL', 'qwen2.5:0.5b-instruct')
    calls = []
    def fake_post(url, json=None, timeout=None):
        calls.append(dict(json))
        if 'think' in json:
            return httpx.Response(400, text='{"error":"model does not support thinking"}', request=httpx.Request('POST', url))
        return httpx.Response(200, json={'message': {'content': '{"name": "X"}'}}, request=httpx.Request('POST', url))
    monkeypatch.setattr(vd.httpx, 'post', fake_post)
    raw = vd.extract_with_local_ollama([vd.FetchedPage(url='https://a.example', title='', text='x')], vd.DiscoveryConfig.from_env())
    assert raw == {'name': 'X'} and len(calls) == 2 and 'think' not in calls[1]
