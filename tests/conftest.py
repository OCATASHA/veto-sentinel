import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _no_real_ai_key(monkeypatch):
    """Tests never call the real Gemini API, even when the developer's shell has a key."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
