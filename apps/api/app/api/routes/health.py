import logging
from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.core.config import settings
from app.core.redis import get_redis
from app.db.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


async def check_health(response: Response) -> dict[str, Any]:
    """Are the API's dependencies reachable? 200 when the database and Redis both answer, 503 otherwise,
    with each check's result. No authentication, no secrets: safe for a load balancer or an uptime monitor."""
    checks: dict[str, str] = {}
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        logger.warning("health: database check failed", extra={"error": f"{type(exc).__name__}: {exc}"})
        checks["database"] = "unavailable"
    try:
        await get_redis().ping()
        checks["redis"] = "ok"
    except Exception as exc:
        logger.warning("health: redis check failed", extra={"error": f"{type(exc).__name__}: {exc}"})
        checks["redis"] = "unavailable"
    healthy = all(value == "ok" for value in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if healthy else "unavailable",
        # `database` is kept at the top level for older monitors.
        "database": checks["database"],
        "checks": checks,
        "environment": settings.ENVIRONMENT,
        "public_demo": settings.PUBLIC_DEMO,
    }


@router.get("/health", summary="Liveness and dependency check (database and Redis)", responses={503: {"description": "A dependency is down"}})
async def health(response: Response) -> dict[str, Any]:
    return await check_health(response)
