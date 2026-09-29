"""Managing deployments (JWT, owner only). Calling one is in deployment_runs.py."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from flowforge_engine import ExecutionServices
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.deployment import Deployment
from app.schemas.deployment import DeploymentCreate, DeploymentRead, DeploymentWithKey
from app.services.deployments import deploy_workflow, deployment_read, rotate_api_key, undeploy
from app.services.providers import get_execution_services
from app.services.runs import InvalidWorkflowGraph
from app.services.workflows import get_owned_workflow

router = APIRouter(prefix="/deployments", tags=["deployments"])

Services = Annotated[ExecutionServices, Depends(get_execution_services)]

# A response carrying an API key must not be kept by the browser or a proxy.
_NO_STORE = {"Cache-Control": "no-store"}


@router.post(
    "",
    response_model=DeploymentWithKey,
    status_code=status.HTTP_201_CREATED,
    summary="Deploy a workflow as an API endpoint (or redeploy it)",
    description=(
        "Validates the workflow's saved graph and publishes a snapshot of it at "
        "`POST /api/v1/deployments/{deployment_id}/run`. The first deploy answers **201** with "
        "the API key in `api_key`: it is shown this once and stored hashed. Deploying the same "
        "workflow again answers **200**: the endpoint serves the current graph from then on, "
        "and the id and key stay the same (`api_key` is null). To replace the key, use "
        "`POST /api/deployments/{deployment_id}/rotate-key`."
    ),
    responses={
        200: {"model": DeploymentWithKey, "description": "Redeployed"},
        404: {"description": "Workflow not found (or not yours)"},
        422: {"description": "The graph wouldn't run; `detail.errors` lists why"},
    },
)
async def deploy(body: DeploymentCreate, db: DbSession, user: CurrentUser, services: Services) -> Any:
    workflow = await get_owned_workflow(db, body.workflow_id, user)
    try:
        deployment, key, created = await deploy_workflow(db, workflow, services)
    except InvalidWorkflowGraph as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "Workflow graph is invalid",
                "errors": [issue.model_dump(mode="json") for issue in exc.issues],
            },
        ) from None
    return _with_key(deployment, key, status.HTTP_201_CREATED if created else status.HTTP_200_OK)


def _with_key(deployment: Deployment, key: str | None, status_code: int) -> JSONResponse:
    payload = DeploymentWithKey(**deployment_read(deployment).model_dump(), api_key=key)
    return JSONResponse(status_code=status_code, content=jsonable_encoder(payload), headers=_NO_STORE)


@router.post(
    "/{deployment_id}/rotate-key",
    response_model=DeploymentWithKey,
    summary="Replace a deployment's API key",
    description=(
        "Issues a new key (in `api_key`, shown this once) and revokes the old one immediately. "
        "The deployed graph doesn't change."
    ),
    responses={404: {"description": "Deployment not found (or not yours)"}},
)
async def rotate_key(deployment_id: uuid.UUID, db: DbSession, user: CurrentUser) -> JSONResponse:
    deployment = await _owned(db, user, deployment_id)
    if deployment.revoked_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="This deployment was undeployed; deploy it again for a new key")
    key = await rotate_api_key(db, deployment)
    return _with_key(deployment, key, status.HTTP_200_OK)


async def _owned(db: DbSession, user: CurrentUser, deployment_id: uuid.UUID) -> Deployment:
    deployment = await db.scalar(
        select(Deployment)
        .where(Deployment.id == deployment_id, Deployment.owner_id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if deployment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    return deployment


@router.delete(
    "/{deployment_id}",
    response_model=DeploymentRead,
    summary="Undeploy (revoke) a deployment",
    description=(
        "Takes the endpoint down: from now on it answers **404**, even with the deployment's key, "
        "and the key never works again (deploying the workflow again issues a new one). The "
        "deployment isn't deleted: it stays, with `revoked_at` set, for history, and its runs stay "
        "in the executions list. Undeploying an undeployed deployment changes nothing."
    ),
    responses={404: {"description": "Deployment not found (or not yours)"}},
)
async def undeploy_deployment(deployment_id: uuid.UUID, db: DbSession, user: CurrentUser) -> DeploymentRead:
    deployment = await _owned(db, user, deployment_id)
    await undeploy(db, deployment)
    return deployment_read(deployment)


@router.get(
    "",
    response_model=list[DeploymentRead],
    summary="List your deployments (most recently deployed first)",
    description=(
        "Never includes API keys, only their `api_key_prefix`. Filter with `workflow_id`; undeployed ones "
        "are left out unless `include_revoked=true`."
    ),
)
async def list_deployments(
    db: DbSession, user: CurrentUser, workflow_id: uuid.UUID | None = None, include_revoked: bool = False
) -> list[DeploymentRead]:
    query = select(Deployment).where(Deployment.owner_id == user.id)
    if not include_revoked:
        query = query.where(Deployment.revoked_at.is_(None))
    if workflow_id is not None:
        query = query.where(Deployment.workflow_id == workflow_id)
    rows = await db.scalars(query.order_by(Deployment.deployed_at.desc()).execution_options(populate_existing=True))
    return [deployment_read(row) for row in rows]
