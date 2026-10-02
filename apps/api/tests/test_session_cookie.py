"""The signed session cookie that the web app's route guard verifies (apps/web/src/proxy.ts)."""

from app.core.config import settings
from app.core.security import InvalidTokenError, decode_token

import pytest


def session_cookie(response):
    header = next(h for h in response.headers.get_list("set-cookie") if h.startswith(f"{settings.SESSION_COOKIE_NAME}="))
    return header, header.split(";")[0].split("=", 1)[1]


async def test_register_login_and_refresh_set_a_signed_httponly_session_cookie(client, user_factory):
    user = await user_factory(email="cookie@example.com", password="password123")
    login = await client.post("/api/auth/login", json={"email": user.email, "password": "password123"})
    header, value = session_cookie(login)
    assert "httponly" in header.lower() and "samesite=lax" in header.lower() and "path=/" in header.lower()
    assert f"max-age={settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400}" in header.lower()
    assert decode_token(value, expected_type="session")["sub"] == user.id
    refreshed = await client.post("/api/auth/refresh", json={"refresh_token": user.refresh_token})
    assert decode_token(session_cookie(refreshed)[1], expected_type="session")["sub"] == user.id
    registered = await client.post("/api/auth/register", json={"email": "new@example.com", "password": "password123", "full_name": "N"})
    assert settings.SESSION_COOKIE_NAME in registered.headers["set-cookie"]


async def test_the_session_cookie_is_not_an_api_credential(client, user_factory):
    user = await user_factory()
    login = await client.post("/api/auth/login", json={"email": user.email, "password": user.password})
    cookie = session_cookie(login)[1]
    assert (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {cookie}"})).status_code == 401
    with pytest.raises(InvalidTokenError):
        decode_token(cookie, expected_type="access")
    # And an access token is no session.
    with pytest.raises(InvalidTokenError):
        decode_token(user.access_token, expected_type="session")


async def test_failed_logins_set_no_cookie_and_logout_clears_it(client, user_factory):
    user = await user_factory()
    bad = await client.post("/api/auth/login", json={"email": user.email, "password": "wrong-password"})
    assert bad.status_code == 401 and "set-cookie" not in bad.headers
    out = await client.post("/api/auth/logout")
    assert out.status_code == 204
    cleared = next(h for h in out.headers.get_list("set-cookie") if h.startswith(settings.SESSION_COOKIE_NAME))
    assert 'max-age=0' in cleared.lower() or "1970" in cleared
