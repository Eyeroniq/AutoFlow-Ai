"""Every migration applies, rolls back, and reapplies cleanly, and matches the models."""

import asyncio

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.db.base import Base


def test_upgrade_downgrade_roundtrip(database_tools):
    recreate_database, alembic_config = database_tools
    url = make_url(settings.DATABASE_URL)
    url = url.set(database=f"{url.database}_migrations")
    asyncio.run(recreate_database(url))
    config = alembic_config(url)

    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    async def diff():
        engine = create_async_engine(url, poolclass=NullPool)
        async with engine.connect() as conn:
            result = await conn.run_sync(
                lambda sync_conn: compare_metadata(MigrationContext.configure(sync_conn), Base.metadata)
            )
        await engine.dispose()
        return result

    assert asyncio.run(diff()) == []
