"""Create the demo user. Idempotent — safe to run repeatedly.

    python -m app.db.seed
"""

import asyncio
import logging

from sqlalchemy import select

from app.core.config import settings
from app.core.logging import setup_logging
from app.core.security import hash_password
from app.db.session import AsyncSessionLocal, engine
from app.models.user import User

DEMO_EMAIL = "demo@flowforge.ai"
DEMO_PASSWORD = "demo1234"
DEMO_FULL_NAME = "Demo User"

logger = logging.getLogger("app.seed")


async def seed() -> None:
    async with AsyncSessionLocal() as session:
        existing = await session.scalar(select(User).where(User.email == DEMO_EMAIL))
        if existing is not None:
            logger.info("demo user already exists", extra={"email": DEMO_EMAIL})
            return

        session.add(
            User(
                email=DEMO_EMAIL,
                hashed_password=hash_password(DEMO_PASSWORD),
                full_name=DEMO_FULL_NAME,
            )
        )
        await session.commit()
        logger.info("demo user created", extra={"email": DEMO_EMAIL})


async def main() -> None:
    try:
        await seed()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    setup_logging(settings.LOG_LEVEL)
    asyncio.run(main())
