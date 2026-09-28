from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from flowforge_engine import ExecutionServices

from app.api.deps import CurrentUser, DbSession
from app.schemas.integration import ConnectRequest, IntegrationRead, IntegrationTestResult
from app.services.credentials import (
    CONNECTABLE,
    CredentialInputError,
    check_connection,
    delete_credential,
    list_integrations,
    normalize_credential,
    save_credential,
)
from app.services.providers import get_execution_services

router = APIRouter(prefix="/integrations", tags=["integrations"])

ProviderName = Annotated[str, Path(description=f"One of: {', '.join(CONNECTABLE)}")]

_UNKNOWN = {404: {"description": "Unknown provider"}}


def _known(provider: str) -> str:
    name = provider.lower()
    if name not in CONNECTABLE:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"Unknown provider '{provider}' (known: {', '.join(CONNECTABLE)})"
        )
    return name


Services = Annotated[ExecutionServices, Depends(get_execution_services)]


@router.get(
    "",
    response_model=list[IntegrationRead],
    summary="List providers and which credential each would use",
    description="Secrets are never returned: your stored keys appear masked (e.g. `AIz...9xQk`).",
)
async def list_all(db: DbSession, user: CurrentUser) -> list[IntegrationRead]:
    return await list_integrations(db, user)


@router.post(
    "/{provider}/connect",
    response_model=IntegrationRead,
    summary="Store (or replace) your credential for a provider",
    description=(
        "Encrypted at rest with ENCRYPTION_KEY. Your credential takes priority over the "
        "server-wide key in .env. Use `/test` afterwards to check it with a real call."
    ),
    responses={**_UNKNOWN, 422: {"description": "A required field is missing for this provider"}},
)
async def connect(provider: ProviderName, body: ConnectRequest, db: DbSession, user: CurrentUser) -> IntegrationRead:
    name = _known(provider)
    try:
        data = normalize_credential(name, body)
    except CredentialInputError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    await save_credential(db, user, name, data)
    return next(i for i in await list_integrations(db, user) if i.provider == name)


@router.delete(
    "/{provider}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove your stored credential for a provider",
    description="Runs fall back to the server-wide key, if one is configured; otherwise they fail validation.",
    responses={404: {"description": "Unknown provider, or nothing stored for it"}},
)
async def disconnect(provider: ProviderName, db: DbSession, user: CurrentUser) -> Response:
    name = _known(provider)
    if not await delete_credential(db, user, name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"No stored credential for '{name}'")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{provider}/test",
    response_model=IntegrationTestResult,
    summary="Check the credential with a real, minimal call",
    description=(
        "LLM providers: fetch the default model's metadata or the model list (no tokens "
        "generated). Gmail: log in to SMTP and IMAP without sending anything. Always 200; "
        "`success` and `error` report the outcome, `latency_ms` the round trip."
    ),
    responses=_UNKNOWN,
)
async def run_connection_test(
    provider: ProviderName, db: DbSession, user: CurrentUser, services: Services
) -> IntegrationTestResult:
    return await check_connection(db, user, _known(provider), services)
