import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from app.api.router import api_router
from app.core.config import settings
from app.core.crypto import get_cipher
from app.core.logging import register_secret, setup_logging
from app.core.rate_limit import limiter
from app.db.session import engine

setup_logging(settings.LOG_LEVEL)
for _secret in settings.secret_values():
    register_secret(_secret)
# Fail at startup (not on the first connect) if ENCRYPTION_KEY isn't a valid Fernet key.
get_cipher()
logger = logging.getLogger("app")
request_logger = logging.getLogger("app.request")

_QUIET_PATHS = frozenset({"/api/health"})


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logger.info("api starting", extra={"environment": settings.ENVIRONMENT})
    yield
    await engine.dispose()
    logger.info("api stopped")


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="0.1.0",
    description="FlowForge AI backend: auth, workflows, real LLM/email providers, and encrypted integrations.",
    lifespan=lifespan,
)

app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(_: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": f"Too many requests: limit is {exc.detail}. Try again shortly."},
    )


# Request bodies on these paths carry API keys and passwords.
_SECRET_BODY_PREFIXES = ("/api/integrations",)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    if request.url.path.startswith(_SECRET_BODY_PREFIXES):
        # FastAPI echoes the offending input by default; never send secrets back.
        errors = [{k: v for k, v in error.items() if k not in ("input", "ctx")} for error in errors]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


@app.middleware("http")
async def log_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        request_logger.exception(
            "request failed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
            },
        )
        raise

    response.headers["X-Request-ID"] = request_id
    # Container healthchecks hit /api/health every few seconds; keep them out of INFO.
    level = logging.DEBUG if request.url.path in _QUIET_PATHS else logging.INFO
    request_logger.log(
        level,
        "request completed",
        extra={
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "query": request.url.query or None,
            "status_code": response.status_code,
            "duration_ms": round((time.perf_counter() - start) * 1000, 2),
            "client_ip": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent"),
        },
    )
    return response


# Added last so it is the outermost middleware and decorates every response.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)

app.include_router(api_router)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {"name": settings.PROJECT_NAME, "docs": "/docs"}
