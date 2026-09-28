"""Per-user provider credentials: encrypted storage, masking, and per-run provider settings.

Resolution order for every provider: the user's own stored credential, then the
server-wide default from .env. Nothing falls back to a mock.
"""

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from flowforge_engine import ExecutionServices, ProviderError
from flowforge_engine.providers import EMAIL_PROVIDERS, LLM_PROVIDERS, EmailAccount, ProviderInfo, ProviderSettings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import CredentialDecryptionError, get_cipher
from app.core.logging import redact_secrets, register_secret
from app.models.credential import Credential
from app.models.enums import IntegrationStatus
from app.models.integration import Integration
from app.models.user import User
from app.schemas.integration import ConnectRequest, IntegrationRead, IntegrationTestResult

logger = logging.getLogger(__name__)

Source = Literal["user", "server", "none"]

# Providers a user can connect ("mock" needs nothing).
CONNECTABLE: dict[str, ProviderInfo] = {
    **{name: info for name, info in LLM_PROVIDERS.items() if name != "mock"},
    "gmail": EMAIL_PROVIDERS["gmail"],
}
SECRET_FIELDS = frozenset({"api_key", "password"})
TEST_TIMEOUT_SECONDS = 45


class CredentialInputError(ValueError):
    """The connect body is missing a field this provider needs (message names the field)."""


def provider_info(provider: str) -> ProviderInfo | None:
    return CONNECTABLE.get(provider)


# --- masking ------------------------------------------------------------------------------


def mask_secret(value: str) -> str:
    """ "sk-...abcd" for API keys; short values are fully hidden."""
    value = value.strip()
    return f"{value[:3]}...{value[-4:]}" if len(value) >= 16 else "****"


def masked_view(provider: str, data: dict[str, Any]) -> dict[str, Any]:
    if provider == "gmail":
        view = {"email": data.get("username"), "app_password": "********"}
        view |= {k: v for k, v in data.items() if k not in SECRET_FIELDS and k != "username"}
        return view
    return {k: (mask_secret(v) if k in SECRET_FIELDS else v) for k, v in data.items()}


# --- normalizing input --------------------------------------------------------------------


def normalize_credential(provider: str, body: ConnectRequest) -> dict[str, Any]:
    """The JSON object to encrypt, validated for `provider`. Raises CredentialInputError."""
    info = CONNECTABLE[provider]
    fields = body.model_fields_set

    def secret(value: Any) -> str | None:
        return (value.get_secret_value().strip() or None) if value is not None else None

    if info.kind == "email":
        stray = fields & {"api_key", "base_url", "model"}
        if stray:
            raise CredentialInputError(f"{provider} doesn't take {', '.join(sorted(stray))}; send email and app_password")
        password = secret(body.app_password)
        if not body.email or not password:
            raise CredentialInputError(f"{provider} needs both 'email' and 'app_password' (a Google App Password)")
        data = {
            "username": str(body.email),
            "password": password,
            "from_name": body.from_name,
            "smtp_host": body.smtp_host,
            "smtp_port": body.smtp_port,
            "smtp_security": body.smtp_security,
            "imap_host": body.imap_host,
            "imap_port": body.imap_port,
        }
        return {k: v for k, v in data.items() if v not in (None, "")}

    stray = fields & {"email", "app_password", "from_name", "smtp_host", "smtp_port", "smtp_security", "imap_host", "imap_port"}
    if stray:
        raise CredentialInputError(f"{provider} doesn't take {', '.join(sorted(stray))}")
    api_key = secret(body.api_key)
    if info.requires_key and not api_key:
        raise CredentialInputError(f"{provider} needs an 'api_key' (get one at {info.get_key_url})")
    if body.base_url and provider not in ("ollama", "openai"):
        raise CredentialInputError(f"{provider} has a fixed endpoint; base_url is only for ollama and openai")
    data = {"api_key": api_key, "base_url": body.base_url, "model": body.model}
    return {k: v for k, v in data.items() if v not in (None, "")}


# --- storage ------------------------------------------------------------------------------


async def load_user_credentials(db: AsyncSession, user_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    """{provider: decrypted data} for the user. Undecryptable rows are skipped with a warning."""
    cipher = get_cipher()
    credentials: dict[str, dict[str, Any]] = {}
    for row in await db.scalars(select(Credential).where(Credential.user_id == user_id)):
        try:
            data = cipher.decrypt(row.encrypted_value)
        except CredentialDecryptionError:
            logger.warning(
                "stored credential can't be decrypted; ignoring it (was ENCRYPTION_KEY changed?)",
                extra={"user_id": str(user_id), "provider": row.provider},
            )
            continue
        for field in SECRET_FIELDS:
            register_secret(data.get(field))
        credentials[row.provider] = data
    return credentials


async def _integration(db: AsyncSession, user_id: uuid.UUID, provider: str) -> Integration | None:
    return await db.scalar(
        select(Integration).where(Integration.user_id == user_id, Integration.provider == provider)
    )


async def save_credential(db: AsyncSession, user: User, provider: str, data: dict[str, Any]) -> None:
    encrypted = get_cipher().encrypt(data)
    row = await db.scalar(select(Credential).where(Credential.user_id == user.id, Credential.provider == provider))
    if row is None:
        db.add(Credential(user_id=user.id, provider=provider, encrypted_value=encrypted))
    else:
        row.encrypted_value = encrypted

    integration = await _integration(db, user.id, provider)
    if integration is None:
        integration = Integration(user_id=user.id, provider=provider)
        db.add(integration)
    integration.status = IntegrationStatus.CONNECTED
    integration.connected_at = datetime.now(UTC)
    integration.metadata_json = {"masked": masked_view(provider, data)}
    await db.commit()
    logger.info("credential connected", extra={"user_id": str(user.id), "provider": provider})


async def delete_credential(db: AsyncSession, user: User, provider: str) -> bool:
    row = await db.scalar(select(Credential).where(Credential.user_id == user.id, Credential.provider == provider))
    if row is None:
        return False
    await db.delete(row)
    integration = await _integration(db, user.id, provider)
    if integration is not None:
        integration.status = IntegrationStatus.DISCONNECTED
        integration.connected_at = None
        integration.metadata_json = None
    await db.commit()
    logger.info("credential disconnected", extra={"user_id": str(user.id), "provider": provider})
    return True


# --- effective provider settings ----------------------------------------------------------


def server_provider_settings() -> ProviderSettings:
    return ProviderSettings.from_mapping(settings.provider_env())


def provider_settings_for(user_credentials: dict[str, dict[str, Any]]) -> ProviderSettings:
    """Server defaults with the user's own credentials layered on top (user wins)."""
    merged = server_provider_settings()
    for provider, data in user_credentials.items():
        if provider == "gmail":
            # A user's mailbox replaces the server's entirely (host overrides included).
            merged = merged.model_copy(update={"gmail": EmailAccount.model_validate(data)})
        elif provider in LLM_PROVIDERS and provider != "mock":
            merged = merged.with_account(provider, **data)
    return merged


async def build_execution_services(db: AsyncSession, user: User) -> ExecutionServices:
    credentials = await load_user_credentials(db, user.id)
    return ExecutionServices(provider_settings=provider_settings_for(credentials))


def credential_source(
    provider: str, user_credentials: dict[str, dict[str, Any]], server: ProviderSettings | None = None
) -> Source:
    if provider in user_credentials:
        return "user"
    # Ignore TESTING here: the question is whether real credentials exist.
    server = (server or server_provider_settings()).model_copy(update={"testing": False})
    return "server" if server.has_credentials(provider) else "none"


# --- listing and testing ------------------------------------------------------------------


async def list_integrations(db: AsyncSession, user: User) -> list[IntegrationRead]:
    credentials = await load_user_credentials(db, user.id)
    rows = {
        row.provider: row
        for row in await db.scalars(select(Integration).where(Integration.user_id == user.id))
    }
    server = server_provider_settings()
    result = []
    for name, info in CONNECTABLE.items():
        row = rows.get(name)
        connected = name in credentials
        metadata = (row.metadata_json or {}) if row else {}
        result.append(IntegrationRead(
            provider=name,
            label=info.label,
            kind=info.kind,
            connected=connected,
            source=credential_source(name, credentials, server),
            status=row.status if row and connected else IntegrationStatus.DISCONNECTED,
            masked=masked_view(name, credentials[name]) if connected else None,
            connected_at=row.connected_at if row and connected else None,
            last_test=metadata.get("last_test") if connected else None,
            default_model=server.default_model(name) if info.kind == "llm" else None,
            get_key_url=info.get_key_url,
        ))
    return result


async def _verify(services: ExecutionServices, provider: str) -> dict[str, Any]:
    if CONNECTABLE[provider].kind == "llm":
        return await services.llm(provider).verify(services.default_model(provider))
    details: dict[str, Any] = {"smtp": await services.email(provider).verify()}
    details["imap"] = await services.mailbox(provider).verify()
    return details


async def check_connection(db: AsyncSession, user: User, provider: str, services: ExecutionServices) -> IntegrationTestResult:
    """A real, minimal call with the credential a run would use (list/get models; SMTP+IMAP login)."""
    credentials = await load_user_credentials(db, user.id)
    source = credential_source(provider, credentials)
    start = time.perf_counter()
    details: dict[str, Any] = {}
    error: str | None = None
    try:
        details = await asyncio.wait_for(_verify(services, provider), timeout=TEST_TIMEOUT_SECONDS)
    except ProviderError as exc:
        error = str(exc)
    except TimeoutError:
        error = f"{provider}: no answer within {TEST_TIMEOUT_SECONDS}s"
    latency_ms = round((time.perf_counter() - start) * 1000)
    result = IntegrationTestResult(
        provider=provider,
        success=error is None,
        source=source,
        latency_ms=latency_ms,
        error=redact_secrets(error) if error else None,
        details=details,
        tested_at=datetime.now(UTC),
    )

    integration = await _integration(db, user.id, provider)
    if integration is not None and provider in credentials:
        integration.status = IntegrationStatus.CONNECTED if result.success else IntegrationStatus.ERROR
        last_test = result.model_dump(mode="json", include={"success", "latency_ms", "error", "tested_at"})
        integration.metadata_json = {**(integration.metadata_json or {}), "last_test": last_test}
        await db.commit()

    logger.info(
        "integration tested",
        extra={"user_id": str(user.id), "provider": provider, "source": source,
               "success": result.success, "latency_ms": latency_ms},
    )
    return result
