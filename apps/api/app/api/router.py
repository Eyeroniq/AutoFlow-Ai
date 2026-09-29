from fastapi import APIRouter

from app.api.routes import (
    auth,
    deployment_runs,
    deployments,
    executions,
    files,
    health,
    integrations,
    nodes,
    templates,
    triggers,
    workflows,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(workflows.router)
api_router.include_router(triggers.router)
api_router.include_router(templates.router)
api_router.include_router(executions.router)
api_router.include_router(integrations.router)
api_router.include_router(nodes.router)
api_router.include_router(files.router)
api_router.include_router(deployments.router)
# /api/v1/deployments/{id}/run: authenticated by the deployment's API key.
api_router.include_router(deployment_runs.router)
