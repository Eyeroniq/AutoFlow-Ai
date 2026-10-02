import logging
import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status
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
    create_session_token,
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


def _set_session_cookie(response: Response, user: User) -> None:
    response.set_cookie(
        settings.SESSION_COOKIE_NAME, create_session_token(str(user.id)),
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400, httponly=True, samesite="lax",
        secure=settings.COOKIE_SECURE, path="/",
    )


def _issue_tokens(user: User, response: Response) -> TokenResponse:
    subject = str(user.id)
    _set_session_cookie(response, user)
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
@limiter.limit(lambda: settings.AUTH_RATE_LIMIT)
async def register(request: Request, response: Response, body: RegisterRequest, db: DbSession) -> TokenResponse:
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
    return _issue_tokens(user, response)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Log in with email and password",
    responses={401: {"description": "Invalid credentials"}, 429: {"description": "Rate limited"}},
)
@limiter.limit(lambda: settings.AUTH_RATE_LIMIT)
async def login(request: Request, response: Response, body: LoginRequest, db: DbSession) -> TokenResponse:
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
    return _issue_tokens(user, response)


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
@limiter.limit(lambda: settings.AUTH_RATE_LIMIT)
async def refresh(request: Request, response: Response, body: RefreshRequest, db: DbSession) -> TokenResponse:
    try:
        payload = decode_token(body.refresh_token, expected_type="refresh")
        user_id = uuid.UUID(payload["sub"])
    except (InvalidTokenError, ValueError):
        raise _invalid_refresh() from None

    user = await db.get(User, user_id)
    if user is None:
        raise _invalid_refresh()

    logger.info("token refreshed", extra={"user_id": str(user.id)})
    return _issue_tokens(user, response)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear the session cookie",
    description="Tokens are stateless; this only removes the signed session cookie the web app's route guard reads.",
)
async def logout() -> Response:
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(settings.SESSION_COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=settings.COOKIE_SECURE)
    return response


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
