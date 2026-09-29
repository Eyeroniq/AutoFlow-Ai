"""Database-backed node state (flowforge_engine.state): what a node remembers between runs.

Each run that saves state writes a new row tied to its execution; `load` reads the newest
row whose execution *succeeded*. So an RSS node's "since last run" only moves forward when
the whole run went through: if a later node failed, the next run sees the same entries.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select

from app.db.session import SessionFactory
from app.models.enums import ExecutionStatus
from app.models.execution import WorkflowExecution
from app.models.trigger import NodeState

# At most this many rows per workflow node (runs that failed after saving state).
MAX_ROWS = 20


class DbNodeStateStore:
    def __init__(self, session_factory: SessionFactory, workflow_id: uuid.UUID, execution_id: uuid.UUID | None):
        """`execution_id` None: read-only (a single-node test run must not move the state)."""
        self._sessions = session_factory
        self._workflow_id = workflow_id
        self._execution_id = execution_id

    async def load(self, node_id: str) -> dict[str, Any] | None:
        async with self._sessions() as db:
            return await db.scalar(
                select(NodeState.state_json)
                .join(WorkflowExecution, WorkflowExecution.id == NodeState.execution_id)
                .where(
                    NodeState.workflow_id == self._workflow_id,
                    NodeState.node_key == node_id,
                    WorkflowExecution.status == ExecutionStatus.SUCCESS,
                )
                .order_by(NodeState.created_at.desc())
                .limit(1)
            )

    async def save(self, node_id: str, state: dict[str, Any]) -> None:
        if self._execution_id is None:
            return
        async with self._sessions() as db:
            db.add(NodeState(
                workflow_id=self._workflow_id, node_key=node_id, execution_id=self._execution_id,
                state_json=state, created_at=datetime.now(UTC),
            ))
            await db.flush()
            this_node = (NodeState.workflow_id == self._workflow_id, NodeState.node_key == node_id)
            # Rows older than the newest successful one can never be read again...
            newest_ok = (
                select(NodeState.created_at)
                .join(WorkflowExecution, WorkflowExecution.id == NodeState.execution_id)
                .where(*this_node, WorkflowExecution.status == ExecutionStatus.SUCCESS)
                .order_by(NodeState.created_at.desc())
                .limit(1)
                .scalar_subquery()
            )
            await db.execute(
                delete(NodeState).where(*this_node, NodeState.created_at < newest_ok)
                .execution_options(synchronize_session=False)
            )
            # ...and a workflow that keeps failing can't pile up rows forever.
            keep = select(NodeState.id).where(*this_node).order_by(NodeState.created_at.desc()).limit(MAX_ROWS)
            await db.execute(
                delete(NodeState).where(*this_node, NodeState.id.not_in(keep))
                .execution_options(synchronize_session=False)
            )
            await db.commit()
