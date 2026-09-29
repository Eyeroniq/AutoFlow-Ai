"""Deployments: a workflow published as POST /api/v1/deployments/{id}/run.

- **Snapshot:** deploying copies the saved graph (validated first) into the deployment, and
  runs use that copy, so editing the pipeline doesn't change what callers get until it is
  redeployed. A redeploy keeps the id, the endpoint, and the API key.
- **API key:** issued on the first deploy and returned in that response only; rotating it
  (separately from redeploying) issues a new one and revokes the old one at once. The table
  keeps its SHA-256 hash and an 8-character display prefix.
- **Runs** go through the same path as the editor's Run: a pending execution (trigger
  `api`, with `deployment_id`), queued to Celery. With ?wait=true the request then waits
  for it on the execution's event channel, re-checking the database every second.
"""

import asyncio
import contextlib
import json
import logging
import uuid
from typing import Any

from flowforge_engine import ExecutionServices, GraphError, WorkflowGraph, topological_sort, validate_workflow
from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import API_KEY_DISPLAY_CHARS, generate_api_key, hash_api_key
from app.db.session import SessionFactory
from app.models.deployment import Deployment
from app.models.execution import WorkflowExecution
from app.models.workflow import Workflow
from app.schemas.deployment import (
    DeploymentExecution,
    DeploymentExecutionLinks,
    DeploymentInput,
    DeploymentOutput,
    DeploymentRead,
)
from app.services.events import events_channel
from app.services.runs import TERMINAL_STATUSES, InvalidWorkflowGraph, utcnow

logger = logging.getLogger(__name__)

# How often a waiting request re-reads the execution's status, in case it missed the
# execution.finished event (Redis hiccup, or the run finished before it subscribed).
WAIT_DB_CHECK_SECONDS = 1.0


def endpoint_path(deployment_id: uuid.UUID) -> str:
    return f"/api/v1/deployments/{deployment_id}/run"


def status_path(deployment_id: uuid.UUID, execution_id: uuid.UUID) -> str:
    return f"/api/v1/deployments/{deployment_id}/executions/{execution_id}"


def describe_io(graph: WorkflowGraph) -> tuple[list[DeploymentInput], list[DeploymentOutput]]:
    """The graph's Input and Output nodes, in execution order."""
    by_id = {node.id: node for node in graph.nodes}
    try:
        order = topological_sort(graph.nodes, graph.edges)
    except GraphError:  # the deployed graph validated; declaration order is fine as a fallback
        order = list(by_id)
    inputs, outputs = [], []
    for node_id in order:
        node = by_id[node_id]
        config = node.config or {}
        if node.type == "input":
            name = config.get("name") or node.id
            default = config.get("default")
            inputs.append(DeploymentInput(
                node_id=node.id,
                label=node.label or name,
                name=name,
                type=config.get("input_type") or "text",
                required=config.get("required", True) is not False and default is None,
                default=default,
            ))
        elif node.type == "output":
            name = config.get("name") or "result"
            outputs.append(DeploymentOutput(node_id=node.id, label=node.label or name, name=name))
    return inputs, outputs


def deployment_read(deployment: Deployment) -> DeploymentRead:
    inputs, outputs = describe_io(WorkflowGraph.model_validate(deployment.graph_json))
    return DeploymentRead(
        id=deployment.id,
        workflow_id=deployment.workflow_id,
        name=deployment.name,
        version=deployment.version,
        workflow_version=deployment.workflow_version,
        endpoint=endpoint_path(deployment.id),
        api_key_prefix=deployment.api_key_prefix,
        inputs=inputs,
        outputs=outputs,
        key_created_at=deployment.key_created_at,
        deployed_at=deployment.deployed_at,
        created_at=deployment.created_at,
        updated_at=deployment.updated_at,
    )


def _issue_key(deployment: Deployment) -> str:
    key = generate_api_key()
    deployment.api_key_hash = hash_api_key(key)
    deployment.api_key_prefix = key[:API_KEY_DISPLAY_CHARS]
    deployment.key_created_at = utcnow()
    return key


async def deploy_workflow(
    db: AsyncSession, workflow: Workflow, services: ExecutionServices
) -> tuple[Deployment, str | None, bool]:
    """Deploy (or redeploy) `workflow`'s saved graph. Returns (deployment, the API key on a
    first deploy else None, created). Raises InvalidWorkflowGraph (changing nothing) if the
    graph wouldn't run, including providers without credentials."""
    # Serialize deploys of one workflow (two first deploys can't both insert), and deploy
    # the graph as saved right now, even if a save landed since `workflow` was loaded.
    await db.scalar(
        select(Workflow).where(Workflow.id == workflow.id).with_for_update().execution_options(populate_existing=True)
    )
    graph = WorkflowGraph.model_validate(workflow.graph_json)
    if issues := validate_workflow(graph, services=services):
        raise InvalidWorkflowGraph(issues)

    deployment = await db.scalar(
        select(Deployment).where(Deployment.workflow_id == workflow.id).execution_options(populate_existing=True)
    )
    now = utcnow()
    created = deployment is None
    key = None
    if deployment is None:
        deployment = Deployment(id=uuid.uuid4(), workflow_id=workflow.id, owner_id=workflow.owner_id, version=1)
        key = _issue_key(deployment)
        db.add(deployment)
    else:
        deployment.version += 1
    deployment.name = workflow.name
    deployment.graph_json = graph.model_dump(mode="json")
    deployment.workflow_version = workflow.version
    deployment.deployed_at = now
    await db.commit()
    await db.refresh(deployment)
    logger.info("workflow deployed", extra={
        "deployment_id": str(deployment.id), "workflow_id": str(workflow.id), "version": deployment.version,
        "new_key": key is not None,
    })
    return deployment, key, created


async def rotate_api_key(db: AsyncSession, deployment: Deployment) -> str:
    """Issue a new API key; the old one stops working with this commit. The deployed graph
    is untouched, so unfinished edits aren't published along the way."""
    key = _issue_key(deployment)
    await db.commit()
    await db.refresh(deployment)
    logger.info("deployment key rotated", extra={"deployment_id": str(deployment.id)})
    return key


async def deployment_execution(
    db: AsyncSession, deployment_id: uuid.UUID, execution_id: uuid.UUID
) -> DeploymentExecution | None:
    """An execution this deployment started (None for any other execution)."""
    execution = await db.scalar(
        select(WorkflowExecution)
        .where(WorkflowExecution.id == execution_id, WorkflowExecution.deployment_id == deployment_id)
        .execution_options(populate_existing=True)
    )
    if execution is None:
        return None
    return DeploymentExecution(
        execution_id=execution.id,
        deployment_id=deployment_id,
        status=execution.status,
        final_output=execution.final_output_json,
        error=execution.error_message,
        created_at=execution.created_at,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        duration_ms=(
            round((execution.finished_at - execution.started_at).total_seconds() * 1000)
            if execution.started_at and execution.finished_at else None
        ),
        links=DeploymentExecutionLinks(status=status_path(deployment_id, execution.id)),
    )


@contextlib.asynccontextmanager
async def execution_events(redis: Redis, execution_id: uuid.UUID):
    """Subscribed to the execution's event channel (None if Redis is unreachable: the
    waiter then only polls the database)."""
    pubsub: PubSub | None = None
    try:
        pubsub = redis.pubsub()
        await pubsub.subscribe(events_channel(execution_id))
    except (RedisError, OSError) as exc:
        logger.warning("can't subscribe to execution events; polling instead",
                       extra={"execution_id": str(execution_id), "error": str(exc)})
        pubsub = None
    try:
        yield pubsub
    finally:
        if pubsub is not None:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe()
                await pubsub.aclose()


async def _finished(session_factory: SessionFactory, execution_id: uuid.UUID) -> bool:
    async with session_factory() as db:
        status = await db.scalar(select(WorkflowExecution.status).where(WorkflowExecution.id == execution_id))
    return status in TERMINAL_STATUSES


async def wait_for_execution(
    session_factory: SessionFactory, pubsub: PubSub | None, execution_id: uuid.UUID, timeout: float
) -> bool:
    """Wait up to `timeout` seconds for the execution to finish; True if it did."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_check = float("-inf")
    finished_event = False
    while True:
        now = loop.time()
        if finished_event or now - last_check >= WAIT_DB_CHECK_SECONDS:
            last_check, finished_event = now, False
            if await _finished(session_factory, execution_id):
                return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        step = min(remaining, max(0.01, last_check + WAIT_DB_CHECK_SECONDS - loop.time()))
        if pubsub is None:
            await asyncio.sleep(step)
            continue
        try:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=step)
        except (RedisError, OSError):
            pubsub = None
            continue
        if message and message.get("type") == "message":
            finished_event = _event_type(message.get("data")) == "execution.finished"


def _event_type(data: Any) -> str | None:
    try:
        event = json.loads(data)
    except (TypeError, ValueError):
        return None
    return event.get("type") if isinstance(event, dict) else None
