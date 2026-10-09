import json
from unittest.mock import patch

import pytest

from review.services import local_llm
from review.services.local_llm import _json_payload


@pytest.mark.parametrize('raw', [
    '```json\n{\n  "ok": true\n}\n```',
    '```\n{"ok": true}\n```',
    '{"ok": true}',
    'Here is the result:\n{"ok": true}\nHope this helps.',
    '<think>x</think>```json\n{"ok": true}\n```',
])
def test_json_payload_unwraps_fences_and_prose(raw):
    assert json.loads(_json_payload(local_llm._strip_thinking(raw))) == {'ok': True}


def test_json_payload_leaves_non_json_unchanged():
    assert _json_payload('no json here') == 'no json here'


def test_shared_qwen_reply_in_a_code_fence_parses(monkeypatch):
    # The exact reply the production worker gave on 9 Oct.
    monkeypatch.setattr(local_llm, 'SHARED_QWEN_QUEUE_ENABLED', True)

    class FakeRedis:
        def ping(self):
            return True

        def rpush(self, *_):
            return 1

        def delete(self, *_):
            return 1

    monkeypatch.setattr(local_llm.redis, 'from_url', lambda *a, **k: FakeRedis())
    with patch.object(local_llm, '_wait_for_shared_result',
                      return_value={'model': 'qwen2.5-shared', 'content': '```json\n{\n  "ok": true\n}\n```'}):
        model, content = local_llm.shared_qwen_chat_json('prompt', max_tokens=50, timeout=5)
    assert model == 'qwen2.5-shared'
    assert json.loads(content) == {'ok': True}
