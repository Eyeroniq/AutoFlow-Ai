"""ICS Calendar: an .ics event file from structured fields, ready to attach or send.

Typical use: Structured Output reads "lunch with Asha tomorrow 1-2pm at Cafe Mondegar" from
an email (with {{system.now}} in its prompt, so "tomorrow" means something), and this node
turns {title, start, end, location} into a calendar file for a Gmail attachment
({{ics.attachment}}) or a Telegram document.

Dates must be explicit: an ISO date-time ("2026-10-05T15:00", "2026-10-05 15:00:00+05:30")
or a date ("2026-10-05", an all-day event). Anything else (including relative words like
"tomorrow") is not guessed at: the node succeeds with `ambiguous: true` and a reason, and no
file, so a Condition can ask for clarification instead of booking the wrong day.
"""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from flowforge_engine.models import NodeContext, NodeResult
from flowforge_engine.registry import NodeConfig, NodeDefinition, register_node

CONTENT_TYPE = "text/calendar"
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[A-Za-z]{2,}$")


class CalendarConfig(NodeConfig):
    title: str = Field(min_length=1, description="The event's title, e.g. {{event.data.title}}.")
    start: str = Field(description="ISO date-time (2026-10-05T15:00) or date (2026-10-05, all day), e.g. {{event.data.start}}.")
    end: str | None = Field(default=None, description="ISO date-time or date. Blank: start + duration_minutes.")
    duration_minutes: int = Field(default=60, ge=1, le=60 * 24 * 14, description="Used when end is blank.")
    timezone: str = Field(default="Asia/Kolkata", description="IANA time zone for times written without an offset.")
    location: str = ""
    description: str = ""
    attendees: str | list[str] = Field(default_factory=list, description="Email addresses: a list, or comma-separated.")
    organizer: str | None = Field(default=None, description="The organizer's email (optional).")
    alarm_minutes: int = Field(default=30, ge=0, le=60 * 24 * 7, description="Reminder this many minutes before (0 = none).")
    method: Literal["PUBLISH", "REQUEST"] = Field(
        default="PUBLISH", description="PUBLISH: a file to add to a calendar. REQUEST: an invitation the attendees can answer."
    )
    filename: str | None = Field(default=None, description="Blank: the title, made safe, plus .ics.")

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone '{value}' (use an IANA name such as Asia/Kolkata)") from None
        return value


class CalendarResult(BaseModel):
    ambiguous: bool
    reason: str | None
    content: str
    content_base64: str
    filename: str
    content_type: str
    uid: str | None
    start: str | None
    end: str | None
    all_day: bool
    attachment: dict[str, Any] | None


def parse_when(value: str, zone: ZoneInfo) -> datetime | date | None:
    """A date (all day) or an aware date-time; None when it isn't explicit ISO 8601."""
    text = (value or "").strip()
    if not text:
        return None
    if _DATE_ONLY.match(text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=zone)


def _addresses(value: str | list[str]) -> list[str]:
    items = value if isinstance(value, list) else str(value).split(",")
    return [a.strip() for a in items if str(a).strip()]


def _safe_name(title: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "", title).strip().replace(" ", "-")[:60] or "event"
    return f"{stem}.ics"


def event_uid(title: str, start: datetime | date) -> str:
    """Stable: the same title and start give the same UID, so re-sending updates the event."""
    digest = hashlib.sha256(f"{title.strip().lower()}|{start.isoformat()}".encode()).hexdigest()[:32]
    return f"{digest}@flowforge"


def build_ics(config: CalendarConfig, start: datetime | date, end: datetime | date, *, now: datetime | None = None) -> str:
    from icalendar import Alarm, Calendar, Event, vCalAddress, vText

    calendar = Calendar()
    calendar.add("prodid", "-//FlowForge AI//ICS Calendar node//EN")
    calendar.add("version", "2.0")
    calendar.add("calscale", "GREGORIAN")
    calendar.add("method", config.method)
    event = Event()
    event.add("uid", event_uid(config.title, start))
    event.add("dtstamp", now or datetime.now(UTC))
    event.add("summary", config.title.strip())
    event.add("dtstart", start)
    event.add("dtend", end)
    if config.location.strip():
        event.add("location", config.location.strip())
    if config.description.strip():
        event.add("description", config.description.strip())
    if config.organizer:
        organizer = vCalAddress(f"mailto:{config.organizer.strip()}")
        event["organizer"] = organizer
    for address in _addresses(config.attendees):
        attendee = vCalAddress(f"mailto:{address}")
        attendee.params["ROLE"] = vText("REQ-PARTICIPANT")
        attendee.params["RSVP"] = vText("TRUE" if config.method == "REQUEST" else "FALSE")
        event.add("attendee", attendee, encode=0)
    if config.alarm_minutes:
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", config.title.strip())
        alarm.add("trigger", timedelta(minutes=-config.alarm_minutes))
        event.add_component(alarm)
    calendar.add_component(event)
    return calendar.to_ical().decode("utf-8")


@register_node("ics_calendar")
class CalendarNode(NodeDefinition[CalendarConfig]):
    category = "integration"
    label = "ICS Calendar Event"
    description = "Makes an .ics calendar file (title, times, place, attendees, reminder) to attach to an email or send on Telegram."
    icon = "calendar-plus"
    config_schema = CalendarConfig
    output_schema = CalendarResult
    portable = True

    async def execute(self, context: NodeContext, config: CalendarConfig) -> NodeResult:
        zone = ZoneInfo(config.timezone)
        filename = config.filename.strip() if config.filename and config.filename.strip() else _safe_name(config.title)
        if not filename.lower().endswith(".ics"):
            filename += ".ics"

        def unclear(reason: str) -> NodeResult:
            return NodeResult.ok(
                ambiguous=True, reason=reason, content="", content_base64="", filename=filename, content_type=CONTENT_TYPE,
                uid=None, start=None, end=None, all_day=False, attachment=None,
            )

        start = parse_when(config.start, zone)
        if start is None:
            return unclear(f"The start '{config.start}' isn't an exact date or date-time (expected e.g. 2026-10-05T15:00)")
        if config.end and config.end.strip():
            end = parse_when(config.end, zone)
            if end is None:
                return unclear(f"The end '{config.end}' isn't an exact date or date-time")
        elif isinstance(start, datetime):
            end = start + timedelta(minutes=config.duration_minutes)
        else:
            end = start + timedelta(days=1)
        all_day = not isinstance(start, datetime)
        if all_day != (not isinstance(end, datetime)):
            return unclear("The start and end mix a whole day with a time of day")
        if end <= start:  # type: ignore[operator]
            return unclear("The end is not after the start")
        bad = [a for a in _addresses(config.attendees) + ([config.organizer] if config.organizer else []) if not _EMAIL.match(a)]
        if bad:
            return NodeResult.fail(f"Not an email address: {', '.join(bad)}")
        content = build_ics(config, start, end)
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        return NodeResult.ok(
            ambiguous=False, reason=None, content=content, content_base64=encoded, filename=filename,
            content_type=CONTENT_TYPE, uid=event_uid(config.title, start), start=start.isoformat(), end=end.isoformat(),
            all_day=all_day,
            # Drops straight into a Gmail node's attachments or a Telegram node's document.
            attachment={"filename": filename, "content": content, "encoding": "text", "content_type": CONTENT_TYPE},
        )
