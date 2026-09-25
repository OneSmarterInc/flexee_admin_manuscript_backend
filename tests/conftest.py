import os
from unittest.mock import patch
import pytest

@pytest.fixture(autouse=True)
def mock_ai_provider():
    with patch.dict(os.environ, {"AI_PROVIDER": "mock", "TEST_BYPASS_ORIGIN": "1"}):
        yield
