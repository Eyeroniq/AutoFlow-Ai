from flowforge_engine.providers import MockEmailProvider, MockLLMProvider
from flowforge_engine.providers.gemini_provider import GeminiProvider
from pydantic import SecretStr

from app.core.config import settings
from app.services.providers import get_execution_services, get_llm_provider


def test_gemini_falls_back_to_mock_without_api_key(caplog):
    # The test env blanks every provider key (see conftest).
    assert settings.GEMINI_API_KEY is None or not settings.GEMINI_API_KEY.get_secret_value()

    provider = get_llm_provider("gemini")

    assert isinstance(provider, MockLLMProvider)
    assert provider.is_mock
    assert "no API key configured for gemini; using MockLLMProvider (mock mode)" in caplog.text


def test_every_llm_provider_is_mock_in_tests():
    for name in ("gemini", "openai", "anthropic"):
        assert isinstance(get_llm_provider(name), MockLLMProvider)


def test_real_adapter_is_used_when_key_is_set(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", SecretStr("test-key"))
    assert isinstance(get_llm_provider("gemini"), GeminiProvider)


def test_execution_services_wire_mocks():
    services = get_execution_services()
    assert isinstance(services.llm("anthropic"), MockLLMProvider)
    assert isinstance(services.email("gmail"), MockEmailProvider)
