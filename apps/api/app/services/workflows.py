import uuid

from fastapi import HTTPException, status
from flowforge_engine import WorkflowGraph, get_node_definition
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import VariableType
from app.models.user import User
from app.models.workflow import Workflow, WorkflowEdge, WorkflowNode, WorkflowVariable


async def get_owned_workflow(db: AsyncSession, workflow_id: uuid.UUID, user: User) -> Workflow:
    """The workflow if `user` owns it; 404 otherwise (so other users' ids aren't revealed)."""
    workflow = await db.scalar(
        select(Workflow).where(Workflow.id == workflow_id, Workflow.owner_id == user.id)
    )
    if workflow is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    return workflow


def _duplicate_node_ids(graph: WorkflowGraph) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for node in graph.nodes:
        if node.id in seen and node.id not in duplicates:
            duplicates.append(node.id)
        seen.add(node.id)
    return duplicates


async def replace_graph(db: AsyncSession, workflow: Workflow, graph: WorkflowGraph) -> None:
    """Make graph_json and the node/edge/variable tables match `graph` exactly.

    Nodes are matched by graph id: a node that survives the save keeps its row (and so
    its execution-history links); removed nodes are deleted, which sets their
    NodeExecution.node_id to NULL. Edges and variables are rewritten wholesale.

    Semantic problems (unknown types, cycles, bad references) are allowed here — saving
    an unfinished graph is normal; /validate reports them. Duplicate node ids are
    rejected because they can't be mapped to rows.
    """
    duplicates = _duplicate_node_ids(graph)
    if duplicates:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Duplicate node id(s): {', '.join(duplicates)}",
        )

    existing = {
        row.node_key: row
        for row in await db.scalars(select(WorkflowNode).where(WorkflowNode.workflow_id == workflow.id))
    }
    keys = {node.id for node in graph.nodes}

    await db.execute(delete(WorkflowEdge).where(WorkflowEdge.workflow_id == workflow.id))
    await db.execute(delete(WorkflowVariable).where(WorkflowVariable.workflow_id == workflow.id))
    removed = [key for key in existing if key not in keys]
    if removed:
        await db.execute(
            delete(WorkflowNode).where(WorkflowNode.workflow_id == workflow.id, WorkflowNode.node_key.in_(removed))
        )

    row_ids: dict[str, uuid.UUID] = {}
    for node in graph.nodes:
        row = existing.get(node.id)
        if row is None:
            row = WorkflowNode(id=uuid.uuid4(), workflow_id=workflow.id, node_key=node.id)
            db.add(row)
        definition = get_node_definition(node.type)
        row.node_type = node.type
        row.label = node.label or (definition.label if definition else node.type)
        row.position_x = node.position.x
        row.position_y = node.position.y
        row.config_json = node.config
        row_ids[node.id] = row.id

    # Dangling edges stay in graph_json (and /validate flags them) but can't be rows.
    for edge in graph.edges:
        if edge.source in row_ids and edge.target in row_ids:
            db.add(WorkflowEdge(
                workflow_id=workflow.id,
                source_node_id=row_ids[edge.source],
                target_node_id=row_ids[edge.target],
                source_handle=edge.source_handle,
                target_handle=edge.target_handle,
            ))

    for variable in graph.variables:
        db.add(WorkflowVariable(
            workflow_id=workflow.id,
            key=variable.key,
            value=variable.value,
            var_type=VariableType(variable.type),
        ))

    workflow.graph_json = graph.model_dump(mode="json")
    workflow.version += 1
