import copy as copy_module
import logging
import socket
import time
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from flowforge_engine import (
    ExecutionServices,
    NodeContext,
    WorkflowGraph,
    execute_node,
    queue_for_graph,
    validate_workflow,
)
from flowforge_engine.errors import ProviderError
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession, SessionFactoryDep, TaskQueueDep
from app.core.config import settings
from app.core.rate_limit import limiter, per_user
from app.core.redis import get_redis
from app.models.enums import ExecutionStatus
from app.models.execution import WorkflowExecution
from app.models.trigger import WorkflowTrigger
from app.models.workflow import Workflow
from app.schemas.execution import ExecutionAccepted, ExecutionDetail, ExecutionSummary, RunRequest
from app.schemas.trigger import TriggerBrief
from app.schemas.workflow import (
    LastExecution,
    NodeTestRequest,
    GenerateRequest,
    GenerateResult,
    NodeTestResult,
    PrivacySettings,
    ValidateRequest,
    WorkflowCreate,
    WorkflowListItem,
    WorkflowRead,
    WorkflowSummary,
    WorkflowUpdate,
    WorkflowValidation,
)
from app.services.files import file_input_issues
from app.services.node_state import DbNodeStateStore
from app.services.privacy import policy_for
from app.services.generation import MAX_ATTEMPTS, GenerationFailed, LLMCall, generate, owner_llm
from app.services.templates import fit_graph
from app.services.triggers import switch_off_blocks, sync_trigger_blocks
from app.services.providers import get_execution_services
from app.services.runs import (
    InvalidWorkflowGraph,
    create_execution,
    fail_unqueued,
    load_execution_detail,
    run_execution,
)
from app.services.task_queue import EnqueueFailed
from app.services.workflows import get_owned_workflow, replace_graph

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workflows", tags=["workflows"])

Services = Annotated[ExecutionServices, Depends(get_execution_services)]

_NOT_FOUND = {404: {"description": "Workflow not found (or not yours)"}}


@router.get(
    "",
    response_model=list[WorkflowListItem],
    summary="List your workflows",
    description="Newest-edited first, each with its node count and most recent execution.",
)
async def list_workflows(db: DbSession, user: CurrentUser) -> list[WorkflowListItem]:
    workflows = list(await db.scalars(
        select(Workflow).where(Workflow.owner_id == user.id).order_by(Workflow.updated_at.desc())
    ))
    latest: dict[uuid.UUID, WorkflowExecution] = {}
    triggers: dict[uuid.UUID, list[TriggerBrief]] = {}
    if workflows:
        for row in await db.scalars(
            select(WorkflowTrigger)
            .where(WorkflowTrigger.workflow_id.in_([w.id for w in workflows]))
            .order_by(WorkflowTrigger.type)
        ):
            triggers.setdefault(row.workflow_id, []).append(
                TriggerBrief(type=row.type, enabled=row.enabled, auto_disabled=row.auto_disabled_at is not None and not row.enabled)
            )
        rows = await db.scalars(
            select(WorkflowExecution)
            .where(WorkflowExecution.workflow_id.in_([w.id for w in workflows]))
            .distinct(WorkflowExecution.workflow_id)
            .order_by(WorkflowExecution.workflow_id, WorkflowExecution.created_at.desc())
        )
        latest = {row.workflow_id: row for row in rows}
    return [
        WorkflowListItem(
            **WorkflowSummary.model_validate(w).model_dump(),
            node_count=len((w.graph_json or {}).get("nodes", [])),
            last_execution=LastExecution.model_validate(latest[w.id]) if w.id in latest else None,
            triggers=triggers.get(w.id, []),
        )
        for w in workflows
    ]


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
    workflow_id: uuid.UUID, body: WorkflowUpdate, db: DbSession, user: CurrentUser, services: Services
) -> Workflow:
    workflow = await get_owned_workflow(db, workflow_id, user)
    previous_graph = copy_module.deepcopy(workflow.graph_json)
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
    if "graph" in fields and body.graph is not None:
        # Trigger blocks (Schedule, Email, Telegram) become the workflow's real triggers.
        await sync_trigger_blocks(db, workflow, previous_graph, services)
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
    summary="Validate the saved graph (or an unsaved one)",
    description=(
        "Returns every problem that would stop the graph from running; an empty `errors` list "
        "means valid. A node whose provider has no credentials (neither yours nor the server's) "
        "is reported with code `auth_missing` (\"Authentication missing for provider ...\"). "
        "Send `{\"graph\": {...}}` to validate that graph instead of the saved one (nothing is stored)."
    ),
    responses=_NOT_FOUND,
)
async def validate(
    workflow_id: uuid.UUID,
    db: DbSession,
    user: CurrentUser,
    services: Services,
    body: ValidateRequest | None = None,
) -> WorkflowValidation:
    workflow = await get_owned_workflow(db, workflow_id, user)
    graph = body.graph if body and body.graph is not None else WorkflowGraph.model_validate(workflow.graph_json)
    errors = validate_workflow(graph, services=services)
    return WorkflowValidation(valid=not errors, errors=errors)


def get_generator_llm(services: Services) -> LLMCall:
    """The LLM that drafts pipelines (overridden in tests)."""
    return owner_llm(services)


@router.post(
    "/generate",
    response_model=GenerateResult,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a pipeline from a plain-English description",
    description=(
        "An LLM drafts the graph from the node catalog; the draft must use real node types and pass validation "
        f"(it gets the problems back and retries, up to {MAX_ATTEMPTS} attempts). The pipeline is saved as a "
        "draft only when it validates; `warnings` lists providers to connect before it can run."
    ),
    responses={422: {"description": "No valid graph after the retries: `detail.problems` says why"},
               502: {"description": "No LLM answered"}},
)
async def generate_workflow(
    body: GenerateRequest, db: DbSession, user: CurrentUser, services: Services,
    llm: Annotated[LLMCall, Depends(get_generator_llm)],
) -> GenerateResult:
    my_email = services.settings.gmail.username or user.email
    try:
        generated = await generate(llm, body.prompt, services, my_email=my_email,
                                   fit=lambda graph: fit_graph(graph, services, email_to=my_email))
    except GenerationFailed as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": f"Couldn't produce a valid pipeline in {exc.attempts} attempts", "problems": exc.problems[:15]},
        ) from None
    except ProviderError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from None
    workflow = Workflow(name=generated.name, description=generated.description, owner_id=user.id,
                        graph_json=WorkflowGraph().model_dump(mode="json"))
    db.add(workflow)
    await db.flush()
    await replace_graph(db, workflow, WorkflowGraph.model_validate(generated.graph))
    await db.commit()
    await db.refresh(workflow)
    return GenerateResult(workflow=WorkflowRead.model_validate(workflow), attempts=generated.attempts, warnings=generated.warnings)


@router.get(
    "/{workflow_id}/privacy", response_model=PrivacySettings, summary="The workflow's privacy settings", responses=_NOT_FOUND,
)
async def get_privacy(workflow_id: uuid.UUID, db: DbSession, user: CurrentUser) -> PrivacySettings:
    workflow = await get_owned_workflow(db, workflow_id, user)
    return PrivacySettings.model_validate(workflow.privacy_json or {})


@router.put(
    "/{workflow_id}/privacy",
    response_model=PrivacySettings,
    summary="Set the workflow's privacy settings",
    description=(
        "Applies to runs started from now on: masking of stored step data, whether personal data (names, emails, "
        "phones, locations) is detected too, and the allowlist of known-safe values."
    ),
    responses=_NOT_FOUND,
)
async def put_privacy(workflow_id: uuid.UUID, body: PrivacySettings, db: DbSession, user: CurrentUser) -> PrivacySettings:
    workflow = await get_owned_workflow(db, workflow_id, user)
    workflow.privacy_json = body.model_dump()
    await db.commit()
    return body


@router.post(
    "/{workflow_id}/duplicate",
    response_model=WorkflowRead,
    status_code=status.HTTP_201_CREATED,
    summary="Copy a workflow (graph, variables, and description; not its history)",
    responses=_NOT_FOUND,
)
async def duplicate_workflow(workflow_id: uuid.UUID, db: DbSession, user: CurrentUser) -> Workflow:
    source = await get_owned_workflow(db, workflow_id, user)
    copy = Workflow(
        name=f"{source.name} (copy)"[:255],
        description=source.description,
        owner_id=user.id,
        graph_json=WorkflowGraph().model_dump(mode="json"),
    )
    db.add(copy)
    await db.flush()
    await replace_graph(db, copy, WorkflowGraph.model_validate(switch_off_blocks(source.graph_json)))
    copy.version = 1
    await db.commit()
    await db.refresh(copy)
    return copy


@router.post(
    "/{workflow_id}/nodes/{node_key}/test",
    response_model=NodeTestResult,
    summary="Run one node in isolation with sample data",
    description=(
        "Runs just this node, as a real run would (real providers: a Gmail node really sends), "
        "resolving `{{...}}` references against `upstream_outputs`, `variables`, and `inputs` "
        "from the body. Pass `config` to test unsaved edits. Returns the resolved input, the "
        "output, the duration, and the error, if any. Nothing is recorded in the execution history."
    ),
    responses={**_NOT_FOUND, 404: {"description": "Workflow or node not found"}},
)
async def run_node_test(
    workflow_id: uuid.UUID,
    node_key: str,
    db: DbSession,
    user: CurrentUser,
    services: Services,
    session_factory: SessionFactoryDep,
    body: NodeTestRequest | None = None,
) -> NodeTestResult:
    workflow = await get_owned_workflow(db, workflow_id, user)
    graph = WorkflowGraph.model_validate(workflow.graph_json)
    node = next((n for n in graph.nodes if n.id == node_key), None)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Node '{node_key}' is not in the saved graph")
    body = body or NodeTestRequest()
    if body.config is not None:
        node = node.model_copy(update={"config": body.config})
    # Reads what earlier runs saved (an RSS feed's position) without moving it.
    services.state = DbNodeStateStore(session_factory, workflow.id, None)
    services.privacy = policy_for(workflow.privacy_json)
    context = NodeContext(
        workflow_id=str(workflow.id),
        execution_id=f"test-{uuid.uuid4()}",
        variables={v.key: v.value for v in graph.variables} | body.variables,
        node_outputs=body.upstream_outputs,
        inputs=body.inputs,
        services=services,
    )
    try:
        result = await execute_node(node, context, node_timeout=settings.WORKFLOW_NODE_TIMEOUT_SECONDS)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    payload = result.model_dump(mode="json", include={"input", "output"})
    return NodeTestResult(
        node_key=result.node_id,
        node_type=result.node_type,
        label=result.label,
        status=result.status.value,
        input=payload["input"],
        output=payload["output"],
        error=result.error or result.skip_reason,
        started_at=result.started_at,
        finished_at=result.finished_at,
        duration_ms=result.duration_ms,
    )


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
@limiter.limit(lambda: settings.WORKFLOW_RUN_RATE_LIMIT, key_func=per_user)
async def run(
    request: Request,
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
    graph = WorkflowGraph.model_validate(workflow.graph_json)
    if issues := await file_input_issues(db, user.id, graph, inputs):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail={"message": "Invalid run inputs", "errors": issues}
        )
    queue = None if sync else queue_for_graph(graph)
    started = time.perf_counter()
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
        services.state = DbNodeStateStore(session_factory, workflow.id, execution.id)
        await run_execution(
            execution.id, session_factory=session_factory, redis=get_redis(),
            worker_id=f"api@{socket.gethostname()}", services=services,
        )
        detail = await load_execution_detail(db, execution.id, user)
        assert detail is not None
        return JSONResponse(status_code=status.HTTP_200_OK, content=jsonable_encoder(detail))

    assert queue is not None
    try:
        created = time.perf_counter()
        await task_queue.enqueue(execution.id, queue)
        sent = time.perf_counter()
        if sent - started > 1:
            logger.warning("slow run request", extra={
                "execution_id": str(execution.id), "create_ms": round((created - started) * 1000),
                "enqueue_ms": round((sent - created) * 1000),
            })
    except EnqueueFailed as exc:
        error = await fail_unqueued(db, execution.id, exc)
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
