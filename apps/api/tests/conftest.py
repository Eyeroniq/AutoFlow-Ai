"""API test harness.

Runs against a real Postgres: a `<db>_test` database on the configured server is dropped,
recreated, and migrated with Alembic once per session. Each test runs inside a
transaction that is rolled back afterwards (route commits become savepoints).
"""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from flowforge_engine.providers import MockEmailProvider
from flowforge_engine.testing import live_tests_enabled
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

# --- Environment, before any app module reads Settings ---------------------------------
os.environ["JWT_SECRET"] = "test-jwt-secret-" + "x" * 32
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
# Unit tests use mocks: with TESTING=true the provider factory hands out mock providers even
# though real keys may be configured. Live tests (-m live) switch this off per test.
os.environ["TESTING"] = "true"

from app.core.config import settings  # noqa: E402

# Point the app at <db>_test before app.db.session builds its engine.
_configured = make_url(settings.DATABASE_URL)
settings.DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or _configured.set(
    database=f"{_configured.database}_test"
).render_as_string(hide_password=False)

# ...and at Redis database 15 (broker, results, stop flags) before the Celery app is built,
# so tests never touch the dev worker's queue. Pub/sub channels are per execution id.
_redis = urlsplit(settings.REDIS_URL)
settings.REDIS_URL = os.environ.get("TEST_REDIS_URL") or urlunsplit(_redis._replace(path="/15"))
settings.CELERY_BROKER_URL = settings.CELERY_RESULT_BACKEND = None

# Fast polling; no background heartbeats or WS database checks (tests share one DB session,
# and those loops would use it concurrently). Tests that need them turn them on.
settings.EXECUTION_STOP_POLL_SECONDS = 0.02
settings.EXECUTION_HEARTBEAT_SECONDS = 3600
settings.EXECUTION_STOP_WAIT_SECONDS = 0
settings.WS_DB_CHECK_SECONDS = 3600
settings.WS_AUTH_TIMEOUT_SECONDS = 2

from app.core.rate_limit import limiter  # noqa: E402
from app.core.redis import get_redis  # noqa: E402
from app.db.session import get_db, get_session_factory  # noqa: E402
from app.main import app  # noqa: E402
from app.services.task_queue import EnqueueFailed, get_task_queue  # noqa: E402

API_ROOT = Path(__file__).resolve().parents[1]

limiter.enabled = False

HERE = Path(__file__).resolve().parent


def pytest_collection_modifyitems(config, items):
    if live_tests_enabled(config.option.markexpr):
        return
    skip = pytest.mark.skip(reason="live test: calls real APIs and sends a real email; run with `pytest -m live`")
    for item in items:
        if "live" in item.keywords and item.path.is_relative_to(HERE):
            item.add_marker(skip)


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


@dataclass
class FakeTaskQueue:
    """Records what the API would send to Celery (tests run executions themselves)."""

    enqueued: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    # Continuations of handed-off runs: (execution id, queue, segment).
    continued: list[tuple[uuid.UUID, str, int]] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)
    fail: bool = False

    async def enqueue(self, execution_id: uuid.UUID, queue: str, *, segment: int = 0) -> str:
        if self.fail:
            raise EnqueueFailed("OperationalError: Error 111 connecting to redis:6379. Connection refused.")
        if segment:
            self.continued.append((execution_id, queue, segment))
        else:
            self.enqueued.append((execution_id, queue))
        return str(execution_id) if segment == 0 else f"{execution_id}:{segment}"

    async def revoke(self, task_id: str) -> None:
        self.revoked.append(task_id)


@pytest.fixture
def task_queue() -> FakeTaskQueue:
    return FakeTaskQueue()


class LockedSession:
    """The test's session, with async operations serialized.

    In production every request/worker has its own session. Tests share one (it rolls back
    at the end), and a run executing in a background task can query while a request
    handler does -- so each awaited operation takes a lock.
    """

    _ASYNC = frozenset({"execute", "scalar", "scalars", "get", "commit", "rollback", "flush", "refresh", "delete", "merge"})

    def __init__(self, session: AsyncSession):
        self._session = session
        self._lock = asyncio.Lock()

    def __getattr__(self, name):
        attr = getattr(self._session, name)
        if name not in self._ASYNC:
            return attr

        async def locked(*args, **kwargs):
            async with self._lock:
                return await attr(*args, **kwargs)

        return locked


@pytest.fixture
def shared_session(db_session) -> LockedSession:
    return LockedSession(db_session)


@pytest.fixture
def session_factory(shared_session):
    """Opens "sessions" that are all the test's (locked) transaction-scoped session."""

    @asynccontextmanager
    async def shared():
        yield shared_session

    return shared


@pytest.fixture
def redis():
    return get_redis()


@pytest.fixture
async def client(shared_session, session_factory, task_queue):
    async def override_get_db():
        yield shared_session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_task_queue] = lambda: task_queue
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http
    app.dependency_overrides.clear()


@pytest.fixture(scope="session", autouse=True)
async def _clean_test_redis():
    yield
    await get_redis().flushdb()


@pytest.fixture(autouse=True)
def _clear_mock_outbox():
    MockEmailProvider.clear_outbox()


@pytest.fixture(autouse=True)
def files_dir(tmp_path, monkeypatch):
    """Uploads go to a per-test directory, never the real upload volume."""
    path = tmp_path / "files"
    monkeypatch.setattr(settings, "FILES_DIR", str(path))
    return path


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
