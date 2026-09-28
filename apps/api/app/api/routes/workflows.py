import socket
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from flowforge_engine import ExecutionServices, WorkflowGraph, queue_for_graph, validate_workflow
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, SessionFactoryDep, TaskQueueDep
from app.core.redis import get_redis
from app.models.enums import ExecutionStatus
from app.models.execution import WorkflowExecution
from app.models.workflow import Workflow
from app.schemas.execution import ExecutionAccepted, ExecutionDetail, ExecutionSummary, RunRequest
from app.schemas.workflow import (
    WorkflowCreate,
    WorkflowRead,
    WorkflowSummary,
    WorkflowUpdate,
    WorkflowValidation,
)
from app.services.providers import get_execution_services
from app.services.runs import (
    InvalidWorkflowGraph,
    close_out_nodes,
    create_execution,
    finish_execution,
    load_execution_detail,
    run_execution,
)
from app.services.task_queue import EnqueueFailed
from app.services.workflows import get_owned_workflow, replace_graph

router = APIRouter(prefix="/workflows", tags=["workflows"])

Services = Annotated[ExecutionServices, Depends(get_execution_services)]

_NOT_FOUND = {404: {"description": "Workflow not found (or not yours)"}}


@router.get("", response_model=list[WorkflowSummary], summary="List your workflows")
async def list_workflows(db: DbSession, user: CurrentUser) -> list[Workflow]:
    result = await db.scalars(
        select(Workflow).where(Workflow.owner_id == user.id).order_by(Workflow.updated_at.desc())
    )
    return list(result)


@router.post(
    "", response_model=WorkflowRead, status_code=status.HTTP_201_CREATED, summary="Create a workflow"
)
async def create_workflow(body: WorkflowCreate, db: DbSession, user: CurrentUser) -> Workflow:
    workflow = Workflow(
        name=body.name,
        description=body.description,
        owner_id=user.id,
        graph_json=WorkflowGraph().model_dump(mode="json"),
    )
    db.add(workflow)
    await db.commit()
    await db.refresh(workflow)
    return workflow


@router.get("/{workflow_id}", response_model=WorkflowRead, summary="Get a workflow", responses=_NOT_FOUND)
async def get_workflow(workflow_id: uuid.UUID, db: DbSession, user: CurrentUser) -> Workflow:
    return await get_owned_workflow(db, workflow_id, user)


@router.put(
    "/{workflow_id}",
    response_model=WorkflowRead,
    summary="Update a workflow",
    description=(
        "Only fields present in the body change. `graph` fully replaces the workflow's nodes, "
        "edges, and variables (and bumps `version`). The graph is stored even if it has "
        "semantic errors; use `/validate` to check it."
    ),
    responses={**_NOT_FOUND, 422: {"description": "Malformed graph (e.g. duplicate node ids)"}},
)
async def update_workflow(
    workflow_id: uuid.UUID, body: WorkflowUpdate, db: DbSession, user: CurrentUser
) -> Workflow:
    workflow = await get_owned_workflow(db, workflow_id, user)
    fields = body.model_fields_set
    if "name" in fields and body.name is not None:
        workflow.name = body.name
    if "description" in fields:
        workflow.description = body.description
    if "status" in fields and body.status is not None:
        workflow.status = body.status
    if "graph" in fields and body.graph is not None:
        await replace_graph(db, workflow, body.graph)
    await db.commit()
    await db.refresh(workflow)
    return workflow


@router.delete(
    "/{workflow_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a workflow and its history",
    responses=_NOT_FOUND,
)
async def delete_workflow(workflow_id: uuid.UUID, db: DbSession, user: CurrentUser) -> Response:
    workflow = await get_owned_workflow(db, workflow_id, user)
    await db.delete(workflow)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{workflow_id}/validate",
    response_model=WorkflowValidation,
    summary="Validate the saved graph",
    description=(
        "Returns every problem that would stop the graph from running; an empty `errors` list "
        "means valid. A node whose provider has no credentials (neither yours nor the server's) "
        "is reported with code `auth_missing` (\"Authentication missing for provider ...\")."
    ),
    responses=_NOT_FOUND,
)
async def validate(
    workflow_id: uuid.UUID, db: DbSession, user: CurrentUser, services: Services
) -> WorkflowValidation:
    workflow = await get_owned_workflow(db, workflow_id, user)
    errors = validate_workflow(WorkflowGraph.model_validate(workflow.graph_json), services=services)
    return WorkflowValidation(valid=not errors, errors=errors)


@router.post(
    "/{workflow_id}/run",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ExecutionAccepted,
    summary="Run the workflow (queued to a worker; `?sync=true` runs it in-request)",
    description=(
        "Validates the saved graph, records a `pending` execution, queues it for a Celery "
        "worker, and returns **202** immediately with the `execution_id`. Follow it live on "
        "`WS /ws/executions/{execution_id}` or poll `GET /api/executions/{execution_id}`.\n\n"
        "With `?sync=true` the run happens within this request instead and the response is "
        "**200** with the full execution (each node's resolved input, output, and duration). "
        "Either way a run that fails at a node ends with `status: failed`, and an invalid "
        "graph (including `auth_missing`) returns 422 without creating an execution."
    ),
    responses={
        **_NOT_FOUND,
        200: {"model": ExecutionDetail, "description": "`?sync=true`: the finished execution"},
        422: {"description": "Graph failed validation; `detail.errors` lists why"},
        503: {"description": "The task broker (Redis) is unreachable; the execution is marked failed"},
    },
)
async def run(
    workflow_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    services: Services,
    task_queue: TaskQueueDep,
    session_factory: SessionFactoryDep,
    body: RunRequest | None = None,
    sync: Annotated[bool, Query(description="Run within this request and return the finished execution (200).")] = False,
) -> Any:
    workflow = await get_owned_workflow(db, workflow_id, user)
    inputs = (body or RunRequest()).inputs
    queue = None if sync else queue_for_graph(WorkflowGraph.model_validate(workflow.graph_json))
    try:
        execution = await create_execution(db, workflow, user, inputs, services, queue=queue)
    except InvalidWorkflowGraph as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "Workflow graph is invalid",
                "errors": [issue.model_dump(mode="json") for issue in exc.issues],
            },
        ) from None

    if sync:
        await run_execution(
            execution.id, session_factory=session_factory, redis=get_redis(),
            worker_id=f"api@{socket.gethostname()}", services=services,
        )
        detail = await load_execution_detail(db, execution.id, user)
        assert detail is not None
        return JSONResponse(status_code=status.HTTP_200_OK, content=jsonable_encoder(detail))

    assert queue is not None
    try:
        await task_queue.enqueue(execution.id, queue)
    except EnqueueFailed as exc:
        error = f"Could not queue the run: the task broker is unavailable ({exc})"
        await finish_execution(db, None, execution.id, ExecutionStatus.FAILED, error=error,
                               from_statuses=(ExecutionStatus.PENDING,))
        await close_out_nodes(db, None, execution.id, pending_reason="Not run: could not be queued",
                              running_reason="Not run: could not be queued")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=error) from None
    return ExecutionAccepted.build(execution.id, workflow.id, ExecutionStatus.PENDING, queue)


@router.get(
    "/{workflow_id}/executions",
    response_model=list[ExecutionSummary],
    summary="List a workflow's past executions (newest first)",
    responses=_NOT_FOUND,
)
async def list_executions(
    workflow_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[WorkflowExecution]:
    await get_owned_workflow(db, workflow_id, user)
    result = await db.scalars(
        select(WorkflowExecution)
        .where(WorkflowExecution.workflow_id == workflow_id)
        .order_by(WorkflowExecution.started_at.desc().nulls_last(), WorkflowExecution.id)
        .limit(limit)
        .offset(offset)
    )
    return list(result)
