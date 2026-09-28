from fastapi import APIRouter

from app.api.routes import auth, executions, health, workflows

api_router = APIRouter(prefix="/api")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(workflows.router)
api_router.include_router(executions.router)
