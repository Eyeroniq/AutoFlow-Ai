from fastapi import APIRouter

from app.api.routes import auth, executions, files, health, integrations, nodes, workflows

api_router = APIRouter(prefix="/api")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(workflows.router)
api_router.include_router(executions.router)
api_router.include_router(integrations.router)
api_router.include_router(nodes.router)
api_router.include_router(files.router)
