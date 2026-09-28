from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

# Something that opens a session: `async with factory() as db: ...`
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


async def get_db() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


def get_session_factory() -> SessionFactory:
    """For code that outlives one query batch (WebSockets, in-request runs): open short
    sessions as needed instead of holding one connection for the whole lifetime."""
    return AsyncSessionLocal
