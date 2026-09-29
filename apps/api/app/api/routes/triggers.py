"""The Triggers panel: schedule, email, and webhook triggers per workflow (JWT, owner only)."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from flowforge_engine import ExecutionServices

from app.api.deps import CurrentUser, DbSession, SessionFactoryDep, TaskQueueDep
from app.core.redis import get_redis
from app.models.enums import TriggerType
from app.schemas.trigger import SchedulePreview, SchedulePreviewRequest, TriggersRead, TriggerSettings, TriggerUpdate
from app.services.providers import get_execution_services
from app.services.schedule import ScheduleError, is_interval, normalize_cron, upcoming
from app.services.triggers import TriggerConfigError, get_trigger, list_triggers, poll_email_trigger, save_trigger
from app.services.workflows import get_owned_workflow

router = APIRouter(tags=["triggers"])

Services = Annotated[ExecutionServices, Depends(get_execution_services)]
_NOT_FOUND = {404: {"description": "Workflow not found (or not yours)"}}


@router.get(
    "/workflows/{workflow_id}/triggers",
    response_model=TriggersRead,
    summary="The workflow's triggers, limits, and recent activity",
    description=(
        "Always lists all three types (schedule, email, webhook); `configured` says which were saved. Each has "
        "`enabled`, the next fire time or mailbox check (`next_run_at`), the last run it started, its "
        "consecutive-failure count, and, if the failure limit switched it off, `auto_disabled_at` and "
        "`disabled_reason`. The webhook is the workflow's deployment endpoint."
    ),
    responses=_NOT_FOUND,
)
async def get_triggers(workflow_id: uuid.UUID, db: DbSession, user: CurrentUser) -> TriggersRead:
    workflow = await get_owned_workflow(db, workflow_id, user)
    return await list_triggers(db, workflow)


@router.put(
    "/workflows/{workflow_id}/triggers/{trigger_type}",
    response_model=TriggersRead,
    summary="Save and switch on or off one trigger",
    description=(
        "**schedule** `{cron, timezone, inputs}`: a 5-field cron (or @daily, @hourly, ...) read in an IANA time "
        "zone. **email** `{folder, from_address, subject, unread_only, poll_minutes, input_name, ...}`: polls the "
        "Gmail account over IMAP; enabling it records the mailbox's current position, so only mail arriving "
        "afterwards starts runs (the mailbox is read right away, and a failure is a 422). **webhook** `{}`: "
        "switches the deployment endpoint on or off. Enabling a trigger clears an automatic disable."
    ),
    responses={**_NOT_FOUND, 422: {"description": "Invalid config, or the mailbox couldn't be read"}},
)
async def put_trigger(
    workflow_id: uuid.UUID, trigger_type: TriggerType, body: TriggerUpdate, db: DbSession, user: CurrentUser, services: Services,
) -> TriggersRead:
    workflow = await get_owned_workflow(db, workflow_id, user)
    try:
        await save_trigger(db, workflow, trigger_type, enabled=body.enabled, config=body.config, services=services)
    except (TriggerConfigError, ScheduleError) as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from None
    return await list_triggers(db, workflow)


@router.put(
    "/workflows/{workflow_id}/trigger-settings",
    response_model=TriggersRead,
    summary="Set the workflow's trigger limits",
    description="`max_runs_per_hour` caps triggered runs per rolling hour; `max_consecutive_failures` (0 = never) switches a trigger off.",
    responses=_NOT_FOUND,
)
async def put_trigger_settings(workflow_id: uuid.UUID, body: TriggerSettings, db: DbSession, user: CurrentUser) -> TriggersRead:
    workflow = await get_owned_workflow(db, workflow_id, user)
    workflow.max_runs_per_hour = body.max_runs_per_hour
    workflow.max_consecutive_failures = body.max_consecutive_failures
    await db.commit()
    await db.refresh(workflow)
    return await list_triggers(db, workflow)


@router.post(
    "/workflows/{workflow_id}/triggers/email/check",
    summary="Check the email trigger's mailbox now",
    description=(
        "Runs one poll right away (the same one the scheduler runs every `poll_minutes`) and starts a run for each "
        "new matching email. Emails already handled are never run again: each Message-ID starts at most one run."
    ),
    responses={**_NOT_FOUND, 409: {"description": "The email trigger is off"}},
)
async def check_mailbox(
    workflow_id: uuid.UUID, db: DbSession, user: CurrentUser, session_factory: SessionFactoryDep, task_queue: TaskQueueDep,
) -> dict[str, Any]:
    workflow = await get_owned_workflow(db, workflow_id, user)
    trigger = await get_trigger(db, workflow.id, TriggerType.EMAIL)
    if trigger is None or not trigger.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Turn the email trigger on first")
    result = await poll_email_trigger(session_factory, task_queue, trigger.id, redis=get_redis())
    return result.as_dict()


@router.post(
    "/triggers/schedule-preview",
    response_model=SchedulePreview,
    summary="Check a cron expression and list its next fire times",
    description="For the schedule editor: always 200; `valid` and `error` say whether it would be accepted.",
)
async def schedule_preview(body: SchedulePreviewRequest, _: CurrentUser) -> SchedulePreview:
    try:
        cron = normalize_cron(body.cron)
        times = upcoming(cron, body.timezone, datetime.now(UTC), body.count)
    except ScheduleError as exc:
        return SchedulePreview(valid=False, error=str(exc))
    return SchedulePreview(valid=True, cron=cron, interval=is_interval(cron), next=times)
