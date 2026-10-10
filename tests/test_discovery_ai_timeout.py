from types import SimpleNamespace
from unittest.mock import patch

from review.services import venue_discovery


def _config():
    return SimpleNamespace(ai_provider='shared_qwen', max_ai_chars=2000)


def _pages():
    return [SimpleNamespace(url='https://example.org', title='Guide', text='Submit here.')]


def test_discovery_extraction_timeout_comes_from_env(monkeypatch):
    monkeypatch.setenv('VENUE_DISCOVERY_AI_TIMEOUT', '600')
    with patch('review.services.ai_provider.ai_chat_json', return_value=('m', '{"name": "X"}')) as chat:
        assert venue_discovery.extract_with_ai(_pages(), _config()) == {'name': 'X'}
    assert chat.call_args.kwargs['timeout'] == 600


def test_discovery_extraction_timeout_defaults_to_180(monkeypatch):
    monkeypatch.delenv('VENUE_DISCOVERY_AI_TIMEOUT', raising=False)
    with patch('review.services.ai_provider.ai_chat_json', return_value=('m', '{"name": "X"}')) as chat:
        venue_discovery.extract_with_ai(_pages(), _config())
    assert chat.call_args.kwargs['timeout'] == 180
