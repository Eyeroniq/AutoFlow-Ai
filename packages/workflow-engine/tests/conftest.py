import pytest

from flowforge_engine.providers import MockEmailProvider


@pytest.fixture(autouse=True)
def _clear_mock_outbox():
    MockEmailProvider.clear_outbox()
    yield
    MockEmailProvider.clear_outbox()


@pytest.fixture(autouse=True)
def _no_real_provider_keys(monkeypatch):
    # Keep provider selection deterministic whatever the developer's shell exports.
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
