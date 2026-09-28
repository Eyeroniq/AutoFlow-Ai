"""Per-request provider access: the user's stored credentials over the server .env defaults."""

from flowforge_engine import ExecutionServices

from app.api.deps import CurrentUser, DbSession
from app.services.credentials import build_execution_services


async def get_execution_services(db: DbSession, user: CurrentUser) -> ExecutionServices:
    """FastAPI dependency. Built per request so each run sees the caller's own keys."""
    return await build_execution_services(db, user)
