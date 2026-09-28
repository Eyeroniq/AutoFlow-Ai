"""API test harness.

Runs against a real Postgres: a `<db>_test` database on the configured server is dropped,
recreated, and migrated with Alembic once per session. Each test runs inside a
transaction that is rolled back afterwards (route commits become savepoints).
"""

import asyncio
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from flowforge_engine.providers import MockEmailProvider
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

# --- Environment, before any app module reads Settings ---------------------------------
os.environ["JWT_SECRET"] = "test-jwt-secret-" + "x" * 32
# Environment beats .env: blank keys force every LLM provider into mock mode.
for _key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ[_key] = ""

from app.core.config import settings  # noqa: E402

# Point the app at <db>_test before app.db.session builds its engine.
_configured = make_url(settings.DATABASE_URL)
settings.DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or _configured.set(
    database=f"{_configured.database}_test"
).render_as_string(hide_password=False)

from app.core.rate_limit import limiter  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app  # noqa: E402

API_ROOT = Path(__file__).resolve().parents[1]

limiter.enabled = False


async def recreate_database(url: URL) -> None:
    admin = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with admin.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        await conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    await admin.dispose()


def alembic_config(url: URL) -> Config:
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%"))
    return config


@pytest.fixture(scope="session")
def database_tools():
    """(recreate_database, alembic_config) for tests that manage their own scratch database."""
    return recreate_database, alembic_config


@pytest.fixture(scope="session")
def migrated_database() -> URL:
    url = make_url(settings.DATABASE_URL)
    asyncio.run(recreate_database(url))
    command.upgrade(alembic_config(url), "head")
    return url


@pytest.fixture(scope="session")
async def db_engine(migrated_database):
    engine = create_async_engine(migrated_database, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    async with db_engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


@pytest.fixture
async def client(db_session):
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _clear_mock_outbox():
    MockEmailProvider.clear_outbox()


@dataclass
class AuthedUser:
    id: str
    email: str
    password: str
    access_token: str
    refresh_token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


@pytest.fixture
def user_factory(client):
    async def create(email: str | None = None, password: str = "password123", full_name: str = "Test User") -> AuthedUser:
        email = email or f"user-{uuid.uuid4().hex[:10]}@example.com"
        response = await client.post(
            "/api/auth/register", json={"email": email, "password": password, "full_name": full_name}
        )
        assert response.status_code == 201, response.text
        tokens = response.json()
        me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
        return AuthedUser(me.json()["id"], email, password, tokens["access_token"], tokens["refresh_token"])

    return create


@pytest.fixture
async def user(user_factory) -> AuthedUser:
    return await user_factory()
