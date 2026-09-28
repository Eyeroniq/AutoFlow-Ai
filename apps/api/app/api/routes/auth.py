import logging
import uuid

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from app.api.deps import CurrentUser, DbSession
from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.security import (
    InvalidTokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.user import User
from app.schemas.auth import LoginRequest, RefreshRequest, RegisterRequest, TokenResponse
from app.schemas.user import UserRead

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# Verified against when the email is unknown so login timing doesn't reveal which
# accounts exist.
_DUMMY_HASH = hash_password("timing-equalizer")


def _issue_tokens(user: User) -> TokenResponse:
    subject = str(user.id)
    return TokenResponse(
        access_token=create_access_token(subject),
        refresh_token=create_refresh_token(subject),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
    responses={409: {"description": "Email already registered"}, 429: {"description": "Rate limited"}},
)
@limiter.limit(settings.AUTH_RATE_LIMIT)
async def register(request: Request, body: RegisterRequest, db: DbSession) -> TokenResponse:
    existing = await db.scalar(select(User.id).where(User.email == body.email))
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Email already registered")

    # bcrypt is CPU-bound (~100ms+); keep it off the event loop.
    user = User(
        email=body.email,
        hashed_password=await run_in_threadpool(hash_password, body.password),
        full_name=body.full_name,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # Lost a race with a concurrent registration for the same email.
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Email already registered") from None

    logger.info("user registered", extra={"user_id": str(user.id)})
    return _issue_tokens(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Log in with email and password",
    responses={401: {"description": "Invalid credentials"}, 429: {"description": "Rate limited"}},
)
@limiter.limit(settings.AUTH_RATE_LIMIT)
async def login(request: Request, body: LoginRequest, db: DbSession) -> TokenResponse:
    user = await db.scalar(select(User).where(User.email == body.email))
    password_ok = await run_in_threadpool(
        verify_password, body.password, user.hashed_password if user else _DUMMY_HASH
    )

    if user is None or not password_ok:
        logger.info("login failed", extra={"reason": "invalid_credentials"})
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    logger.info("user logged in", extra={"user_id": str(user.id)})
    return _issue_tokens(user)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Exchange a refresh token for a new token pair",
    description=(
        "Returns a new access token and a rotated refresh token. Tokens are stateless, "
        "so a previously issued refresh token stays valid until it expires."
    ),
    responses={401: {"description": "Invalid, expired, or non-refresh token"}, 429: {"description": "Rate limited"}},
)
@limiter.limit(settings.AUTH_RATE_LIMIT)
async def refresh(request: Request, body: RefreshRequest, db: DbSession) -> TokenResponse:
    try:
        payload = decode_token(body.refresh_token, expected_type="refresh")
        user_id = uuid.UUID(payload["sub"])
    except (InvalidTokenError, ValueError):
        raise _invalid_refresh() from None

    user = await db.get(User, user_id)
    if user is None:
        raise _invalid_refresh()

    logger.info("token refreshed", extra={"user_id": str(user.id)})
    return _issue_tokens(user)


def _invalid_refresh() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired refresh token",
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.get(
    "/me",
    response_model=UserRead,
    summary="Get the authenticated user",
    responses={401: {"description": "Missing, invalid, or expired token"}},
)
async def me(current_user: CurrentUser) -> User:
    return current_user
