"""Triggers: schedule math (time zones, DST), firing exactly once per fire time (including a
real race between two database connections), misfires, the hourly cap, auto-disable after
consecutive failures, email polling with Message-ID de-duplication, and the webhook trigger.

Runs execute in-process (app.services.runs.run_execution) against the test database; the
mailbox is an in-memory fake with real UID semantics.
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from flowforge_engine import ExecutionServices, ProviderSettings
from flowforge_engine.errors import ProviderError
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.main import app
from app.models.enums import ExecutionStatus, TriggerType
from app.models.execution import WorkflowExecution
from app.models.trigger import TriggerEvent, WorkflowTrigger
from app.models.user import User
from app.models.workflow import Workflow
from app.services import triggers as trigger_service
from app.services.providers import get_execution_services
from app.services.runs import run_execution
from app.services.schedule import ScheduleError, is_interval, next_fire_time, normalize_cron, upcoming
from app.services.triggers import (
    claim_email_polls,
    email_event_key,
    fire_due_schedules,
    fire_schedule,
    poll_email_trigger,
)
from tests.support import create_workflow
from tests.test_deployments import call, deploy


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


# --- schedule math ---------------------------------------------------------------------------


def test_next_fire_time_in_a_time_zone():
    # 07:30 in Kolkata (UTC+5:30) is 02:00 UTC.
    assert next_fire_time("30 7 * * *", "Asia/Kolkata", at("2026-09-28T12:00:00+00:00")) == at("2026-09-29T02:00:00+00:00")
    # Strictly after: a fire time equal to `after` is not returned again.
    assert next_fire_time("30 7 * * *", "Asia/Kolkata", at("2026-09-29T02:00:00+00:00")) == at("2026-09-30T02:00:00+00:00")
    assert next_fire_time("@daily", "UTC", at("2026-09-28T23:59:00+00:00")) == at("2026-09-29T00:00:00+00:00")
    assert normalize_cron("  */5   *  * * * ") == "*/5 * * * *" and normalize_cron("@hourly") == "0 * * * *"


def test_fixed_times_fire_once_when_clocks_fall_back():
    # US DST ends 2026-11-01: 01:30 happens at 05:30 UTC (EDT) and again at 06:30 UTC (EST).
    times = upcoming("30 1 * * *", "America/New_York", at("2026-10-31T12:00:00+00:00"), 3)
    assert times == [at("2026-11-01T05:30:00+00:00"), at("2026-11-02T06:30:00+00:00"), at("2026-11-03T06:30:00+00:00")]
    # Even starting inside the repeated hour, the second 01:30 doesn't fire.
    assert next_fire_time("30 1 * * *", "America/New_York", at("2026-11-01T06:10:00+00:00")) == at("2026-11-02T06:30:00+00:00")
    # 08:00 keeps its wall-clock time: 12:00 UTC in EDT, 13:00 UTC in EST.
    assert upcoming("0 8 * * *", "America/New_York", at("2026-10-31T00:00:00+00:00"), 2) == [
        at("2026-10-31T12:00:00+00:00"), at("2026-11-01T13:00:00+00:00"),
    ]


def test_intervals_follow_real_time_across_dst():
    # Every 30 minutes keeps going through the repeated hour (01:00 and 01:30 EDT, then EST).
    times = upcoming("*/30 * * * *", "America/New_York", at("2026-11-01T04:40:00+00:00"), 5)
    assert times == [at(f"2026-11-01T{t}:00+00:00") for t in ("05:00", "05:30", "06:00", "06:30", "07:00")]
    assert is_interval("0 * * * *") and not is_interval("0 9-17 * * 1-5")


def test_a_time_skipped_by_spring_forward_runs_after_the_jump():
    # US DST starts 2026-03-08: 02:00-03:00 doesn't exist; 02:30 runs at 03:30 EDT (07:30 UTC).
    times = upcoming("30 2 * * *", "America/New_York", at("2026-03-07T12:00:00+00:00"), 2)
    assert times == [at("2026-03-08T07:30:00+00:00"), at("2026-03-09T06:30:00+00:00")]


@pytest.mark.parametrize(
    ("cron", "zone_name", "message"),
    [
        ("* * * *", "UTC", "5 fields"),
        ("0 0 * * * *", "UTC", "5 fields"),
        ("61 * * * *", "UTC", "not a valid cron"),
        ("0 8 * * *", "Mars/Olympus", "unknown time zone"),
        ("0 0 30 2 *", "UTC", "never fires"),
    ],
)
def test_invalid_schedules(cron, zone_name, message):
    with pytest.raises(ScheduleError, match=message):
        next_fire_time(cron, zone_name, datetime.now(UTC))


# --- helpers ---------------------------------------------------------------------------------


def text_graph(text: str = "tick") -> dict[str, Any]:
    return {
        "nodes": [
            {"id": "t", "type": "text", "config": {"text": text}},
            {"id": "out", "type": "output", "config": {"value": "{{t.text}}"}},
        ],
        "edges": [{"source": "t", "target": "out"}],
    }


def failing_graph() -> dict[str, Any]:
    """Validates, but every run fails: its required input is never supplied."""
    return {
        "nodes": [
            {"id": "need", "type": "input", "config": {"name": "need", "required": True}},
            {"id": "out", "type": "output", "config": {"value": "{{need.value}}"}},
        ],
        "edges": [{"source": "need", "target": "out"}],
    }


async def put_trigger(client, user, wid, trigger_type, enabled=True, config=None, expect=200):
    response = await client.put(
        f"/api/workflows/{wid}/triggers/{trigger_type}", json={"enabled": enabled, "config": config}, headers=user.headers
    )
    assert response.status_code == expect, response.text
    return response.json()


def trigger_of(body: dict[str, Any], trigger_type: str) -> dict[str, Any]:
    return next(t for t in body["triggers"] if t["type"] == trigger_type)


async def schedule(client, user, graph=None, cron="*/5 * * * *", timezone="UTC") -> tuple[str, dict[str, Any]]:
    wid = await create_workflow(client, user, graph or text_graph(), name="Scheduled")
    body = await put_trigger(client, user, wid, "schedule", config={"cron": cron, "timezone": timezone})
    return wid, trigger_of(body, "schedule")


async def set_next_fire(db, trigger_id, when: datetime) -> None:
    await db.execute(update(WorkflowTrigger).where(WorkflowTrigger.id == trigger_id).values(next_fire_at=when))
    await db.commit()


async def executions_of(db, wid) -> list[WorkflowExecution]:
    return list(await db.scalars(
        select(WorkflowExecution).where(WorkflowExecution.workflow_id == uuid.UUID(wid))
        .order_by(WorkflowExecution.created_at).execution_options(populate_existing=True)
    ))


async def run_all(db, session_factory, redis, wid) -> None:
    for execution in await executions_of(db, wid):
        if execution.status is ExecutionStatus.PENDING:
            await run_execution(execution.id, session_factory=session_factory, redis=redis, worker_id="worker@test")


# --- the schedule trigger --------------------------------------------------------------------


async def test_saving_a_schedule_sets_the_next_fire_time(client, user):
    wid, trigger = await schedule(client, user, cron="30 7 * * *", timezone="Asia/Kolkata")
    assert trigger["enabled"] is True and trigger["configured"] is True
    next_run = datetime.fromisoformat(trigger["next_run_at"])
    assert (next_run.hour, next_run.minute) == (2, 0)  # 07:30 IST
    assert len(trigger["upcoming"]) == 3

    body = (await client.get(f"/api/workflows/{wid}/triggers", headers=user.headers)).json()
    assert [t["type"] for t in body["triggers"]] == ["schedule", "email", "webhook", "telegram"]
    assert body["settings"] == {"max_runs_per_hour": 30, "max_consecutive_failures": 3}

    off = trigger_of(await put_trigger(client, user, wid, "schedule", enabled=False), "schedule")
    assert off["enabled"] is False and off["next_run_at"] is None
    assert off["config"]["cron"] == "30 7 * * *"  # config kept

    bad = await client.put(f"/api/workflows/{wid}/triggers/schedule", json={"enabled": True, "config": {"cron": "every day"}},
                           headers=user.headers)
    assert bad.status_code == 422 and "5 fields" in bad.json()["detail"]


async def test_a_fire_time_starts_exactly_one_run(client, user, db_session, session_factory, task_queue):
    wid, trigger = await schedule(client, user)
    trigger_id = uuid.UUID(trigger["id"])
    due = at("2026-09-28T10:00:00+00:00")
    await set_next_fire(db_session, trigger_id, due)

    # Two ticks see the same due time.
    now = due + timedelta(seconds=4)
    first, second = await asyncio.gather(
        fire_due_schedules(session_factory, task_queue, now=now), fire_due_schedules(session_factory, task_queue, now=now)
    )
    started = [r for r in first + second if r.outcome == "started"]
    assert len(started) == 1
    [execution] = await executions_of(db_session, wid)
    assert execution.trigger.value == "schedule" and execution.trigger_id == trigger_id
    assert execution.triggered_by_user_id is None
    assert task_queue.enqueued == [(execution.id, "default")]

    row = await db_session.get(WorkflowTrigger, trigger_id, populate_existing=True)
    assert row.next_fire_at == due + timedelta(minutes=5) and row.last_fired_at == now
    # A late duplicate tick, and a stale reader that still has the old fire time, do nothing.
    assert await fire_due_schedules(session_factory, task_queue, now=now) == []
    assert await fire_schedule(session_factory, task_queue, trigger_id, now=now) is None
    assert len(await executions_of(db_session, wid)) == 1


async def test_the_event_key_is_a_second_guard(client, user, db_session, session_factory, task_queue):
    wid, trigger = await schedule(client, user)
    trigger_id = uuid.UUID(trigger["id"])
    due = at("2026-09-28T10:05:00+00:00")
    await set_next_fire(db_session, trigger_id, due)
    # As if another scheduler already fired this exact time.
    db_session.add(TriggerEvent(trigger_id=trigger_id, event_key=f"schedule:{due.isoformat()}", outcome="started"))
    await db_session.commit()
    result = await fire_schedule(session_factory, task_queue, trigger_id, now=due + timedelta(seconds=1))
    assert result.outcome == "duplicate"
    assert await executions_of(db_session, wid) == [] and task_queue.enqueued == []


async def test_two_database_connections_racing_fire_once(db_engine, task_queue):
    """Real concurrency: two sessions on separate connections claim the same fire time."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
    async with factory() as db:
        owner = User(email=f"race-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x", full_name="Race")
        db.add(owner)
        await db.flush()
        workflow = Workflow(name="Race", owner_id=owner.id, graph_json=text_graph())
        db.add(workflow)
        await db.flush()
        due = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=5)
        trigger = WorkflowTrigger(
            workflow_id=workflow.id, type=TriggerType.SCHEDULE, enabled=True,
            config_json={"cron": "* * * * *", "timezone": "UTC", "inputs": {}}, next_fire_at=due,
        )
        db.add(trigger)
        await db.commit()
        owner_id, workflow_id, trigger_id = owner.id, workflow.id, trigger.id
    try:
        results = await asyncio.gather(*(fire_schedule(factory, task_queue, trigger_id) for _ in range(4)))
        assert sorted(r.outcome for r in results if r is not None) == ["started"]
        async with factory() as db:
            runs = list(await db.scalars(select(WorkflowExecution).where(WorkflowExecution.workflow_id == workflow_id)))
            events = list(await db.scalars(select(TriggerEvent).where(TriggerEvent.trigger_id == trigger_id)))
        assert len(runs) == 1 and len(events) == 1 and len(task_queue.enqueued) == 1
    finally:
        async with factory() as db:
            await db.execute(delete(User).where(User.id == owner_id))
            await db.commit()


async def test_scheduled_runs_run_and_show_in_history(client, user, db_session, session_factory, task_queue, redis):
    wid, trigger = await schedule(client, user, cron="* * * * *")
    trigger_id = uuid.UUID(trigger["id"])
    first = at("2026-09-28T10:00:00+00:00")
    await set_next_fire(db_session, trigger_id, first)
    await fire_due_schedules(session_factory, task_queue, now=first + timedelta(seconds=2))
    await fire_due_schedules(session_factory, task_queue, now=first + timedelta(minutes=1, seconds=2))
    await run_all(db_session, session_factory, redis, wid)

    listed = (await client.get(f"/api/executions?workflow_id={wid}&trigger=schedule", headers=user.headers)).json()
    assert len(listed) == 2 and {e["status"] for e in listed} == {"success"}
    assert all(e["trigger"] == "schedule" and e["trigger_id"] == str(trigger_id) for e in listed)
    assert (await client.get("/api/executions?trigger=email", headers=user.headers)).json() == []
    panel = trigger_of((await client.get(f"/api/workflows/{wid}/triggers", headers=user.headers)).json(), "schedule")
    assert panel["last_run"]["status"] == "success" and panel["consecutive_failures"] == 0


async def test_a_long_missed_fire_time_is_skipped_not_run_late(client, user, db_session, session_factory, task_queue):
    wid, trigger = await schedule(client, user, cron="0 * * * *")
    trigger_id = uuid.UUID(trigger["id"])
    now = at("2026-09-28T12:10:00+00:00")
    await set_next_fire(db_session, trigger_id, now - timedelta(hours=3))
    [result] = await fire_due_schedules(session_factory, task_queue, now=now)
    assert result.outcome == "missed" and await executions_of(db_session, wid) == []
    row = await db_session.get(WorkflowTrigger, trigger_id, populate_existing=True)
    assert row.next_fire_at == at("2026-09-28T13:00:00+00:00")  # the next one, not a backlog
    assert row.last_error.startswith("Missed the run due at")


async def test_the_hourly_cap_skips_runs(client, user, db_session, session_factory, task_queue):
    wid, trigger = await schedule(client, user, cron="* * * * *")
    trigger_id = uuid.UUID(trigger["id"])
    settings = await client.put(f"/api/workflows/{wid}/trigger-settings",
                                json={"max_runs_per_hour": 1, "max_consecutive_failures": 3}, headers=user.headers)
    assert settings.json()["settings"]["max_runs_per_hour"] == 1
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    await set_next_fire(db_session, trigger_id, now)
    [first] = await fire_due_schedules(session_factory, task_queue, now=now + timedelta(seconds=1))
    await set_next_fire(db_session, trigger_id, now + timedelta(minutes=1))
    [second] = await fire_due_schedules(session_factory, task_queue, now=now + timedelta(minutes=1, seconds=1))
    assert (first.outcome, second.outcome) == ("started", "skipped")
    assert "already started 1 triggered runs in the last hour" in second.detail
    assert len(await executions_of(db_session, wid)) == 1
    panel = trigger_of((await client.get(f"/api/workflows/{wid}/triggers", headers=user.headers)).json(), "schedule")
    assert panel["last_error"].startswith("Skipped:") and (await client.get(
        f"/api/workflows/{wid}/triggers", headers=user.headers)).json()["runs_last_hour"] == 1


async def test_consecutive_failures_disable_the_trigger(client, user, db_session, session_factory, task_queue, redis):
    wid, trigger = await schedule(client, user, graph=failing_graph(), cron="* * * * *")
    trigger_id = uuid.UUID(trigger["id"])
    base = datetime.now(UTC).replace(second=0, microsecond=0)
    for minute in range(3):
        await set_next_fire(db_session, trigger_id, base + timedelta(minutes=minute))
        [result] = await fire_due_schedules(session_factory, task_queue, now=base + timedelta(minutes=minute, seconds=1))
        assert result.outcome == "started"
        await run_all(db_session, session_factory, redis, wid)

    panel = trigger_of((await client.get(f"/api/workflows/{wid}/triggers", headers=user.headers)).json(), "schedule")
    assert panel["enabled"] is False and panel["consecutive_failures"] == 3 and panel["auto_disabled_at"]
    assert panel["disabled_reason"].startswith("Disabled after 3 consecutive failed runs")
    assert "Missing required input 'need'" in panel["disabled_reason"]
    assert panel["next_run_at"] is None and panel["last_run"]["status"] == "failed"
    # Switched off: a tick does nothing.
    assert await fire_due_schedules(session_factory, task_queue, now=base + timedelta(minutes=5)) == []
    listing = (await client.get("/api/workflows", headers=user.headers)).json()
    assert next(w for w in listing if w["id"] == wid)["triggers"] == [{"type": "schedule", "enabled": False, "auto_disabled": True}]

    # Turning it back on clears the automatic disable.
    again = trigger_of(await put_trigger(client, user, wid, "schedule"), "schedule")
    assert again["enabled"] is True and again["consecutive_failures"] == 0 and again["disabled_reason"] is None


async def test_a_success_resets_the_failure_count(client, user, db_session, session_factory, task_queue, redis):
    wid, trigger = await schedule(client, user, graph=failing_graph(), cron="* * * * *")
    trigger_id = uuid.UUID(trigger["id"])
    base = datetime.now(UTC).replace(second=0, microsecond=0)
    await set_next_fire(db_session, trigger_id, base)
    await fire_due_schedules(session_factory, task_queue, now=base + timedelta(seconds=1))
    await run_all(db_session, session_factory, redis, wid)
    # Give the schedule the missing input: the next run succeeds.
    await put_trigger(client, user, wid, "schedule", config={"cron": "* * * * *", "timezone": "UTC", "inputs": {"need": "x"}})
    await set_next_fire(db_session, trigger_id, base + timedelta(minutes=1))
    await fire_due_schedules(session_factory, task_queue, now=base + timedelta(minutes=1, seconds=1))
    await run_all(db_session, session_factory, redis, wid)
    row = await db_session.get(WorkflowTrigger, trigger_id, populate_existing=True)
    assert row.consecutive_failures == 0 and row.enabled is True


async def test_a_run_that_cannot_start_is_recorded_as_failed(client, user, db_session, session_factory, task_queue):
    graph = {"nodes": [{"id": "x", "type": "text", "config": {}}], "edges": []}  # missing `text`
    wid, trigger = await schedule(client, user, graph=graph)
    trigger_id = uuid.UUID(trigger["id"])
    due = datetime.now(UTC).replace(second=0, microsecond=0)
    await set_next_fire(db_session, trigger_id, due)
    [result] = await fire_due_schedules(session_factory, task_queue, now=due + timedelta(seconds=1))
    assert result.outcome == "rejected" and task_queue.enqueued == []
    [execution] = await executions_of(db_session, wid)
    assert execution.status is ExecutionStatus.FAILED and execution.trigger.value == "schedule"
    assert execution.error_message.startswith("The workflow can't run") and "missing required config field 'text'" in execution.error_message
    detail = (await client.get(f"/api/executions/{execution.id}", headers=user.headers)).json()
    assert [n["status"] for n in detail["node_executions"]] == ["skipped"]
    row = await db_session.get(WorkflowTrigger, trigger_id, populate_existing=True)
    assert row.consecutive_failures == 1


async def test_schedule_preview(client, user):
    ok = (await client.post("/api/triggers/schedule-preview", json={"cron": "@daily", "timezone": "Asia/Kolkata", "count": 2},
                            headers=user.headers)).json()
    assert ok["valid"] is True and ok["cron"] == "0 0 * * *" and ok["interval"] is False and len(ok["next"]) == 2
    bad = (await client.post("/api/triggers/schedule-preview", json={"cron": "0 25 * * *"}, headers=user.headers)).json()
    assert bad == {"valid": False, "error": "'0 25 * * *' is not a valid cron expression", "cron": None, "interval": None, "next": []}


# --- the email trigger -----------------------------------------------------------------------


@dataclass
class FakeMailbox:
    """An IMAP folder: ascending UIDs, UIDVALIDITY, unread flags, SUBJECT/FROM filters."""

    uidvalidity: int = 7
    messages: list[dict[str, Any]] = field(default_factory=list)
    queries: list[Any] = field(default_factory=list)
    fail: bool = False
    name: str = "gmail"
    is_mock: bool = False

    def deliver(self, subject: str, *, message_id: str | None = None, sender: str = "Ann <ann@example.com>", unread=True) -> int:
        uid = (max((m["uid"] for m in self.messages), default=100)) + 1
        self.messages.append({
            "uid": uid, "message_id": message_id or f"<{uuid.uuid4().hex}@example.com>", "from": sender,
            "from_address": sender.split("<")[-1].rstrip(">"), "to": ["me@example.com"], "cc": [], "subject": subject,
            "date": "2026-09-28T10:00:00+00:00", "unread": unread, "snippet": subject, "body_text": f"Body of {subject}",
            "body_truncated": False, "attachments": [], "size": 100,
        })
        return uid

    async def mailbox_status(self, folder="INBOX"):
        if self.fail:
            raise ProviderError("gmail", "IMAP login rejected for me@example.com (AUTHENTICATIONFAILED)")
        return {"uidvalidity": self.uidvalidity, "uidnext": max((m["uid"] for m in self.messages), default=100) + 1, "messages": len(self.messages)}

    async def fetch_emails(self, query):
        self.queries.append(query)
        found = [
            m for m in sorted(self.messages, key=lambda m: m["uid"])
            if (query.uid_after is None or m["uid"] > query.uid_after)
            and (not query.unread_only or m["unread"])
            and (not query.subject or query.subject.lower() in m["subject"].lower())
            and (not query.from_address or query.from_address.lower() in m["from"].lower())
        ]
        found = found[: query.max_results] if query.oldest_first else found[-query.max_results:]
        return [dict(m) for m in reversed(found)]  # newest first, like IMAPEmailProvider

    async def verify(self):
        return {}


@pytest.fixture
def mailbox(monkeypatch):
    box = FakeMailbox()

    def services() -> ExecutionServices:
        return ExecutionServices(provider_settings=ProviderSettings(testing=True), mailbox_providers={"gmail": box})

    async def build(*args, **kwargs):
        return services()

    monkeypatch.setattr(trigger_service, "build_execution_services", build)
    app.dependency_overrides[get_execution_services] = services
    yield box
    app.dependency_overrides.pop(get_execution_services, None)


def triage_graph(input_type: str = "json") -> dict[str, Any]:
    return {
        "nodes": [
            {"id": "email", "type": "input", "config": {"name": "email", "input_type": input_type, "required": False}},
            {"id": "out", "type": "output", "config": {"value": "{{email.value}}"}},
        ],
        "edges": [{"source": "email", "target": "out"}],
    }


async def email_trigger(client, user, graph=None, **config) -> tuple[str, uuid.UUID]:
    wid = await create_workflow(client, user, graph or triage_graph(), name="Triage")
    body = await put_trigger(client, user, wid, "email", config={"poll_minutes": 1, **config})
    return wid, uuid.UUID(trigger_of(body, "email")["id"])


async def test_enabling_starts_from_the_current_mailbox_end(client, user, mailbox, db_session, session_factory, task_queue):
    mailbox.deliver("Old unread 1")
    mailbox.deliver("Old unread 2")
    wid, trigger_id = await email_trigger(client, user)
    row = await db_session.get(WorkflowTrigger, trigger_id, populate_existing=True)
    assert row.state_json == {"folder": "INBOX", "uidvalidity": 7, "last_uid": 102}
    result = await poll_email_trigger(session_factory, task_queue, trigger_id)
    assert result.polled and result.found == 0 and await executions_of(db_session, wid) == []


async def test_a_new_email_runs_exactly_once(client, user, mailbox, db_session, session_factory, task_queue, redis):
    wid, trigger_id = await email_trigger(client, user)
    mailbox.deliver("Server down!", message_id="<outage-1@example.com>")

    first = await poll_email_trigger(session_factory, task_queue, trigger_id, redis=redis)
    assert [r.outcome for r in first.runs] == ["started"] and first.runs[0].event_key == "email:<outage-1@example.com>"
    query = mailbox.queries[-1]
    assert (query.uid_after, query.oldest_first, query.unread_only, query.mark_as_read) == (100, True, True, False)

    # Polled again (the scheduler, "Check now", a redelivered task): nothing new.
    assert (await poll_email_trigger(session_factory, task_queue, trigger_id, redis=redis)).found == 0
    # Even if the mailbox position were lost, the Message-ID stops a second run...
    await db_session.execute(update(WorkflowTrigger).where(WorkflowTrigger.id == trigger_id)
                             .values(state_json={"folder": "INBOX", "uidvalidity": 7, "last_uid": 100}))
    await db_session.commit()
    replay = await poll_email_trigger(session_factory, task_queue, trigger_id, redis=redis)
    assert replay.found == 1 and [r.outcome for r in replay.runs] == ["duplicate"]
    # ...and so does the same message showing up again under a new UID (moved back, copied).
    mailbox.messages.append({**mailbox.messages[0], "uid": 150})
    again = await poll_email_trigger(session_factory, task_queue, trigger_id, redis=redis)
    assert [r.outcome for r in again.runs] == ["duplicate"]

    [execution] = await executions_of(db_session, wid)
    assert execution.trigger.value == "email" and execution.trigger_id == trigger_id
    assert execution.inputs_json["email"]["subject"] == "Server down!"
    assert execution.inputs_json["email"]["message_id"] == "<outage-1@example.com>"
    await run_all(db_session, session_factory, redis, wid)
    detail = (await client.get(f"/api/executions/{execution.id}", headers=user.headers)).json()
    assert detail["status"] == "success" and detail["final_output"]["result"]["from"] == "Ann <ann@example.com>"


async def test_filters_order_and_backlog(client, user, mailbox, db_session, session_factory, task_queue):
    wid, trigger_id = await email_trigger(client, user, subject="invoice", max_per_poll=2)
    mailbox.deliver("Invoice 1")
    mailbox.deliver("Lunch?")
    mailbox.deliver("Invoice 2")
    mailbox.deliver("Invoice 3", unread=False)  # already read: not "unread mail"
    mailbox.deliver("INVOICE 4")
    first = await poll_email_trigger(session_factory, task_queue, trigger_id)
    assert [r.outcome for r in first.runs] == ["started", "started"]  # max_per_poll, oldest first
    second = await poll_email_trigger(session_factory, task_queue, trigger_id)
    assert len(second.runs) == 1
    subjects = [e.inputs_json["email"]["subject"] for e in await executions_of(db_session, wid)]
    assert subjects == ["Invoice 1", "Invoice 2", "INVOICE 4"]


async def test_a_text_input_gets_the_email_as_text(client, user, mailbox, db_session, session_factory, task_queue):
    wid, trigger_id = await email_trigger(client, user, graph=triage_graph("text"))
    mailbox.deliver("Hello there")
    await poll_email_trigger(session_factory, task_queue, trigger_id)
    [execution] = await executions_of(db_session, wid)
    text = execution.inputs_json["email"]
    assert text.startswith("From: Ann <ann@example.com>\n") and "Subject: Hello there" in text and text.endswith("Body of Hello there")


async def test_a_renumbered_folder_restarts_from_its_end(client, user, mailbox, db_session, session_factory, task_queue):
    wid, trigger_id = await email_trigger(client, user)
    mailbox.uidvalidity = 8
    mailbox.deliver("After the renumbering")
    result = await poll_email_trigger(session_factory, task_queue, trigger_id)
    assert result.reset and result.runs == []
    row = await db_session.get(WorkflowTrigger, trigger_id, populate_existing=True)
    assert row.state_json["uidvalidity"] == 8 and row.state_json["last_uid"] == 101


async def test_an_unreadable_mailbox(client, user, mailbox, db_session, session_factory, task_queue):
    wid = await create_workflow(client, user, triage_graph(), name="Triage")
    mailbox.fail = True
    response = await client.put(f"/api/workflows/{wid}/triggers/email", json={"enabled": True, "config": {}}, headers=user.headers)
    assert response.status_code == 422 and response.json()["detail"].startswith("Can't read the mailbox: gmail: IMAP login rejected")

    mailbox.fail = False
    _, trigger_id = await email_trigger(client, user)
    mailbox.fail = True
    result = await poll_email_trigger(session_factory, task_queue, trigger_id)
    assert result.error.startswith("Couldn't check the mailbox")
    row = await db_session.get(WorkflowTrigger, trigger_id, populate_existing=True)
    assert row.last_error == result.error and row.enabled is True


async def test_check_now_and_the_input_warning(client, user, mailbox):
    wid = await create_workflow(client, user, text_graph(), name="No input")
    body = await put_trigger(client, user, wid, "email", config={"input_name": "email"})
    assert "No Input node is named 'email'" in trigger_of(body, "email")["warnings"][0]
    mailbox.deliver("Ping")
    checked = await client.post(f"/api/workflows/{wid}/triggers/email/check", headers=user.headers)
    assert checked.status_code == 200 and [r["outcome"] for r in checked.json()["runs"]] == ["started"]
    await put_trigger(client, user, wid, "email", enabled=False)
    off = await client.post(f"/api/workflows/{wid}/triggers/email/check", headers=user.headers)
    assert off.status_code == 409


async def test_the_tick_claims_each_poll_once(client, user, mailbox, db_session, session_factory):
    _, trigger_id = await email_trigger(client, user, poll_minutes=5)
    tick = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(minutes=1, milliseconds=180)
    assert await claim_email_polls(session_factory, now=tick) == [trigger_id]
    assert await claim_email_polls(session_factory, now=tick) == []
    row = await db_session.get(WorkflowTrigger, trigger_id, populate_existing=True)
    # On the minute, so the tick five minutes on (a few ms past :00) finds it due.
    assert row.next_fire_at == tick.replace(microsecond=0) + timedelta(minutes=5)
    assert await claim_email_polls(session_factory, now=tick + timedelta(minutes=5, milliseconds=-150)) == [trigger_id]


def test_email_event_keys():
    assert email_event_key({"message_id": " <a@b> ", "uid": 5}, 7) == "email:<a@b>"
    assert email_event_key({"message_id": None, "uid": 5}, 7) == "email:uid:7:5"


# --- the webhook trigger ---------------------------------------------------------------------


async def test_the_deployment_endpoint_is_the_webhook_trigger(client, user, db_session):
    wid = await create_workflow(client, user, text_graph(), name="Hooked")
    before = trigger_of((await client.get(f"/api/workflows/{wid}/triggers", headers=user.headers)).json(), "webhook")
    assert before["enabled"] is False and "Deploy the workflow" in before["warnings"][0]

    created = await deploy(client, user, wid, expect=201)
    key = created["api_key"]
    panel = trigger_of((await client.get(f"/api/workflows/{wid}/triggers", headers=user.headers)).json(), "webhook")
    assert panel["enabled"] is True and panel["webhook"]["endpoint"] == created["endpoint"]

    accepted = await call(client, created, key)
    assert accepted.status_code == 202, accepted.text
    execution = await db_session.get(WorkflowExecution, uuid.UUID(accepted.json()["execution_id"]))
    assert execution.trigger.value == "webhook" and execution.trigger_id is not None

    await put_trigger(client, user, wid, "webhook", enabled=False)
    refused = await call(client, created, key)
    assert refused.status_code == 409 and "switched off" in refused.json()["detail"]

    await put_trigger(client, user, wid, "webhook", enabled=True)
    await client.put(f"/api/workflows/{wid}/trigger-settings", json={"max_runs_per_hour": 1, "max_consecutive_failures": 3},
                     headers=user.headers)
    limited = await call(client, created, key)
    assert limited.status_code == 429 and "the limit is 1" in limited.json()["detail"]
