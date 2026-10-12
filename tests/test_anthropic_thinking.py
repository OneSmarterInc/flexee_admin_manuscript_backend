"""Claude 5.5 models think by default; thinking tokens count toward max_tokens (12 Oct e2e with
Haiku 5.5: one answer cut off, one with no text). The review path turns thinking off."""
import sys
import types
from types import SimpleNamespace

import pytest

from review.services import ai_provider


class FakeMessages:
    def __init__(self, response):
        self.response, self.calls = response, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def install_fake_sdk(monkeypatch, response):
    messages = FakeMessages(response)
    fake = types.ModuleType('anthropic')
    fake.Anthropic = lambda **kwargs: SimpleNamespace(messages=messages)
    monkeypatch.setitem(sys.modules, 'anthropic', fake)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-test')
    monkeypatch.setenv('ANTHROPIC_MODEL', 'claude-haiku-5-5')
    return messages


def reply(*blocks, stop_reason='end_turn'):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def test_thinking_is_off_and_only_text_blocks_are_read(monkeypatch):
    monkeypatch.delenv('ANTHROPIC_THINKING', raising=False)
    messages = install_fake_sdk(monkeypatch, reply(
        SimpleNamespace(type='thinking', thinking='Let me think.', text=None),
        SimpleNamespace(type='text', text='{"ok": true}')))
    model, raw = ai_provider._anthropic_chat_json('prompt', max_tokens=650)
    assert (model, raw) == ('claude-haiku-5-5', '{"ok": true}')
    assert messages.calls[0]['thinking'] == {'type': 'disabled'}
    assert 'temperature' not in messages.calls[0]


def test_thinking_can_be_turned_back_on(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_THINKING', 'on')
    messages = install_fake_sdk(monkeypatch, reply(SimpleNamespace(type='text', text='{"ok": true}')))
    ai_provider._anthropic_chat_json('prompt', max_tokens=650)
    assert 'thinking' not in messages.calls[0]


def test_an_empty_or_refused_answer_names_the_stop_reason(monkeypatch):
    install_fake_sdk(monkeypatch, reply(SimpleNamespace(type='thinking', thinking='...'), stop_reason='max_tokens'))
    with pytest.raises(RuntimeError, match='max_tokens'):
        ai_provider._anthropic_chat_json('prompt', max_tokens=650)
    install_fake_sdk(monkeypatch, reply(stop_reason='refusal'))
    with pytest.raises(RuntimeError, match='declined'):
        ai_provider._anthropic_chat_json('prompt', max_tokens=650)
