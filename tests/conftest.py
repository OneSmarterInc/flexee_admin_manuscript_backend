import os
from unittest.mock import patch

import pytest

# Settings that differ per machine (AI providers, local models, discovery sources, keys).
# Tests must behave the same everywhere, so these are cleared for every test; a test that
# needs one sets it itself with monkeypatch.
MACHINE_SPECIFIC_PREFIXES = (
    'OLLAMA_', 'VENUE_', 'ANTHROPIC_', 'SHARED_QWEN_', 'AI_', 'OPENAI_', 'QWEN_',
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith(MACHINE_SPECIFIC_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    # Sessions need a signing secret; tests that check a specific one still set their own.
    if not os.environ.get('ADMIN_SESSION_SECRET'):
        monkeypatch.setenv('ADMIN_SESSION_SECRET', 'test-only-session-secret')
    yield


@pytest.fixture(autouse=True)
def mock_ai_provider(isolated_environment):
    with patch.dict(os.environ, {"AI_PROVIDER": "mock", "TEST_BYPASS_ORIGIN": "1"}):
        yield
