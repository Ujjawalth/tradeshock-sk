import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Tests never call the real API.
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ.pop("AI_ACCESS_PASSWORD", None)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_ai_request_flag():
    """The web app sets this per request; reset it so tests don't leak state."""
    import ai
    token = ai.request_ai_allowed.set(True)
    yield
    try:
        ai.request_ai_allowed.reset(token)
    except ValueError:
        ai.request_ai_allowed.set(True)
