import uuid
from datetime import UTC, datetime, timedelta

import pytest
from jose import jwt

from app.core.config import settings


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_token(sub: str, token_type: str, *, expires_in: timedelta = timedelta(minutes=5), secret: str | None = None) -> str:
    now = datetime.now(UTC)
    payload = {"sub": sub, "type": token_type, "iat": now, "exp": now + expires_in, "jti": uuid.uuid4().hex}
    return jwt.encode(payload, secret or settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def claims(token: str) -> dict:
    return jwt.get_unverified_claims(token)


class TestRegisterLoginMe:
    async def test_register_then_login_then_me(self, client):
        register = await client.post(
            "/api/auth/register",
            json={"email": "Ada@Example.com", "password": "password123", "full_name": " Ada Lovelace "},
        )
        assert register.status_code == 201
        assert claims(register.json()["access_token"])["type"] == "access"
        assert claims(register.json()["refresh_token"])["type"] == "refresh"

        login = await client.post("/api/auth/login", json={"email": "ada@example.com", "password": "password123"})
        assert login.status_code == 200

        me = await client.get("/api/auth/me", headers=bearer(login.json()["access_token"]))
        assert me.status_code == 200
        assert me.json()["email"] == "ada@example.com"
        assert me.json()["full_name"] == "Ada Lovelace"
        assert "hashed_password" not in me.json()

    async def test_duplicate_email(self, client, user):
        response = await client.post(
            "/api/auth/register", json={"email": user.email.upper(), "password": "password123", "full_name": "X"}
        )
        assert response.status_code == 409

    async def test_wrong_password(self, client, user):
        response = await client.post("/api/auth/login", json={"email": user.email, "password": "wrong-password"})
        assert response.status_code == 401

    @pytest.mark.parametrize(
        "headers",
        [{}, bearer("garbage"), {"Authorization": "Basic abc"}],
        ids=["missing", "garbage", "wrong-scheme"],
    )
    async def test_me_rejects_bad_credentials(self, client, headers):
        response = await client.get("/api/auth/me", headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"

    async def test_me_rejects_refresh_token(self, client, user):
        assert (await client.get("/api/auth/me", headers=bearer(user.refresh_token))).status_code == 401


class TestRefresh:
    async def test_valid_refresh_returns_working_tokens(self, client, user):
        response = await client.post("/api/auth/refresh", json={"refresh_token": user.refresh_token})

        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
        assert claims(body["access_token"])["type"] == "access"
        assert claims(body["access_token"])["sub"] == user.id
        # Rotated: a fresh refresh token (new jti and expiry), not the one we sent.
        assert body["refresh_token"] != user.refresh_token
        assert claims(body["refresh_token"])["type"] == "refresh"

        me = await client.get("/api/auth/me", headers=bearer(body["access_token"]))
        assert me.status_code == 200 and me.json()["email"] == user.email

        # The rotated refresh token works in turn.
        again = await client.post("/api/auth/refresh", json={"refresh_token": body["refresh_token"]})
        assert again.status_code == 200

    async def test_expired_refresh_token_is_rejected(self, client, user):
        expired = make_token(user.id, "refresh", expires_in=timedelta(seconds=-1))
        response = await client.post("/api/auth/refresh", json={"refresh_token": expired})
        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid or expired refresh token"

    @pytest.mark.parametrize("token", ["not-a-jwt", "a.b.c", "eyJhbGciOiJIUzI1NiJ9.e30."])
    async def test_malformed_refresh_token_is_rejected(self, client, token):
        response = await client.post("/api/auth/refresh", json={"refresh_token": token})
        assert response.status_code == 401

    async def test_forged_refresh_token_is_rejected(self, client, user):
        forged = make_token(user.id, "refresh", secret="attacker-secret-" + "y" * 32)
        assert (await client.post("/api/auth/refresh", json={"refresh_token": forged})).status_code == 401

    async def test_tampered_refresh_token_is_rejected(self, client, user, user_factory):
        other = await user_factory()
        header, _, signature = user.refresh_token.split(".")
        other_payload = other.refresh_token.split(".")[1]
        tampered = f"{header}.{other_payload}.{signature}"
        assert (await client.post("/api/auth/refresh", json={"refresh_token": tampered})).status_code == 401

    async def test_access_token_used_as_refresh_token_is_rejected(self, client, user):
        response = await client.post("/api/auth/refresh", json={"refresh_token": user.access_token})
        assert response.status_code == 401

    async def test_refresh_token_for_deleted_user_is_rejected(self, client):
        orphan = make_token(str(uuid.uuid4()), "refresh")
        assert (await client.post("/api/auth/refresh", json={"refresh_token": orphan})).status_code == 401

    async def test_refresh_token_with_non_uuid_subject_is_rejected(self, client):
        weird = make_token("not-a-uuid", "refresh")
        assert (await client.post("/api/auth/refresh", json={"refresh_token": weird})).status_code == 401

    @pytest.mark.parametrize("body", [{}, {"refresh_token": ""}])
    async def test_missing_refresh_token(self, client, body):
        assert (await client.post("/api/auth/refresh", json=body)).status_code == 422
