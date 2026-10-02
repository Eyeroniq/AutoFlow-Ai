import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings

TokenType = Literal["access", "refresh", "session"]

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class InvalidTokenError(Exception):
    """Raised when a JWT is malformed, expired, or of the wrong type."""


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def _create_token(subject: str, token_type: TokenType, expires_delta: timedelta) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_access_token(subject: str) -> str:
    return _create_token(
        subject, "access", timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )


def create_session_token(subject: str) -> str:
    """A signed, httpOnly-cookie token that says "this browser signed in" for as long as a refresh token
    lives. The web app's route guard verifies its signature (same secret) before rendering a protected
    page; the API itself never accepts it as authentication."""
    return _create_token(subject, "session", timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS))


def create_refresh_token(subject: str) -> str:
    return _create_token(
        subject, "refresh", timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    )


API_KEY_PREFIX = "ffk_"
# "ffk_" plus the first 8 random characters: enough to tell keys apart, useless to an attacker.
API_KEY_DISPLAY_CHARS = len(API_KEY_PREFIX) + 8


def generate_api_key() -> str:
    """A new deployment API key: "ffk_" + 256 random bits (URL-safe base64)."""
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    """SHA-256 hex digest. A slow hash (bcrypt) protects low-entropy passwords; a random
    256-bit key can't be guessed from its hash, and checking it on every call stays cheap."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def verify_api_key(key: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_api_key(key), expected_hash)


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """Verify signature and expiry, and ensure the token is of the expected type."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as exc:
        raise InvalidTokenError(str(exc)) from exc

    if payload.get("type") != expected_type:
        raise InvalidTokenError(f"Expected a {expected_type} token")
    if not payload.get("sub"):
        raise InvalidTokenError("Token has no subject")
    return payload
