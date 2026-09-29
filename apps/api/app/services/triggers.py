"""Triggers: workflows that run without a click.

- **Schedule**: a cron expression in a time zone (app.services.schedule). Celery beat ticks
  every minute; the tick finds enabled schedules whose `next_fire_at` has passed and claims
  each one with a compare-and-set on that exact fire time
  (UPDATE ... WHERE next_fire_at = <the time it read>), moving it to the following fire
  time. Only one tick can win that update, and the fire time is also recorded in
  `trigger_events` under a unique key, so a fire time starts at most one run even with
  several beats, overlapping ticks, or redelivered tasks.
- **Email**: the tick claims due mailbox polls the same way and queues one poll task each.
  A poll reads the IMAP folder for unread mail matching the filters that arrived after the
  mailbox position the trigger has seen (UIDs, reset if the folder's UIDVALIDITY changes;
  enabling a trigger starts from "now", so old mail never floods it), and starts one run
  per new email, with the email as the run's input. Each email's Message-ID is a unique
  trigger event, so however often a message is seen it runs once.
- **Webhook**: the workflow's deployment endpoint (app.api.routes.deployment_runs), which
  checks the trigger is on and within the limits below.

Every triggered run goes through the ordinary path (create_execution + the task queue) and
is recorded with its trigger type and `trigger_id`. Safety, per workflow: at most
`max_runs_per_hour` triggered runs start per rolling hour (the rest are skipped, and the
trigger shows why), and a trigger switches itself off after `max_consecutive_failures`
failed runs in a row (app.services.trigger_outcomes).
"""

import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from flowforge_engine import ExecutionServices, GraphError, ProviderError, WorkflowGraph, queue_for_graph, topological_sort
from flowforge_engine.models import IDENTIFIER_PATTERN
from flowforge_engine.providers import MailboxQuery
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import SessionFactory
from app.models.deployment import Deployment
from app.models.enums import ExecutionStatus, ExecutionTrigger, NodeExecutionStatus, TriggerType
from app.models.execution import NodeExecution, WorkflowExecution
from app.models.trigger import TriggerEvent, WorkflowTrigger
from app.models.user import User
from app.models.workflow import Workflow
from app.schemas.trigger import TriggerLastRun, TriggerRead, TriggersRead, TriggerSettings, WebhookInfo
from app.services.credentials import build_execution_services
from app.services.deployments import endpoint_path
from app.services.runs import InvalidWorkflowGraph, create_execution, fail_unqueued
from app.services.schedule import ScheduleError, next_fire_time, normalize_cron, upcoming, zone
from app.services.task_queue import EnqueueFailed, TaskQueue
from app.services.trigger_outcomes import apply_trigger_outcome

logger = logging.getLogger(__name__)

TRIGGERED = (ExecutionTrigger.SCHEDULE, ExecutionTrigger.EMAIL, ExecutionTrigger.WEBHOOK)
_EXECUTION_TRIGGER = {
    TriggerType.SCHEDULE: ExecutionTrigger.SCHEDULE,
    TriggerType.EMAIL: ExecutionTrigger.EMAIL,
    TriggerType.WEBHOOK: ExecutionTrigger.WEBHOOK,
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class TriggerConfigError(ValueError):
    """A trigger config that can't be saved (message is safe to show)."""


# --- config ---------------------------------------------------------------------------------


class ScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cron: str = Field(description="5-field cron, e.g. '30 7 * * *' (07:30 daily) or '*/15 * * * *'.")
    timezone: str = Field(default="UTC", description="IANA time zone the cron is read in, e.g. Asia/Kolkata.")
    inputs: dict[str, Any] = Field(default_factory=dict, description="Run inputs, keyed by Input node name.")

    @field_validator("cron")
    @classmethod
    def _cron(cls, value: str) -> str:
        return normalize_cron(value)

    @field_validator("timezone")
    @classmethod
    def _zone(cls, value: str) -> str:
        zone(value)
        return value.strip()


class EmailTriggerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder: str = Field(default="INBOX", min_length=1, max_length=255)
    from_address: str | None = Field(default=None, max_length=255, description="Only mail whose From contains this.")
    subject: str | None = Field(default=None, max_length=255, description="Only mail whose Subject contains this.")
    unread_only: bool = True
    poll_minutes: int = Field(default=5, ge=1, le=1440, description="How often the mailbox is checked.")
    input_name: str = Field(
        default="email", max_length=100, pattern=IDENTIFIER_PATTERN,
        description="The run input the email is passed as (an Input node with this name, type JSON or text).",
    )
    max_per_poll: int = Field(default=10, ge=1, le=50, description="Emails handled per check; the rest wait for the next.")
    mark_as_read: bool = Field(default=False, description="Flag the emails a check picked up as read.")
    max_body_chars: int = Field(default=5000, ge=0, le=100_000)

    @field_validator("from_address", "subject")
    @classmethod
    def _blank(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @field_validator("poll_minutes")
    @classmethod
    def _min_poll(cls, value: int) -> int:
        return max(value, settings.EMAIL_TRIGGER_MIN_POLL_MINUTES)


class WebhookTriggerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


CONFIG_MODELS: dict[TriggerType, type[BaseModel]] = {
    TriggerType.SCHEDULE: ScheduleConfig,
    TriggerType.EMAIL: EmailTriggerConfig,
    TriggerType.WEBHOOK: WebhookTriggerConfig,
}


def parse_config(trigger_type: TriggerType, config: dict[str, Any]) -> BaseModel:
    try:
        return CONFIG_MODELS[trigger_type].model_validate(config)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'config'}: {str(e['msg']).removeprefix('Value error, ')}" for e in exc.errors()
        )
        raise TriggerConfigError(problems) from None


# --- reading --------------------------------------------------------------------------------


async def get_trigger(db: AsyncSession, workflow_id: uuid.UUID, trigger_type: TriggerType, *, lock: bool = False) -> WorkflowTrigger | None:
    query = select(WorkflowTrigger).where(WorkflowTrigger.workflow_id == workflow_id, WorkflowTrigger.type == trigger_type)
    if lock:
        query = query.with_for_update()
    return await db.scalar(query.execution_options(populate_existing=True))


async def ensure_trigger(db: AsyncSession, workflow_id: uuid.UUID, trigger_type: TriggerType, *, enabled: bool = False) -> WorkflowTrigger:
    """The workflow's trigger of this type, created if missing (race-free), locked."""
    await db.execute(
        insert(WorkflowTrigger)
        .values(id=uuid.uuid4(), workflow_id=workflow_id, type=trigger_type, enabled=enabled, config_json={})
        .on_conflict_do_nothing(index_elements=["workflow_id", "type"])
    )
    trigger = await get_trigger(db, workflow_id, trigger_type, lock=True)
    assert trigger is not None
    return trigger


async def runs_in_last_hour(db: AsyncSession, workflow_id: uuid.UUID, now: datetime) -> int:
    return await db.scalar(
        select(func.count()).select_from(WorkflowExecution).where(
            WorkflowExecution.workflow_id == workflow_id,
            WorkflowExecution.trigger.in_(TRIGGERED),
            WorkflowExecution.created_at > now - timedelta(hours=1),
        )
    ) or 0


def input_node_warning(graph: WorkflowGraph, input_name: str) -> str | None:
    """Why an email trigger's input won't reach the workflow, or None."""
    for node in graph.nodes:
        if node.type != "input":
            continue
        if (node.config.get("name") or node.id) == input_name:
            kind = node.config.get("input_type", "text")
            if kind not in ("json", "text"):
                return f"The Input node '{node.id}' named '{input_name}' has type {kind}; use JSON (or text) to receive the email."
            return None
    return (
        f"No Input node is named '{input_name}', so runs won't see the email. Add an Input node with name "
        f"'{input_name}' and type JSON, then use {{{{<node>.value.subject}}}}, {{{{<node>.value.body_text}}}}, ..."
    )


# --- saving ---------------------------------------------------------------------------------


async def mailbox_position(services: ExecutionServices, folder: str) -> dict[str, Any]:
    """The folder's current end (only mail arriving after it triggers runs). Raises
    TriggerConfigError when the mailbox can't be read."""
    try:
        status = await services.mailbox("gmail").mailbox_status(folder)
    except ProviderError as exc:
        raise TriggerConfigError(f"Can't read the mailbox: {exc}") from None
    return {"folder": folder, "uidvalidity": status["uidvalidity"], "last_uid": max(0, int(status["uidnext"]) - 1)}


async def save_trigger(
    db: AsyncSession,
    workflow: Workflow,
    trigger_type: TriggerType,
    *,
    enabled: bool,
    config: dict[str, Any] | None,
    services: ExecutionServices,
    now: datetime | None = None,
) -> WorkflowTrigger:
    """Create or update the workflow's trigger. Enabling one clears an automatic disable
    and its failure count; enabling (or re-pointing) an email trigger reads the mailbox's
    current position first, so only mail arriving from now on triggers runs."""
    now = now or utcnow()
    trigger = await ensure_trigger(db, workflow.id, trigger_type)
    previous = dict(trigger.config_json or {})
    parsed = parse_config(trigger_type, config if config is not None else previous)
    new_config = parsed.model_dump(mode="json")
    turning_on = enabled and not trigger.enabled

    if trigger_type is TriggerType.SCHEDULE:
        assert isinstance(parsed, ScheduleConfig)
        if enabled:
            unchanged = previous.get("cron") == parsed.cron and previous.get("timezone") == parsed.timezone
            if turning_on or not unchanged or trigger.next_fire_at is None:
                trigger.next_fire_at = next_fire_time(parsed.cron, parsed.timezone, now)
        else:
            trigger.next_fire_at = None
    elif trigger_type is TriggerType.EMAIL:
        assert isinstance(parsed, EmailTriggerConfig)
        if enabled:
            state = trigger.state_json or {}
            if turning_on or state.get("folder") != parsed.folder or "last_uid" not in state:
                trigger.state_json = await mailbox_position(services, parsed.folder)
            if turning_on or trigger.next_fire_at is None:
                trigger.next_fire_at = now  # the next tick checks
        else:
            trigger.next_fire_at = None

    if turning_on:
        trigger.consecutive_failures = 0
        trigger.auto_disabled_at = None
        trigger.disabled_reason = None
        trigger.last_error = None
        trigger.last_error_at = None
    trigger.enabled = enabled
    trigger.config_json = new_config
    await db.commit()
    await db.refresh(trigger)
    logger.info("trigger saved", extra={
        "workflow_id": str(workflow.id), "type": trigger_type.value, "enabled": enabled,
        "next_fire_at": trigger.next_fire_at.isoformat() if trigger.next_fire_at else None,
    })
    return trigger


# --- starting runs --------------------------------------------------------------------------


@dataclass
class FireResult:
    """What one trigger occurrence did."""

    outcome: str  # started | duplicate | skipped | rejected | missed | enqueue_failed
    trigger_id: uuid.UUID
    event_key: str
    execution_id: uuid.UUID | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome, "trigger_id": str(self.trigger_id), "event_key": self.event_key,
            "execution_id": str(self.execution_id) if self.execution_id else None, "detail": self.detail,
        }


def _note(trigger: WorkflowTrigger, message: str, now: datetime) -> None:
    trigger.last_error, trigger.last_error_at = message, now


async def limit_problem(db: AsyncSession, workflow: Workflow, now: datetime) -> str | None:
    """Why another triggered run may not start now (the hourly cap), or None. Locks the
    workflow row until the caller's commit, so concurrent triggers count each other."""
    locked = await db.scalar(
        select(Workflow).where(Workflow.id == workflow.id).with_for_update().execution_options(populate_existing=True)
    )
    assert locked is not None
    count = await runs_in_last_hour(db, workflow.id, now)
    if count >= locked.max_runs_per_hour:
        return (
            f"Skipped: this workflow already started {count} triggered runs in the last hour "
            f"(the limit is {locked.max_runs_per_hour}; change it in the Triggers panel)"
        )
    return None


async def _record_event(db: AsyncSession, trigger_id: uuid.UUID, event_key: str, outcome: str, detail: str | None = None) -> uuid.UUID | None:
    """Insert the occurrence; None if this trigger already acted on `event_key`."""
    return await db.scalar(
        insert(TriggerEvent)
        .values(id=uuid.uuid4(), trigger_id=trigger_id, event_key=event_key[:998], outcome=outcome, detail=detail)
        .on_conflict_do_nothing(index_elements=["trigger_id", "event_key"])
        .returning(TriggerEvent.id)
    )


async def record_rejected_run(
    db: AsyncSession,
    workflow: Workflow,
    trigger: WorkflowTrigger,
    inputs: dict[str, Any],
    error: str,
    now: datetime,
) -> uuid.UUID:
    """A failed execution for a triggered run that couldn't start (the graph didn't
    validate), so it shows up in the history with its trigger, and counts as a failure."""
    execution = WorkflowExecution(
        id=uuid.uuid4(), workflow_id=workflow.id, status=ExecutionStatus.FAILED,
        trigger=_EXECUTION_TRIGGER[trigger.type], trigger_id=trigger.id, inputs_json=inputs,
        graph_json=workflow.graph_json, started_at=now, finished_at=now, created_at=now, error_message=error,
    )
    db.add(execution)
    graph = WorkflowGraph.model_validate(workflow.graph_json)
    by_id = {node.id: node for node in graph.nodes}
    try:
        order = topological_sort(graph.nodes, graph.edges)
    except GraphError:
        order = list(by_id)
    for position, node_id in enumerate(order):
        node = by_id[node_id]
        db.add(NodeExecution(
            execution_id=execution.id, node_key=node.id, node_type=node.type, node_label=node.label or node.type,
            position=position, status=NodeExecutionStatus.SKIPPED, error_message="Not run: the workflow failed validation",
        ))
    await db.flush()
    await apply_trigger_outcome(db, trigger.id, ExecutionStatus.FAILED, error)
    return execution.id


async def start_triggered_run(
    db: AsyncSession,
    workflow: Workflow,
    trigger: WorkflowTrigger,
    *,
    event_key: str,
    inputs: dict[str, Any],
    services: ExecutionServices,
    task_queue: TaskQueue,
    now: datetime,
) -> FireResult:
    """Start one run for one trigger occurrence, at most once per `event_key`.

    Runs in the caller's transaction (which may hold the trigger's claim) and commits it.
    """
    if await _record_event(db, trigger.id, event_key, "started") is None:
        await db.commit()
        return FireResult("duplicate", trigger.id, event_key, detail="Already handled")
    if problem := await limit_problem(db, workflow, now):
        _note(trigger, problem, now)
        await _set_outcome(db, trigger.id, event_key, "skipped", problem)
        await db.commit()
        logger.warning("triggered run skipped", extra={"trigger_id": str(trigger.id), "reason": problem})
        return FireResult("skipped", trigger.id, event_key, detail=problem)

    graph = WorkflowGraph.model_validate(workflow.graph_json)
    queue = queue_for_graph(graph)
    try:
        execution = await create_execution(
            db, workflow, None, inputs, services, queue=queue, graph=graph,
            trigger=_EXECUTION_TRIGGER[trigger.type], trigger_id=trigger.id, created_at=now,
        )
    except InvalidWorkflowGraph as exc:
        error = "The workflow can't run: " + "; ".join(issue.message for issue in exc.issues[:5])
        execution_id = await record_rejected_run(db, workflow, trigger, inputs, error, now)
        await _set_outcome(db, trigger.id, event_key, "rejected", error, execution_id)
        await db.commit()
        logger.warning("triggered run rejected", extra={"trigger_id": str(trigger.id), "error": error})
        return FireResult("rejected", trigger.id, event_key, execution_id, error)

    await _set_outcome(db, trigger.id, event_key, "started", None, execution.id)
    await db.commit()
    try:
        await task_queue.enqueue(execution.id, queue)
    except EnqueueFailed as exc:
        error = await fail_unqueued(db, execution.id, exc)
        return FireResult("enqueue_failed", trigger.id, event_key, execution.id, error)
    logger.info("triggered run started", extra={
        "trigger_id": str(trigger.id), "type": trigger.type.value, "execution_id": str(execution.id), "event_key": event_key,
    })
    return FireResult("started", trigger.id, event_key, execution.id)


async def _set_outcome(
    db: AsyncSession, trigger_id: uuid.UUID, event_key: str, outcome: str, detail: str | None,
    execution_id: uuid.UUID | None = None,
) -> None:
    await db.execute(
        update(TriggerEvent)
        .where(TriggerEvent.trigger_id == trigger_id, TriggerEvent.event_key == event_key[:998])
        .values(outcome=outcome, detail=detail, execution_id=execution_id)
        .execution_options(synchronize_session=False)
    )


async def _owner_services(db: AsyncSession, workflow: Workflow) -> ExecutionServices:
    owner = await db.get(User, workflow.owner_id)
    assert owner is not None
    return await build_execution_services(db, owner)


# --- schedules ------------------------------------------------------------------------------


async def due_triggers(db: AsyncSession, trigger_type: TriggerType, now: datetime, limit: int = 200) -> list[uuid.UUID]:
    rows = await db.scalars(
        select(WorkflowTrigger.id)
        .where(WorkflowTrigger.type == trigger_type, WorkflowTrigger.enabled.is_(True), WorkflowTrigger.next_fire_at <= now)
        .order_by(WorkflowTrigger.next_fire_at)
        .limit(limit)
    )
    return list(rows)


async def fire_schedule(
    session_factory: SessionFactory, task_queue: TaskQueue, trigger_id: uuid.UUID, *, now: datetime | None = None
) -> FireResult | None:
    """Fire one due schedule, if this call wins its fire time (None: not due, or taken)."""
    now = now or utcnow()
    async with session_factory() as db:
        trigger = await db.get(WorkflowTrigger, trigger_id, populate_existing=True)
        if trigger is None or not trigger.enabled or trigger.next_fire_at is None or trigger.next_fire_at > now:
            return None
        fire_at = trigger.next_fire_at
        try:
            config = ScheduleConfig.model_validate(trigger.config_json)
            following = next_fire_time(config.cron, config.timezone, max(now, fire_at))
        except (ValidationError, ScheduleError) as exc:
            trigger.enabled, trigger.next_fire_at = False, None
            trigger.disabled_reason = f"Disabled: the schedule is invalid ({exc})"
            await db.commit()
            return None
        # The claim: only the caller that still sees this exact fire time moves it on.
        claimed = await db.scalar(
            update(WorkflowTrigger)
            .where(WorkflowTrigger.id == trigger_id, WorkflowTrigger.enabled.is_(True), WorkflowTrigger.next_fire_at == fire_at)
            .values(next_fire_at=following, last_fired_at=now)
            .returning(WorkflowTrigger.id)
            .execution_options(synchronize_session=False)
        )
        if claimed is None:
            return None  # another tick took this fire time
        event_key = f"schedule:{fire_at.astimezone(UTC).isoformat()}"
        late = (now - fire_at).total_seconds()
        if late > settings.TRIGGER_MISFIRE_GRACE_SECONDS:
            message = (
                f"Missed the run due at {fire_at.isoformat()} ({late / 60:.0f} min late: were the beat or the workers down?); "
                f"the next is at {following.isoformat()}"
            )
            if await _record_event(db, trigger_id, event_key, "missed", message) is not None:
                _note(trigger, message, now)
            await db.commit()
            logger.warning("scheduled run missed", extra={"trigger_id": str(trigger_id), "due": fire_at.isoformat()})
            return FireResult("missed", trigger_id, event_key, detail=message)
        workflow = await db.get(Workflow, trigger.workflow_id)
        assert workflow is not None
        services = await _owner_services(db, workflow)
        return await start_triggered_run(
            db, workflow, trigger, event_key=event_key, inputs=dict(config.inputs),
            services=services, task_queue=task_queue, now=now,
        )


async def fire_due_schedules(session_factory: SessionFactory, task_queue: TaskQueue, *, now: datetime | None = None) -> list[FireResult]:
    now = now or utcnow()
    async with session_factory() as db:
        due = await due_triggers(db, TriggerType.SCHEDULE, now)
    results = []
    for trigger_id in due:
        try:
            result = await fire_schedule(session_factory, task_queue, trigger_id, now=now)
        except Exception:  # one broken trigger mustn't stop the others
            logger.exception("firing a schedule failed", extra={"trigger_id": str(trigger_id)})
            continue
        if result is not None:
            results.append(result)
    return results


# --- email ----------------------------------------------------------------------------------


async def claim_email_polls(session_factory: SessionFactory, *, now: datetime | None = None) -> list[uuid.UUID]:
    """Claim every due email trigger's next poll (compare-and-set on next_fire_at)."""
    now = now or utcnow()
    claimed: list[uuid.UUID] = []
    async with session_factory() as db:
        for trigger_id in await due_triggers(db, TriggerType.EMAIL, now):
            trigger = await db.get(WorkflowTrigger, trigger_id, populate_existing=True)
            if trigger is None or trigger.next_fire_at is None:
                continue
            minutes = max(int((trigger.config_json or {}).get("poll_minutes") or 5), settings.EMAIL_TRIGGER_MIN_POLL_MINUTES)
            # On the minute: ticks run at :00, so "now + N minutes" would land a few ms
            # after the tick N minutes on and wait one more minute.
            next_poll = now.replace(second=0, microsecond=0) + timedelta(minutes=minutes)
            won = await db.scalar(
                update(WorkflowTrigger)
                .where(WorkflowTrigger.id == trigger_id, WorkflowTrigger.enabled.is_(True),
                       WorkflowTrigger.next_fire_at == trigger.next_fire_at)
                .values(next_fire_at=next_poll)
                .returning(WorkflowTrigger.id)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            if won is not None:
                claimed.append(trigger_id)
    return claimed


def email_input(email: dict[str, Any], input_type: str | None) -> Any:
    """The email as a run input: the message as JSON, or as readable text for a text input."""
    if input_type != "text":
        return email
    attachments = ", ".join(a.get("filename") or "?" for a in email.get("attachments") or [])
    lines = [
        f"From: {email.get('from', '')}",
        f"To: {', '.join(email.get('to') or [])}",
        f"Date: {email.get('date') or ''}",
        f"Subject: {email.get('subject', '')}",
    ]
    if attachments:
        lines.append(f"Attachments: {attachments}")
    return "\n".join(lines) + "\n\n" + (email.get("body_text") or "")


def _input_type(graph: WorkflowGraph, input_name: str) -> str | None:
    for node in graph.nodes:
        if node.type == "input" and (node.config.get("name") or node.id) == input_name:
            return str(node.config.get("input_type", "text"))
    return None


def email_event_key(email: dict[str, Any], uidvalidity: Any) -> str:
    """What makes an email "the same email": its Message-ID (the folder's UID only when a
    message has none)."""
    message_id = str(email.get("message_id") or "").strip()
    return f"email:{message_id}" if message_id else f"email:uid:{uidvalidity}:{email.get('uid')}"


def _uid(email: dict[str, Any]) -> int | None:
    try:
        return int(email.get("uid"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None  # the mock mailbox has no numeric UIDs: Message-IDs still de-duplicate


@dataclass
class PollResult:
    trigger_id: uuid.UUID
    polled: bool
    found: int = 0
    runs: list[FireResult] = field(default_factory=list)
    reset: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "trigger_id": str(self.trigger_id), "polled": self.polled, "found": self.found, "reset": self.reset,
            "error": self.error, "runs": [run.as_dict() for run in self.runs],
        }


@contextlib.asynccontextmanager
async def _poll_lock(redis: Redis | None, trigger_id: uuid.UUID):
    """One poll per trigger at a time (a scheduled poll and "Check now" can overlap)."""
    if redis is None:
        yield True
        return
    key = f"flowforge:email-poll:{trigger_id}"
    token = uuid.uuid4().hex
    try:
        acquired = bool(await redis.set(key, token, nx=True, ex=settings.EMAIL_POLL_LOCK_SECONDS))
    except RedisError as exc:
        logger.warning("can't take the email poll lock; polling anyway", extra={"trigger_id": str(trigger_id), "error": str(exc)})
        yield True
        return
    try:
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(RedisError):
                if await redis.get(key) in (token, token.encode()):
                    await redis.delete(key)


async def poll_email_trigger(
    session_factory: SessionFactory,
    task_queue: TaskQueue,
    trigger_id: uuid.UUID,
    *,
    redis: Redis | None = None,
    now: datetime | None = None,
) -> PollResult:
    """Check one email trigger's mailbox and start a run per new matching email."""
    now = now or utcnow()
    async with _poll_lock(redis, trigger_id) as acquired:
        if not acquired:
            return PollResult(trigger_id, polled=False, error="Another check of this mailbox is running")
        async with session_factory() as db:
            trigger = await db.get(WorkflowTrigger, trigger_id, populate_existing=True)
            if trigger is None or trigger.type is not TriggerType.EMAIL or not trigger.enabled:
                return PollResult(trigger_id, polled=False, error="The email trigger is off")
            workflow = await db.get(Workflow, trigger.workflow_id)
            assert workflow is not None
            config = EmailTriggerConfig.model_validate(trigger.config_json)
            services = await _owner_services(db, workflow)
            state = dict(trigger.state_json or {})
        result = PollResult(trigger_id, polled=True)
        try:
            mailbox = services.mailbox("gmail")
            status = await mailbox.mailbox_status(config.folder)
            if state.get("folder") != config.folder or state.get("uidvalidity") != status["uidvalidity"] or "last_uid" not in state:
                # A new folder, or the server renumbered it: start from its current end.
                state = {"folder": config.folder, "uidvalidity": status["uidvalidity"], "last_uid": max(0, int(status["uidnext"]) - 1)}
                result.reset = True
                emails: list[dict[str, Any]] = []
            else:
                query = MailboxQuery(
                    folder=config.folder, from_address=config.from_address, subject=config.subject,
                    unread_only=config.unread_only, max_results=config.max_per_poll, mark_as_read=config.mark_as_read,
                    include_body=True, max_body_chars=config.max_body_chars,
                    uid_after=int(state["last_uid"]), oldest_first=True,
                )
                emails = await mailbox.fetch_emails(query)
        except ProviderError as exc:
            result.error = f"Couldn't check the mailbox: {exc}"
            async with session_factory() as db:
                trigger = await db.get(WorkflowTrigger, trigger_id, populate_existing=True)
                if trigger is not None:
                    _note(trigger, result.error, now)
                    await db.commit()
            logger.warning("email poll failed", extra={"trigger_id": str(trigger_id), "error": str(exc)})
            return result

        emails.sort(key=lambda e: (_uid(e) is None, _uid(e) or 0))
        result.found = len(emails)
        input_type = _input_type(WorkflowGraph.model_validate(workflow.graph_json), config.input_name)
        for email in emails:
            key = email_event_key(email, state.get("uidvalidity"))
            payload = {**email, "folder": config.folder}
            async with session_factory() as db:
                trigger = await db.get(WorkflowTrigger, trigger_id, populate_existing=True)
                current = await db.get(Workflow, workflow.id, populate_existing=True)
                if trigger is None or current is None or not trigger.enabled:
                    break  # switched off (e.g. by the failure limit) while we were polling
                fired = await start_triggered_run(
                    db, current, trigger, event_key=key, inputs={config.input_name: email_input(payload, input_type)},
                    services=services, task_queue=task_queue, now=utcnow(),
                )
            result.runs.append(fired)

        uids = [uid for uid in map(_uid, emails) if uid is not None]
        last_uid = max([int(state["last_uid"]), *uids])
        if len(emails) < config.max_per_poll:
            last_uid = max(last_uid, int(status["uidnext"]) - 1)  # nothing else matched up to here
        state.update(last_uid=last_uid, uidvalidity=status["uidvalidity"], last_poll_at=now.isoformat())
        async with session_factory() as db:
            trigger = await db.get(WorkflowTrigger, trigger_id, populate_existing=True)
            if trigger is not None:
                trigger.state_json = state
                if trigger.last_error and trigger.last_error.startswith("Couldn't check the mailbox"):
                    trigger.last_error = trigger.last_error_at = None
                await db.commit()
        logger.info("email trigger polled", extra={
            "trigger_id": str(trigger_id), "found": result.found,
            "started": sum(1 for r in result.runs if r.outcome == "started"), "reset": result.reset,
        })
        return result


# --- the Triggers panel ---------------------------------------------------------------------


async def _last_run(db: AsyncSession, trigger_id: uuid.UUID) -> TriggerLastRun | None:
    row = await db.scalar(
        select(WorkflowExecution)
        .where(WorkflowExecution.trigger_id == trigger_id)
        .order_by(WorkflowExecution.created_at.desc(), WorkflowExecution.id)
        .limit(1)
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    return TriggerLastRun(
        execution_id=row.id, status=row.status, created_at=row.created_at,
        finished_at=row.finished_at, error_message=row.error_message,
    )


async def list_triggers(db: AsyncSession, workflow: Workflow, *, now: datetime | None = None) -> TriggersRead:
    """All three trigger types for the panel (unsaved ones with their defaults)."""
    now = now or utcnow()
    rows = {
        row.type: row
        for row in await db.scalars(
            select(WorkflowTrigger).where(WorkflowTrigger.workflow_id == workflow.id).execution_options(populate_existing=True)
        )
    }
    graph = WorkflowGraph.model_validate(workflow.graph_json)
    deployment = await db.scalar(select(Deployment).where(Deployment.workflow_id == workflow.id))
    views = []
    for trigger_type in (TriggerType.SCHEDULE, TriggerType.EMAIL, TriggerType.WEBHOOK):
        row = rows.get(trigger_type)
        config = dict(row.config_json) if row and row.config_json else {}
        warnings: list[str] = []
        view = TriggerRead(
            type=trigger_type, id=row.id if row else None, configured=row is not None,
            enabled=bool(row and row.enabled), config=config, next_run_at=row.next_fire_at if row and row.enabled else None,
        )
        if row is not None:
            view.last_fired_at = row.last_fired_at
            view.last_run = await _last_run(db, row.id)
            view.consecutive_failures = row.consecutive_failures
            view.auto_disabled_at, view.disabled_reason = row.auto_disabled_at, row.disabled_reason
            view.last_error, view.last_error_at = row.last_error, row.last_error_at
        if trigger_type is TriggerType.SCHEDULE:
            if not config:
                view.config = {"cron": "0 8 * * *", "timezone": "UTC", "inputs": {}}
            elif row and row.enabled:
                with contextlib.suppress(ScheduleError, KeyError):
                    view.upcoming = upcoming(config["cron"], config.get("timezone", "UTC"), now, 3)
        elif trigger_type is TriggerType.EMAIL:
            defaults = EmailTriggerConfig().model_dump(mode="json")
            view.config = {**defaults, **config}
            if row is not None and (warning := input_node_warning(graph, view.config["input_name"])):
                warnings.append(warning)
            if row and row.state_json:
                view.mailbox = {k: row.state_json.get(k) for k in ("folder", "last_uid", "last_poll_at")}
        else:
            # The deployment endpoint is the webhook; it's on unless switched off here.
            view.enabled = row.enabled if row is not None else deployment is not None
            view.configured = row is not None or deployment is not None
            if deployment is None:
                warnings.append("Deploy the workflow (Deploy in the top bar) to get its webhook URL and API key.")
            else:
                behind = deployment.workflow_version < workflow.version
                view.webhook = WebhookInfo(
                    deployment_id=deployment.id, endpoint=endpoint_path(deployment.id),
                    api_key_prefix=deployment.api_key_prefix, deployed_version=deployment.version, behind=behind,
                )
                if behind:
                    warnings.append("The workflow has changed since it was deployed; redeploy to publish the edits.")
        view.warnings = warnings
        views.append(view)
    return TriggersRead(
        workflow_id=workflow.id,
        settings=TriggerSettings(
            max_runs_per_hour=workflow.max_runs_per_hour, max_consecutive_failures=workflow.max_consecutive_failures
        ),
        runs_last_hour=await runs_in_last_hour(db, workflow.id, now),
        triggers=views,
    )
