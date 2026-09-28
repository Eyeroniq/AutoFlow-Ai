import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import InvalidTokenError, decode_token
from app.db.session import SessionFactory, get_db, get_session_factory
from app.models.user import User
from app.services.task_queue import TaskQueue, get_task_queue

# auto_error=False so a missing header yields our 401 (with WWW-Authenticate) rather
# than FastAPI's default 403.
bearer_scheme = HTTPBearer(auto_error=False, description="Paste the access_token from /api/auth/login")

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
