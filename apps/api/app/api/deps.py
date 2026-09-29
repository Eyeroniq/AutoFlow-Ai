import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import InvalidTokenError, decode_token, hash_api_key, verify_api_key
from app.db.session import SessionFactory, get_db, get_session_factory
from app.models.deployment import Deployment
from app.models.user import User
from app.services.task_queue import TaskQueue, get_task_queue

# auto_error=False so a missing header yields our 401 (with WWW-Authenticate) rather
# than FastAPI's default 403.
bearer_scheme = HTTPBearer(auto_error=False, description="Paste the access_token from /api/auth/login")
# A deployment's API key, for /api/v1/deployments/...: either header works.
deployment_key_bearer = HTTPBearer(
    auto_error=False, scheme_name="DeploymentKey", description="A deployment's API key (ffk_...)"
)
deployment_key_header = APIKeyHeader(
    name="X-API-Key", auto_error=False, scheme_name="DeploymentKeyHeader", description="A deployment's API key (ffk_...)"
)
# Checked against when the deployment doesn't exist, so the response time doesn't tell.
_DUMMY_KEY_HASH = hash_api_key("ffk_timing-equalizer")

DbSession = Annotated[AsyncSession, Depends(get_db)]
SessionFactoryDep = Annotated[SessionFactory, Depends(get_session_factory)]
TaskQueueDep = Annotated[TaskQueue, Depends(get_task_queue)]


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized("Not authenticated")

    try:
        return await user_from_token(db, credentials.credentials)
    except InvalidTokenError as exc:
        raise _unauthorized(str(exc)) from None


async def user_from_token(db: AsyncSession, token: str) -> User:
    """The user an access token belongs to. Raises InvalidTokenError (message is safe to show)."""
    try:
        payload = decode_token(token, expected_type="access")
        user_id = uuid.UUID(payload["sub"])
    except (InvalidTokenError, ValueError, KeyError, TypeError):
        raise InvalidTokenError("Invalid or expired token") from None
    user = await db.get(User, user_id)
    if user is None:
        raise InvalidTokenError("User no longer exists")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_deployment_for_key(
    deployment_id: uuid.UUID,
    db: DbSession,
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(deployment_key_bearer)],
    header_key: Annotated[str | None, Depends(deployment_key_header)],
) -> Deployment:
    """The deployment in the path, if the request carries its API key. An unknown
    deployment and a wrong key get the same 401, so ids can't be probed."""
    key = header_key or (bearer.credentials if bearer is not None else None)
    if not key:
        raise _unauthorized("Missing API key: send 'Authorization: Bearer <key>' or 'X-API-Key: <key>'")
    deployment = await db.get(Deployment, deployment_id, populate_existing=True)
    valid = verify_api_key(key, deployment.api_key_hash if deployment else _DUMMY_KEY_HASH)
    if deployment is None or not valid:
        raise _unauthorized("Invalid deployment or API key")
    return deployment


DeploymentByKey = Annotated[Deployment, Depends(get_deployment_for_key)]
