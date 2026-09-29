import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, TaskQueueDep
from app.core.redis import get_redis
from app.models.enums import ExecutionStatus, ExecutionTrigger
from app.models.execution import WorkflowExecution
from app.models.workflow import Workflow
from app.schemas.execution import ExecutionDetail, ExecutionListItem, ExecutionSummary
from app.services.control import ExecutionAlreadyFinished, stop_execution
from app.services.exports import to_csv, to_json
from app.services.runs import load_execution_detail, owned_execution

router = APIRouter(prefix="/executions", tags=["executions"])

_NOT_FOUND = {404: {"description": "Execution not found (or not yours)"}}


@router.get(
    "",
    response_model=list[ExecutionListItem],
    summary="Your executions across all workflows (newest first)",
)
async def list_executions(
    db: DbSession,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    status_: Annotated[ExecutionStatus | None, Query(alias="status")] = None,
    workflow_id: uuid.UUID | None = None,
    trigger: Annotated[ExecutionTrigger | None, Query(description="manual, schedule, email, or webhook")] = None,
) -> list[ExecutionListItem]:
    query = (
        select(WorkflowExecution, Workflow.name)
        .join(Workflow, Workflow.id == WorkflowExecution.workflow_id)
        .where(Workflow.owner_id == user.id)
    )
    if status_ is not None:
        query = query.where(WorkflowExecution.status == status_)
    if workflow_id is not None:
        query = query.where(WorkflowExecution.workflow_id == workflow_id)
    if trigger is not None:
        query = query.where(WorkflowExecution.trigger == trigger)
    rows = await db.execute(
        query.order_by(WorkflowExecution.created_at.desc(), WorkflowExecution.id).limit(limit).offset(offset)
    )
    return [
        ExecutionListItem(**ExecutionSummary.model_validate(execution).model_dump(), workflow_name=name)
        for execution, name in rows
    ]


@router.get(
    "/{execution_id}",
    response_model=ExecutionDetail,
    summary="Get one execution with every node's result",
    responses=_NOT_FOUND,
)
async def get_execution(execution_id: uuid.UUID, db: DbSession, user: CurrentUser) -> ExecutionDetail:
    detail = await load_execution_detail(db, execution_id, user)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Execution not found")
    return detail


@router.get(
    "/{execution_id}/output",
    summary="Download the final output as JSON or CSV",
    description=(
        "`format=json` (default) is the final output as is. `format=csv` flattens it: one row per object in any "
        "list (e.g. one per extracted entity) with its fields as columns, plus one row per other value; the "
        "`group` column says where each row came from. UTF-8 with a byte-order mark, so Excel opens it cleanly."
    ),
    responses={**_NOT_FOUND, 404: {"description": "Execution not found, or it has no final output (yet)"}},
    response_class=Response,
)
async def download_output(
    execution_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    fmt: Annotated[Literal["json", "csv"], Query(alias="format", description="json or csv")] = "json",
) -> Response:
    execution = await owned_execution(db, execution_id, user)
    if execution is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Execution not found")
    if not execution.final_output_json:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="This execution has no final output")
    name = f"execution-{str(execution.id)[:8]}-output.{fmt}"
    if fmt == "csv":
        body, media = "﻿" + to_csv(execution.final_output_json), "text/csv; charset=utf-8"
    else:
        body, media = to_json(execution.final_output_json), "application/json"
    return Response(content=body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post(
    "/{execution_id}/stop",
    response_model=ExecutionDetail,
    summary="Stop a pending or running execution",
    description=(
        "A **pending** execution is marked `stopped` at once and its queued task revoked. A "
        "**running** one is signalled: the worker cancels the current node if it can be "
        "interrupted (Delay, LLM calls; an email being sent is allowed to finish), skips the "
        "remaining nodes, and marks the execution `stopped`. The response is **200** with the "
        "final state once that happened (normally well under a second), or **202** with the "
        "current state if the worker hasn't confirmed within `EXECUTION_STOP_WAIT_SECONDS`. "
        "If the worker is unresponsive the execution is marked stopped directly."
    ),
    responses={
        **_NOT_FOUND,
        202: {"model": ExecutionDetail, "description": "Stop requested; the worker hasn't confirmed yet"},
        409: {"description": "The execution already finished"},
    },
)
async def stop(
    execution_id: uuid.UUID, db: DbSession, user: CurrentUser, task_queue: TaskQueueDep, response: Response
) -> ExecutionDetail:
    if await owned_execution(db, execution_id, user) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Execution not found")
    try:
        outcome = await stop_execution(db, execution_id, redis=get_redis(), task_queue=task_queue)
    except ExecutionAlreadyFinished as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from None
    if not outcome.finished:
        response.status_code = status.HTTP_202_ACCEPTED
    detail = await load_execution_detail(db, execution_id, user)
    assert detail is not None
    return detail
