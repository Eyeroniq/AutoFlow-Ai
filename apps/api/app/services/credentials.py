"""Per-user provider credentials: encrypted storage, masking, and per-run provider settings.

Resolution order for every provider: the user's own stored credential, then the
server-wide default from .env. Nothing falls back to a mock.
"""

import asyncio
import logging
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from flowforge_engine import ExecutionServices, NodeStateStore, ProviderError
from flowforge_engine.providers import (
    EMAIL_PROVIDERS,
    LLM_PROVIDERS,
    MESSAGING_PROVIDERS,
    SEARCH_PROVIDERS,
    WORKSPACE_PROVIDERS,
    DiscordAccount,
    EmailAccount,
    ProviderInfo,
    ProviderSettings,
    SearchAccount,
    TelegramAccount,
)
from flowforge_engine.netguard import BlockedDestination, check_url
from flowforge_engine.providers.discord_provider import parse_webhook_url
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
from app.services import demo
from app.services.files import DbFileStore
from app.services.knowledge import DbKnowledgeStore

logger = logging.getLogger(__name__)

Source = Literal["user", "server", "none"]

# Providers a user can connect ("mock" needs nothing).
CONNECTABLE: dict[str, ProviderInfo] = {
    **{name: info for name, info in LLM_PROVIDERS.items() if name != "mock"},
    "gmail": EMAIL_PROVIDERS["gmail"],
    **MESSAGING_PROVIDERS,
    # DuckDuckGo needs no credential.
    "tavily": SEARCH_PROVIDERS["tavily"],
    # Notion and Airtable: a token each.
    **WORKSPACE_PROVIDERS,
}
# Providers whose base URL the user may set: a local Ollama, OpenAI (or a proxy), and any
# OpenAI-compatible endpoint.
CUSTOM_ENDPOINTS = frozenset({"ollama", "openai", "custom"})
SECRET_FIELDS = frozenset({"api_key", "password", "bot_token", "webhook_url"})
_BOT_TOKEN = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
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


def mask_webhook(url: str) -> str:
    """The webhook id stays readable; the token part is hidden."""
    try:
        webhook_id, _ = parse_webhook_url(url)
    except ValueError:
        return "****"
    return f"https://discord.com/api/webhooks/{webhook_id}/****"


def masked_view(provider: str, data: dict[str, Any]) -> dict[str, Any]:
    if provider == "discord":
        return {"webhook_url": mask_webhook(str(data.get("webhook_url", "")))}
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
        stray = fields & {"api_key", "base_url", "model", "bot_token", "chat_id", "webhook_url"}
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

    if info.kind == "messaging":
        return _messaging_credential(provider, body, fields)

    if info.kind in ("search", "workspace"):
        stray = fields - {"api_key"}
        if stray:
            raise CredentialInputError(f"{provider} doesn't take {', '.join(sorted(stray))}; send api_key")
        api_key = secret(body.api_key)
        if not api_key:
            what = "an 'api_key' (its token)" if info.kind == "workspace" else "an 'api_key'"
            raise CredentialInputError(f"{provider} needs {what} (get one at {info.get_key_url})")
        return {"api_key": api_key}

    stray = fields & {
        "email", "app_password", "from_name", "smtp_host", "smtp_port", "smtp_security", "imap_host", "imap_port",
        "bot_token", "chat_id", "webhook_url",
    }
    if stray:
        raise CredentialInputError(f"{provider} doesn't take {', '.join(sorted(stray))}")
    api_key = secret(body.api_key)
    if info.requires_key and not api_key:
        raise CredentialInputError(f"{provider} needs an 'api_key' (get one at {info.get_key_url})")
    if body.base_url and provider not in CUSTOM_ENDPOINTS:
        raise CredentialInputError(f"{provider} has a fixed endpoint; base_url is only for ollama, openai, and custom")
    if provider == "custom":
        if not body.base_url or not (body.model or "").strip():
            raise CredentialInputError("custom needs a 'base_url' (e.g. https://host/v1) and the 'model' to use")
        if not settings.HTTP_ALLOW_PRIVATE_NETWORKS:
            # The same SSRF guard as the HTTP Request node: the endpoint must be public.
            try:
                check_url(body.base_url)
            except BlockedDestination as exc:
                raise CredentialInputError(f"base_url: {exc}") from None
    data = {"api_key": api_key, "base_url": body.base_url, "model": body.model}
    return {k: v for k, v in data.items() if v not in (None, "")}


def _messaging_credential(provider: str, body: ConnectRequest, fields: set[str]) -> dict[str, Any]:
    allowed = {"bot_token", "chat_id"} if provider == "telegram" else {"webhook_url"}
    stray = fields - allowed
    if stray:
        raise CredentialInputError(f"{provider} doesn't take {', '.join(sorted(stray))}; send {' and '.join(sorted(allowed))}")
    if provider == "telegram":
        token = body.bot_token.get_secret_value().strip() if body.bot_token else ""
        if not token:
            raise CredentialInputError("telegram needs a 'bot_token' (from @BotFather)")
        if not _BOT_TOKEN.match(token):
            raise CredentialInputError("that doesn't look like a Telegram bot token (it reads like 123456789:AAE...)")
        chat_id = (body.chat_id or "").strip()
        return {"bot_token": token, **({"chat_id": chat_id} if chat_id else {})}
    url = body.webhook_url.get_secret_value().strip() if body.webhook_url else ""
    if not url:
        raise CredentialInputError("discord needs a 'webhook_url' (channel settings > Integrations > Webhooks)")
    try:
        parse_webhook_url(url)
    except ValueError as exc:
        raise CredentialInputError(f"webhook_url is {exc}") from None
    return {"webhook_url": url}


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


def server_provider_settings(*, visitor: bool = False) -> ProviderSettings:
    """The server's .env settings. For a PUBLIC_DEMO visitor the personal accounts in them (SMTP, Telegram,
    Discord, Notion, Airtable: the owner's inbox, bot, server, workspaces) are removed; the AI keys stay."""
    env = settings.provider_env()
    if visitor:
        env = {k: ("" if k in demo.PERSONAL_ENV_KEYS else v) for k, v in env.items()}
    return ProviderSettings.from_mapping(env)


def provider_settings_for(user_credentials: dict[str, dict[str, Any]], *, visitor: bool = False) -> ProviderSettings:
    """Server defaults with the user's own credentials layered on top (user wins).

    A PUBLIC_DEMO `visitor` gets the server's AI keys only (whatever AI keys they stored are ignored: no
    per-visitor choice), and may use Gmail, Discord, Telegram, Notion, and Airtable only through their
    own credentials."""
    merged = server_provider_settings(visitor=visitor)
    for provider, data in user_credentials.items():
        if visitor and provider not in demo.PERSONAL_PROVIDERS:
            continue
        if provider == "gmail":
            # A user's mailbox replaces the server's entirely (host overrides included).
            merged = merged.model_copy(update={"gmail": EmailAccount.model_validate(data)})
        elif provider == "telegram":
            # A user's bot comes with its own default chat, never the server's.
            merged = merged.model_copy(update={"telegram": TelegramAccount.model_validate(data)})
        elif provider == "discord":
            merged = merged.model_copy(update={"discord": DiscordAccount.model_validate(data)})
        elif provider in ("tavily", *WORKSPACE_PROVIDERS):
            merged = merged.model_copy(update={provider: SearchAccount.model_validate(data)})
        elif provider == "custom":
            # A user's endpoint replaces the server's entirely (a key for one server is no use on another).
            merged = merged.model_copy(update={"custom": type(merged.custom).model_validate(data)})
        elif provider in LLM_PROVIDERS and provider != "mock":
            merged = merged.with_account(provider, **data)
    return merged


async def build_execution_services(
    db: AsyncSession, user: User, *, state: NodeStateStore | None = None
) -> ExecutionServices:
    """Provider credentials, the user's uploaded files and knowledge bases, node state (`state`; in memory when
    omitted), and the SSRF policy for a run."""
    credentials = await load_user_credentials(db, user.id)
    visitor = demo.is_visitor(user)
    options: dict[str, Any] = {
        "provider_settings": provider_settings_for(credentials, visitor=visitor),
        "files": DbFileStore(db, user.id),
        "knowledge": DbKnowledgeStore(db, user.id),
        "allow_private_network": settings.HTTP_ALLOW_PRIVATE_NETWORKS,
        "state": state,
    }
    # A visitor's AI calls count against their daily token budget.
    return demo.MeteredServices(user.id, **options) if visitor else ExecutionServices(**options)


def credential_source(
    provider: str, user_credentials: dict[str, dict[str, Any]], server: ProviderSettings | None = None, *, visitor: bool = False
) -> Source:
    if provider in user_credentials and not (visitor and provider not in demo.PERSONAL_PROVIDERS):
        return "user"
    # Ignore TESTING here: the question is whether real credentials exist.
    server = (server or server_provider_settings(visitor=visitor)).model_copy(update={"testing": False})
    return "server" if server.has_credentials(provider) else "none"


# --- listing and testing ------------------------------------------------------------------


async def list_integrations(db: AsyncSession, user: User) -> list[IntegrationRead]:
    credentials = await load_user_credentials(db, user.id)
    rows = {
        row.provider: row
        for row in await db.scalars(select(Integration).where(Integration.user_id == user.id))
    }
    visitor = demo.is_visitor(user)
    server = server_provider_settings(visitor=visitor)
    result = []
    for name, info in CONNECTABLE.items():
        if visitor and name not in demo.PERSONAL_PROVIDERS:
            continue  # the demo provides the AI; a visitor has nothing to connect or choose
        row = rows.get(name)
        connected = name in credentials
        metadata = (row.metadata_json or {}) if row else {}
        result.append(IntegrationRead(
            provider=name,
            label=info.label,
            kind=info.kind,
            connected=connected,
            source=credential_source(name, credentials, server, visitor=visitor),
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
    if provider == "telegram":
        # getMe (the token works) and, with a default chat, getChat (the bot can reach it).
        return await services.telegram("telegram").verify(services.telegram_default_chat())
    if provider == "discord":
        return await services.discord("discord").verify()
    if provider == "tavily":
        # GET /usage: proves the key and shows the credits left, without a search.
        return await services.search("tavily").verify()
    if provider in WORKSPACE_PROVIDERS:
        # Notion: GET /users/me (the integration's bot). Airtable: GET /meta/whoami. Nothing changes.
        return await services.workspace(provider).verify()
    details: dict[str, Any] = {"smtp": await services.email(provider).verify()}
    details["imap"] = await services.mailbox(provider).verify()
    return details


async def check_connection(db: AsyncSession, user: User, provider: str, services: ExecutionServices) -> IntegrationTestResult:
    """A real, minimal call with the credential a run would use (list/get models; SMTP+IMAP login)."""
    credentials = await load_user_credentials(db, user.id)
    source = credential_source(provider, credentials, visitor=demo.is_visitor(user))
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
