"""Calling a deployment: /api/v1/deployments/{id}/..., authenticated by its API key (not a
user's JWT) and rate limited per deployment."""

import contextlib
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from flowforge_engine import WorkflowGraph, queue_for_graph

from app.api.deps import DbSession, DeploymentByKey, SessionFactoryDep, TaskQueueDep
from app.core.config import settings
from app.core.rate_limit import limiter
from app.core.redis import get_redis
from app.models.enums import ExecutionTrigger, TriggerType
from app.models.user import User
from app.models.workflow import Workflow
from app.schemas.deployment import DeploymentExecution, DeploymentRunRequest
from app.services.credentials import build_execution_services
from app.services.deployments import deployment_execution, execution_events, wait_for_execution
from app.services.files import file_input_issues
from app.services.runs import TERMINAL_STATUSES, InvalidWorkflowGraph, create_execution, fail_unqueued, utcnow
from app.services.task_queue import EnqueueFailed
from app.services.triggers import ensure_trigger, limit_problem

router = APIRouter(prefix="/v1/deployments", tags=["deployment API"])

_AUTH = {401: {"description": "Missing or invalid API key (or no such deployment)"}}
_RATE = {429: {"description": "Rate limited (per deployment)"}}


def _per_deployment(request: Request) -> str:
    return f"deployment:{request.path_params.get('deployment_id')}"


# Callables, so the limits are read per request (and tests can lower them).
def _run_limit() -> str:
    return settings.DEPLOYMENT_RUN_RATE_LIMIT


def _status_limit() -> str:
    return settings.DEPLOYMENT_STATUS_RATE_LIMIT


@router.post(
    "/{deployment_id}/run",
    response_model=DeploymentExecution,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Run a deployed workflow",
    description=(
        "Authenticate with the deployment's API key: `Authorization: Bearer <key>` or "
        "`X-API-Key: <key>`. The body is `{\"inputs\": {...}}`, keyed by the Input nodes' names.\n\n"
        "By default the run is queued and the answer is **202** with the `execution_id` and "
        "`links.status` (poll it with the same key). With `?wait=true` the request waits up "
        "to `timeout` seconds (default `DEPLOYMENT_WAIT_TIMEOUT_SECONDS`, at most "
        "`DEPLOYMENT_MAX_WAIT_SECONDS`) and answers **200** with the final output once the run "
        "finishes (a run that failed has `status: failed` and `error`), or **202** if it is still "
        "going. Rate limited per deployment (`DEPLOYMENT_RUN_RATE_LIMIT`).\n\n"
        "This endpoint is the workflow's **webhook trigger**: runs are recorded with trigger `webhook`, count "
        "toward the workflow's triggered runs per hour (429 beyond it), and fail the trigger's consecutive-failure "
        "count; when the trigger is switched off (in the Triggers panel, or by that limit) calls get 409."
    ),
    responses={
        **_AUTH,
        **_RATE,
        200: {"model": DeploymentExecution, "description": "`?wait=true`: the run finished"},
        409: {"description": "The workflow's webhook trigger is switched off"},
        422: {"description": "Invalid inputs, or the deployed graph no longer validates (e.g. a removed credential)"},
        503: {"description": "The task broker (Redis) is unreachable; the execution is marked failed"},
    },
)
@limiter.limit(_run_limit, key_func=_per_deployment)
async def run_deployment(
    request: Request,
    deployment: DeploymentByKey,
    db: DbSession,
    task_queue: TaskQueueDep,
    session_factory: SessionFactoryDep,
    body: DeploymentRunRequest | None = None,
    wait: Annotated[bool, Query(description="Wait for the run to finish and return its final output.")] = False,
    timeout: Annotated[
        float | None,
        Query(gt=0, le=settings.DEPLOYMENT_MAX_WAIT_SECONDS, description="With `wait=true`: seconds to wait."),
    ] = None,
) -> Any:
    inputs = (body or DeploymentRunRequest()).inputs
    graph = WorkflowGraph.model_validate(deployment.graph_json)
    owner = await db.get(User, deployment.owner_id)
    workflow = await db.get(Workflow, deployment.workflow_id)
    assert owner is not None and workflow is not None  # both cascade-delete the deployment
    if issues := await file_input_issues(db, owner.id, graph, inputs):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail={"message": "Invalid run inputs", "errors": issues}
        )
    services = await build_execution_services(db, owner)
    queue = queue_for_graph(graph)
    now = utcnow()
    trigger = await ensure_trigger(db, workflow.id, TriggerType.WEBHOOK, enabled=True)
    if not trigger.enabled:
        detail = trigger.disabled_reason or "The webhook trigger is switched off; turn it on in the editor's Triggers panel"
        await db.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, detail=detail)
    if problem := await limit_problem(db, workflow, now):
        trigger.last_error, trigger.last_error_at = problem, now
        await db.commit()
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail=problem)
    try:
        execution = await create_execution(
            db, workflow, None, inputs, services, queue=queue, graph=graph, trigger=ExecutionTrigger.WEBHOOK,
            deployment_id=deployment.id, trigger_id=trigger.id, created_at=now,
        )
    except InvalidWorkflowGraph as exc:
        await db.commit()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "The deployed workflow can't run; fix it and redeploy",
                "errors": [issue.model_dump(mode="json") for issue in exc.issues],
            },
        ) from None

    # Subscribed before queueing, so even an instant run's finish event isn't missed.
    async with (execution_events(get_redis(), execution.id) if wait else contextlib.nullcontext()) as events:
        try:
            await task_queue.enqueue(execution.id, queue)
        except EnqueueFailed as exc:
            error = await fail_unqueued(db, execution.id, exc)
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=error) from None
        if wait:
            await wait_for_execution(
                session_factory, events, execution.id, timeout or settings.DEPLOYMENT_WAIT_TIMEOUT_SECONDS
            )

    async with session_factory() as fresh:
        result = await deployment_execution(fresh, deployment.id, execution.id)
    assert result is not None
    finished = result.status in TERMINAL_STATUSES
    return JSONResponse(
        status_code=status.HTTP_200_OK if wait and finished else status.HTTP_202_ACCEPTED,
        content=jsonable_encoder(result),
    )


@router.get(
    "/{deployment_id}/executions/{execution_id}",
    response_model=DeploymentExecution,
    summary="Status and final output of a run this deployment started",
    description="Same API key as the run. Rate limited per deployment (`DEPLOYMENT_STATUS_RATE_LIMIT`).",
    responses={**_AUTH, **_RATE, 404: {"description": "Not a run of this deployment"}},
)
@limiter.limit(_status_limit, key_func=_per_deployment)
async def get_deployment_run(
    request: Request, execution_id: uuid.UUID, deployment: DeploymentByKey, db: DbSession
) -> DeploymentExecution:
    result = await deployment_execution(db, deployment.id, execution_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Execution not found")
    return result
