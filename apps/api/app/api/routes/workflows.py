import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from flowforge_engine import ExecutionServices, WorkflowGraph, validate_workflow
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.execution import WorkflowExecution
from app.models.workflow import Workflow
from app.schemas.execution import ExecutionDetail, ExecutionSummary, RunRequest
from app.schemas.workflow import (
    WorkflowCreate,
    WorkflowRead,
    WorkflowSummary,
    WorkflowUpdate,
    WorkflowValidation,
)
from app.services.providers import get_execution_services
from app.services.runs import InvalidWorkflowGraph, load_execution_detail, run_workflow
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
    description="Returns every problem that would stop the graph from running; an empty `errors` list means valid.",
    responses=_NOT_FOUND,
)
async def validate(workflow_id: uuid.UUID, db: DbSession, user: CurrentUser) -> WorkflowValidation:
    workflow = await get_owned_workflow(db, workflow_id, user)
    errors = validate_workflow(WorkflowGraph.model_validate(workflow.graph_json))
    return WorkflowValidation(valid=not errors, errors=errors)


@router.post(
    "/{workflow_id}/run",
    response_model=ExecutionDetail,
    summary="Run the workflow now (synchronously)",
    description=(
        "Executes the saved graph within this request and returns the full execution, "
        "including each node's resolved input, output, and duration. A run that fails at "
        "a node still returns 200 with `status: failed`; an invalid graph returns 422 "
        "without creating an execution."
    ),
    responses={**_NOT_FOUND, 422: {"description": "Graph failed validation; `detail.errors` lists why"}},
)
async def run(
    workflow_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    services: Services,
    body: RunRequest | None = None,
) -> ExecutionDetail:
    workflow = await get_owned_workflow(db, workflow_id, user)
    try:
        execution_id = await run_workflow(db, workflow, user, (body or RunRequest()).inputs, services)
    except InvalidWorkflowGraph as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "Workflow graph is invalid",
                "errors": [issue.model_dump(mode="json") for issue in exc.issues],
            },
        ) from None
    detail = await load_execution_detail(db, execution_id, user)
    assert detail is not None
    return detail


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
