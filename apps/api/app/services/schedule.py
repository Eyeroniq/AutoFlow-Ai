"""Cron schedules in a time zone: validation and the next fire time (croniter).

Daylight saving time follows classic cron:

- **Fixed times** (the hour field names hours, e.g. `30 7 * * *`) are wall-clock times that
  fire once a day. When clocks fall back and 01:30 happens twice, `30 1 * * *` fires on the
  first 01:30 only; plain croniter would fire on both. When clocks spring forward past
  02:30, `30 2 * * *` fires at 03:30, the same offset after the jump.
- **Intervals** (the hour field is `*` or `*/n`, e.g. `*/15 * * * *` or `0 * * * *`) follow
  real time: every quarter hour stays a quarter hour apart through the change, so the
  repeated hour gets its runs too.

Five fields only (minute granularity: the beat ticks once a minute), plus the @hourly,
@daily, @weekly, @monthly, and @yearly shorthands.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterError, croniter

ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
# A cron that matches nothing for years (e.g. Feb 30) must not loop forever.
_MAX_STEPS = 5000


class ScheduleError(ValueError):
    """An invalid cron expression or time zone; the message is safe to show."""


def normalize_cron(expression: str) -> str:
    """The expression with single spaces and aliases expanded. Raises ScheduleError."""
    text = " ".join(str(expression).split())
    text = ALIASES.get(text.lower(), text)
    fields = text.split(" ")
    if len(fields) != 5:
        raise ScheduleError(
            f"a cron expression has 5 fields (minute hour day-of-month month day-of-week), e.g. '30 7 * * *'; got {len(fields)}"
        )
    if not croniter.is_valid(text):
        raise ScheduleError(f"'{text}' is not a valid cron expression")
    return text


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ScheduleError(
            f"unknown time zone '{name}' (use an IANA name such as UTC, Europe/Berlin, or Asia/Kolkata)"
        ) from None


def is_interval(expression: str) -> bool:
    """True when the hour field is `*` or `*/n` (real-time semantics across DST)."""
    return normalize_cron(expression).split(" ")[1].startswith("*")


def next_fire_time(expression: str, timezone: str, after: datetime) -> datetime:
    """The first fire time strictly after `after`, in UTC. See the module docstring for DST."""
    expression = normalize_cron(expression)
    tz = zone(timezone)
    after = after.astimezone(UTC)
    local = after.astimezone(tz)
    try:
        if is_interval(expression):
            return croniter(expression, local).get_next(datetime).astimezone(UTC)
        # Wall-clock times: walk local times and take the first occurrence of each (fold=0).
        times = croniter(expression, local.replace(tzinfo=None))
        for _ in range(_MAX_STEPS):
            candidate = times.get_next(datetime).replace(tzinfo=tz).astimezone(UTC)
            if candidate > after:
                return candidate
    except CroniterError:
        pass  # e.g. '0 0 30 2 *': February never has a 30th
    raise ScheduleError(f"'{expression}' never fires (check the day and month fields)")


def upcoming(expression: str, timezone: str, after: datetime, count: int = 5) -> list[datetime]:
    times: list[datetime] = []
    moment = after
    for _ in range(count):
        moment = next_fire_time(expression, timezone, moment)
        times.append(moment)
    return times
