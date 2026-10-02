"""The telegram-listener service: long polling for the Telegram Command Center.

    python -m app.telegram_listener

Polls the server's bot (TELEGRAM_BOT_TOKEN) with getUpdates and hands each update to
app.services.telegram_center.CommandCenter. The offset of the next update is saved in Redis
after each batch, so a restart picks up where it left off; each update_id is also claimed
atomically before it's handled, so nothing runs twice even if a batch is redelivered or two
listeners run by mistake. Run exactly one per bot: Telegram allows one getUpdates consumer
(a webhook set on the bot is removed at start, since the two can't coexist).
"""

import asyncio
import contextlib
import logging
import signal

from flowforge_engine.errors import ProviderError
from flowforge_engine.providers.telegram_provider import TelegramProvider
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.logging import register_secret, setup_logging
from app.core.redis import new_redis
from app.services import automations
from app.services.task_queue import CeleryTaskQueue
from app.services.telegram_center import OFFSET_KEY, CommandCenter

logger = logging.getLogger("app.telegram_listener")

POLL_TIMEOUT_SECONDS = 30
# After a network or API error, wait this long (doubling up to the maximum) before polling again.
BACKOFF_SECONDS, MAX_BACKOFF_SECONDS = 2.0, 60.0


async def listen(stop: asyncio.Event) -> None:
    token = settings.TELEGRAM_BOT_TOKEN.get_secret_value().strip() if settings.TELEGRAM_BOT_TOKEN else ""
    if not token:
        logger.warning("TELEGRAM_BOT_TOKEN isn't set; the Telegram Command Center is off")
        await stop.wait()
        return
    bot = TelegramProvider(token)
    redis = new_redis()
    engine = create_async_engine(settings.DATABASE_URL, pool_size=5)
    center = CommandCenter(
        bot, session_factory=async_sessionmaker(engine, expire_on_commit=False, autoflush=False),
        redis=redis, task_queue=CeleryTaskQueue(),
    )
    backoff = BACKOFF_SECONDS
    try:
        with contextlib.suppress(ProviderError):
            await bot.delete_webhook()
        me = await bot.call("getMe")
        logger.info("telegram listener started", extra={"bot": me.get("username")})
        while not stop.is_set():
            saved = await redis.get(OFFSET_KEY)
            offset = int(saved) if saved else None
            try:
                updates = await bot.get_updates(offset, timeout=POLL_TIMEOUT_SECONDS)
            except ProviderError as exc:
                logger.warning("getUpdates failed; backing off", extra={"error": str(exc), "seconds": backoff})
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue
            backoff = BACKOFF_SECONDS
            if updates and await automations.is_paused(redis, "telegram"):
                # Paused from the UI: read the messages and drop them, so nothing is replayed on resume.
                await redis.set(OFFSET_KEY, int(updates[-1]["update_id"]) + 1)
                logger.info("telegram paused; dropped updates", extra={"count": len(updates)})
                continue
            for update in updates:
                try:
                    outcome = await center.handle_update(update)
                    logger.info("telegram update", extra={"update_id": update.get("update_id"), "outcome": outcome})
                except Exception:  # one bad update must not stop the listener
                    logger.exception("telegram update failed", extra={"update_id": update.get("update_id")})
                # Saved after each update: a crash mid-batch resumes after the last handled one.
                await redis.set(OFFSET_KEY, int(update["update_id"]) + 1)
    finally:
        await center.drain()
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    # The same redaction as the API and workers: the bot token is in every Bot API URL.
    setup_logging(settings.LOG_LEVEL)
    for secret in settings.secret_values():
        register_secret(secret)
    stop = asyncio.Event()

    async def run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await listen(stop)

    asyncio.run(run())


if __name__ == "__main__":
    main()
