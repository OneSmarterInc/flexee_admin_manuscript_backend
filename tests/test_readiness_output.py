import pytest
from review.services.local_llm import ollama_chat_json
from review.services.readiness_schema import readiness_schema

class Response:
    status_code = 200
    def __init__(self, reason): self.reason = reason
    def json(self):
        return {'message': {'content': '{}'}, 'done_reason': self.reason}


def test_readiness_schema_sent_to_ollama(monkeypatch):
    monkeypatch.setenv('SHARED_QWEN_QUEUE_ENABLED', 'false')
    captured = {}
    def post(*args, **kwargs):
        captured.update(kwargs['json'])
        return Response('stop')
    monkeypatch.setattr('httpx.post', post)
    ollama_chat_json('Read manuscript', response_schema=readiness_schema())
    assert captured['format']['type'] == 'object'
    assert 'findings' in captured['format']['required']
    assert captured['format']['additionalProperties'] is False


def test_truncated_response_is_not_accepted(monkeypatch):
    monkeypatch.setenv('SHARED_QWEN_QUEUE_ENABLED', 'false')
    monkeypatch.setattr('httpx.post', lambda *a, **kw: Response('length'))
    with pytest.raises(RuntimeError, match='output token limit'):
        ollama_chat_json('Read manuscript')
