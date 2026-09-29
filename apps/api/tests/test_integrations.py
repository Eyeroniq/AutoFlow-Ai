"""/api/integrations: encrypted storage, masking, connect/disconnect, and the /test call."""

import json
import logging

import pytest
from flowforge_engine import ProviderError
from flowforge_engine.providers.mock import MockLLMProvider
from sqlalchemy import select

from app.core.config import settings
from app.core.crypto import CredentialCipher, CredentialDecryptionError, get_cipher
from app.core.logging import JSONFormatter, register_secret
from app.models.credential import Credential
from app.services.credentials import mask_secret

GEMINI_KEY = "AIzaSyD-test-key-0123456789abcdefghij"
APP_PASSWORD = "abcd efgh ijkl mnop"


def by_provider(listing):
    return {item["provider"]: item for item in listing}


async def test_connect_stores_ciphertext_and_returns_only_masked(client, user, db_session):
    response = await client.post(
        "/api/integrations/gemini/connect", json={"api_key": GEMINI_KEY, "model": "gemini-3.5-flash-lite"},
        headers=user.headers,
    )
    assert response.status_code == 200, response.text
    assert GEMINI_KEY not in response.text
    body = response.json()
    assert body["connected"] is True and body["source"] == "user" and body["status"] == "connected"
    assert body["masked"] == {"api_key": "AIz...ghij", "model": "gemini-3.5-flash-lite"}

    [row] = list(await db_session.scalars(select(Credential).where(Credential.provider == "gemini")))
    assert GEMINI_KEY not in row.encrypted_value
    assert get_cipher().decrypt(row.encrypted_value) == {"api_key": GEMINI_KEY, "model": "gemini-3.5-flash-lite"}


async def test_listing_masks_and_reports_sources(client, user):
    await client.post("/api/integrations/groq/connect", json={"api_key": "gsk_test_0123456789abcdef"}, headers=user.headers)
    response = await client.get("/api/integrations", headers=user.headers)
    assert response.status_code == 200
    assert "gsk_test_0123456789abcdef" not in response.text
    listing = by_provider(response.json())
    assert set(listing) == {
        "gemini", "groq", "openrouter", "mistral", "cerebras", "ollama", "openai", "custom", "anthropic", "gmail", "telegram",
        "discord", "tavily",
    }
    assert listing["telegram"]["kind"] == listing["discord"]["kind"] == "messaging"
    assert listing["groq"]["connected"] is True and listing["groq"]["masked"] == {"api_key": "gsk...cdef"}
    assert listing["groq"]["default_model"] == "openai/gpt-oss-20b"
    assert listing["ollama"]["source"] == "server"  # needs no key
    assert listing["openrouter"]["get_key_url"] == "https://openrouter.ai/settings/keys"
    assert all(item["connected"] is False for name, item in listing.items() if name != "groq")


async def test_connect_twice_replaces_the_credential(client, user, db_session):
    for key in ("first-key-0123456789", "second-key-0123456789"):
        response = await client.post("/api/integrations/openrouter/connect", json={"api_key": key}, headers=user.headers)
        assert response.status_code == 200
    rows = list(await db_session.scalars(select(Credential).where(Credential.provider == "openrouter")))
    assert len(rows) == 1
    assert get_cipher().decrypt(rows[0].encrypted_value)["api_key"] == "second-key-0123456789"


async def test_gmail_credentials(client, user):
    response = await client.post(
        "/api/integrations/gmail/connect", json={"email": "me@gmail.com", "app_password": APP_PASSWORD},
        headers=user.headers,
    )
    assert response.status_code == 200, response.text
    assert "efgh" not in response.text
    assert response.json()["masked"] == {"email": "me@gmail.com", "app_password": "********"}


@pytest.mark.parametrize(
    ("provider", "body", "message"),
    [
        ("gemini", {}, "gemini needs an 'api_key'"),
        ("gemini", {"api_key": "   "}, "gemini needs an 'api_key'"),
        ("gmail", {"email": "me@gmail.com"}, "needs both 'email' and 'app_password'"),
        ("gmail", {"email": "me@gmail.com", "app_password": "x", "api_key": "k"}, "doesn't take api_key"),
        ("groq", {"api_key": "k", "base_url": "https://evil.example.com"}, "base_url is only for ollama, openai, and custom"),
        ("custom", {"base_url": "https://api.example.com/v1"}, "custom needs a 'base_url'"),
        ("custom", {"model": "m"}, "custom needs a 'base_url'"),
        ("tavily", {}, "tavily needs an 'api_key'"),
        ("tavily", {"api_key": "tvly-x", "model": "m"}, "doesn't take model"),
    ],
)
async def test_connect_validation(client, user, provider, body, message):
    response = await client.post(f"/api/integrations/{provider}/connect", json=body, headers=user.headers)
    assert response.status_code == 422
    assert message in response.json()["detail"]


async def test_validation_errors_never_echo_secrets(client, user):
    # Unknown field -> FastAPI validation error; the body (with the key) must not come back.
    response = await client.post(
        "/api/integrations/gemini/connect", json={"api_key": GEMINI_KEY, "surprise": GEMINI_KEY}, headers=user.headers
    )
    assert response.status_code == 422
    assert GEMINI_KEY not in response.text


async def test_ollama_connect_needs_no_key(client, user):
    response = await client.post(
        "/api/integrations/ollama/connect",
        json={"base_url": "http://host.docker.internal:11434/v1/", "model": "qwen3"},
        headers=user.headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["masked"] == {"base_url": "http://host.docker.internal:11434/v1", "model": "qwen3"}


async def test_disconnect(client, user):
    await client.post("/api/integrations/anthropic/connect", json={"api_key": "sk-ant-test-0123456789"}, headers=user.headers)
    assert (await client.delete("/api/integrations/anthropic", headers=user.headers)).status_code == 204
    assert (await client.delete("/api/integrations/anthropic", headers=user.headers)).status_code == 404
    listing = by_provider((await client.get("/api/integrations", headers=user.headers)).json())
    assert listing["anthropic"]["connected"] is False and listing["anthropic"]["status"] == "disconnected"


async def test_unknown_provider_and_auth(client, user):
    assert (await client.post("/api/integrations/cohere/connect", json={}, headers=user.headers)).status_code == 404
    assert (await client.post("/api/integrations/mock/test", headers=user.headers)).status_code == 404
    assert (await client.get("/api/integrations")).status_code == 401


async def test_credentials_are_per_user(client, user_factory):
    alice, bob = await user_factory(), await user_factory()
    await client.post("/api/integrations/groq/connect", json={"api_key": "alice-groq-0123456789"}, headers=alice.headers)
    bob_listing = by_provider((await client.get("/api/integrations", headers=bob.headers)).json())
    assert bob_listing["groq"]["connected"] is False
    assert (await client.delete("/api/integrations/groq", headers=bob.headers)).status_code == 404


async def test_test_endpoint_in_testing_mode(client, user):
    await client.post("/api/integrations/gemini/connect", json={"api_key": GEMINI_KEY}, headers=user.headers)
    response = await client.post("/api/integrations/gemini/test", headers=user.headers)
    assert response.status_code == 200
    result = response.json()
    assert result["success"] is True and result["source"] == "user"
    assert result["details"]["mock"] is True
    assert isinstance(result["latency_ms"], int)

    listing = by_provider((await client.get("/api/integrations", headers=user.headers)).json())
    assert listing["gemini"]["last_test"]["success"] is True


async def test_test_endpoint_reports_failures(client, user, monkeypatch):
    async def failing_verify(self, model=None):
        raise ProviderError("gemini", f"authentication failed (HTTP 401): API key not valid {GEMINI_KEY}")

    register_secret(GEMINI_KEY)
    monkeypatch.setattr(MockLLMProvider, "verify", failing_verify)
    await client.post("/api/integrations/gemini/connect", json={"api_key": GEMINI_KEY}, headers=user.headers)
    result = (await client.post("/api/integrations/gemini/test", headers=user.headers)).json()
    assert result["success"] is False
    assert "authentication failed (HTTP 401)" in result["error"]
    assert GEMINI_KEY not in json.dumps(result)

    listing = by_provider((await client.get("/api/integrations", headers=user.headers)).json())
    assert listing["gemini"]["status"] == "error"


async def test_test_without_any_credential_says_authentication_missing(client, user, monkeypatch):
    monkeypatch.setattr(settings, "TESTING", False)
    monkeypatch.setattr(settings, "GROQ_API_KEY", None)
    result = (await client.post("/api/integrations/groq/test", headers=user.headers)).json()
    assert result["success"] is False and result["source"] == "none"
    assert result["error"].startswith("Authentication missing for provider 'groq'")


def test_cipher_rotation_and_errors():
    from cryptography.fernet import Fernet

    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    token = CredentialCipher(old).encrypt({"api_key": "k"})
    assert CredentialCipher(f"{new},{old}").decrypt(token) == {"api_key": "k"}
    with pytest.raises(CredentialDecryptionError):
        CredentialCipher(new).decrypt(token)
    with pytest.raises(ValueError, match="not a valid Fernet key") as info:
        CredentialCipher("definitely-not-a-key")
    assert "definitely-not-a-key" not in str(info.value)


def test_mask_secret():
    assert mask_secret("sk-abcdefghijklmnop1234") == "sk-...1234"
    assert mask_secret("short") == "****"


def test_log_formatter_redacts_registered_secrets():
    register_secret("super-secret-value-123")
    record = logging.makeLogRecord({"msg": "calling with super-secret-value-123", "name": "t", "levelname": "INFO"})
    record.detail = "key=super-secret-value-123"
    line = JSONFormatter().format(record)
    assert "super-secret-value-123" not in line and line.count("[REDACTED]") == 2


# --- Mistral, Cerebras, Custom (OpenAI-compatible), Tavily -------------------------------------


async def test_new_providers_are_listed(client, user):
    listing = by_provider((await client.get("/api/integrations", headers=user.headers)).json())
    assert listing["mistral"]["kind"] == "llm" and listing["mistral"]["default_model"] == "mistral-small-latest"
    assert listing["cerebras"]["kind"] == "llm" and listing["cerebras"]["get_key_url"].startswith("https://cloud.cerebras.ai")
    assert listing["custom"]["label"] == "Custom (OpenAI-compatible)"
    assert listing["tavily"]["kind"] == "search"
    assert "github" not in listing and "duckduckgo" not in listing  # retired / needs no credential


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8080/v1",
        "http://127.0.0.1/v1",
        "http://10.0.0.5/v1",
        "http://169.254.169.254/latest",  # cloud metadata
        "http://[::1]:11434/v1",
        "http://redis:6379",  # a Docker service name (resolves to a private address)
        "file:///etc/passwd",
    ],
)
async def test_custom_endpoint_must_be_public(client, user, base_url):
    response = await client.post(
        "/api/integrations/custom/connect", json={"base_url": base_url, "model": "m"}, headers=user.headers
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    # A non-http(s) URL fails request validation; the rest fail the SSRF guard.
    assert (isinstance(detail, str) and detail.startswith("base_url: Blocked")) or "base_url" in json.dumps(detail)


async def test_custom_endpoint_private_addresses_allowed_for_local_development(client, user, monkeypatch):
    monkeypatch.setattr(settings, "HTTP_ALLOW_PRIVATE_NETWORKS", True)
    response = await client.post(
        "/api/integrations/custom/connect", json={"base_url": "http://localhost:8080/v1", "model": "m"}, headers=user.headers
    )
    assert response.status_code == 200, response.text


async def test_custom_endpoint_connects_masked_with_an_optional_key(client, user):
    keyless = await client.post(
        "/api/integrations/custom/connect",
        json={"base_url": "https://api.groq.com/openai/v1", "model": "openai/gpt-oss-20b"},
        headers=user.headers,
    )
    assert keyless.status_code == 200, keyless.text
    with_key = await client.post(
        "/api/integrations/custom/connect",
        json={"base_url": "https://api.groq.com/openai/v1", "model": "openai/gpt-oss-20b", "api_key": "gsk_custom_0123456789"},
        headers=user.headers,
    )
    body = with_key.json()
    assert "gsk_custom_0123456789" not in with_key.text
    assert body["masked"]["base_url"] == "https://api.groq.com/openai/v1" and body["masked"]["model"] == "openai/gpt-oss-20b"
    assert body["source"] == "user"


async def test_tavily_connect_and_test(client, user):
    connected = await client.post("/api/integrations/tavily/connect", json={"api_key": "tvly-dev-0123456789abcdef"}, headers=user.headers)
    assert connected.status_code == 200, connected.text
    assert "0123456789abcdef" not in connected.text
    result = (await client.post("/api/integrations/tavily/test", headers=user.headers)).json()
    assert result["success"] is True and result["source"] == "user" and result["details"] == {"mock": True}
    assert (await client.delete("/api/integrations/tavily", headers=user.headers)).status_code == 204
