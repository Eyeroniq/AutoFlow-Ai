"""What a finished triggered run means for its trigger: the consecutive-failure count, and
switching the trigger off once it reaches the workflow's limit.

Called from app.services.runs.finish_execution (every way a run ends goes through it:
the worker, a stop, crash recovery, a failed enqueue) inside the same transaction, and
for runs rejected before they started (the workflow didn't validate).
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ExecutionStatus
from app.models.trigger import WorkflowTrigger
from app.models.workflow import Workflow

logger = logging.getLogger(__name__)


async def apply_trigger_outcome(
    db: AsyncSession, trigger_id: uuid.UUID, status: ExecutionStatus, error: str | None
) -> None:
    """Success resets the count; a failure adds one and may disable the trigger; a stop by
    the user changes nothing. Doesn't commit (the caller's transaction does)."""
    if status not in (ExecutionStatus.SUCCESS, ExecutionStatus.FAILED):
        return
    trigger = await db.scalar(
        select(WorkflowTrigger).where(WorkflowTrigger.id == trigger_id).with_for_update().execution_options(populate_existing=True)
    )
    if trigger is None:
        return
    if status is ExecutionStatus.SUCCESS:
        trigger.consecutive_failures = 0
        return
    trigger.consecutive_failures += 1
    limit = await db.scalar(select(Workflow.max_consecutive_failures).where(Workflow.id == trigger.workflow_id))
    if limit and trigger.enabled and trigger.consecutive_failures >= limit:
        now = datetime.now(UTC)
        trigger.enabled = False
        trigger.next_fire_at = None
        trigger.auto_disabled_at = now
        detail = (error or "no error message").strip()
        if len(detail) > 500:
            detail = detail[:499] + "…"
        trigger.disabled_reason = (
            f"Disabled after {trigger.consecutive_failures} consecutive failed runs. Last error: {detail}. "
            "Fix the workflow, then turn the trigger back on."
        )
        logger.warning(
            "trigger disabled after consecutive failures",
            extra={"trigger_id": str(trigger.id), "workflow_id": str(trigger.workflow_id),
                   "type": trigger.type.value, "failures": trigger.consecutive_failures},
        )
