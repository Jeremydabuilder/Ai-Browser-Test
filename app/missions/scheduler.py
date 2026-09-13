"""Scheduled Missions: a scheduling/task layer on top of the existing
Mission and agent machinery - not a second execution engine.

A ScheduledTask names a Mission (existing or to be created) and when to run
it. Firing one still means exactly one thing: AgentSession.send(goal) (or
resuming an existing Mission first) - the same call a typed message makes.
See app/missions/task_runner.py for the timer that decides when to fire one
and how it recovers from a crash mid-run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


class TaskState:
    """One scheduled run's own lifecycle - distinct from MissionStatus
    (active/paused/completed), which is about whether the user considers
    the underlying Mission's *work* done. A Mission can be MissionStatus
    ACTIVE while its ScheduledTask sits QUEUED waiting for tomorrow.
    """

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"

    ALL = (QUEUED, RUNNING, WAITING_FOR_APPROVAL, PAUSED, COMPLETED, FAILED)


class ScheduleKind:
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"
    INTERVAL = "interval"

    ALL = (ONCE, DAILY, WEEKLY, INTERVAL)


@dataclass(frozen=True)
class ScheduledTask:
    id: int
    mission_id: int | None
    mission_title: str
    goal: str
    schedule_kind: str
    #: "once"'s fire time (ISO), or None for the other kinds.
    schedule_at: str | None
    #: "daily"/"weekly"'s time of day, "HH:MM", or None otherwise.
    time_of_day: str | None
    #: "weekly"'s day, 0=Monday..6=Sunday, or None otherwise.
    weekday: int | None
    #: "interval"'s spacing in seconds, or None otherwise.
    interval_seconds: int | None
    state: str
    next_run_at: str | None
    last_run_at: str | None
    last_duration_s: float | None
    last_error: str | None
    #: Set the moment a not-read-only tool call is about to run, before it
    #: actually does - see TaskRunner. If the app dies mid-run, this is what
    #: tells recovery whether the interrupted run might have left a write
    #: half-done (never silently replayed) or definitely had not yet (safe
    #: to just say so and let the user retry deliberately either way).
    write_attempted: bool
    created_at: str
    updated_at: str


def _parse_time_of_day(value: str) -> tuple[int, int]:
    hour_str, _, minute_str = value.partition(":")
    return int(hour_str), int(minute_str)


def compute_next_run(
    kind: str,
    *,
    now: datetime,
    schedule_at: str | None = None,
    time_of_day: str | None = None,
    weekday: int | None = None,
    interval_seconds: int | None = None,
    last_run_at: datetime | None = None,
) -> datetime | None:
    """The next time this schedule should fire, strictly after ``now``, or
    None when there is not one (a "once" schedule already in the past - it
    already ran, or never will).

    Pure and clock-free by design: every "what time is it" input is a
    parameter, so this is testable without patching datetime.now anywhere.
    """
    if kind == ScheduleKind.ONCE:
        if not schedule_at:
            return None
        at = datetime.fromisoformat(schedule_at)
        return at if at > now else None

    if kind == ScheduleKind.DAILY:
        if not time_of_day:
            return None
        hour, minute = _parse_time_of_day(time_of_day)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if kind == ScheduleKind.WEEKLY:
        if not time_of_day or weekday is None:
            return None
        hour, minute = _parse_time_of_day(time_of_day)
        days_ahead = (weekday - now.weekday()) % 7
        candidate = (now + timedelta(days=days_ahead)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    if kind == ScheduleKind.INTERVAL:
        if not interval_seconds or interval_seconds <= 0:
            return None
        base = last_run_at or now
        candidate = base + timedelta(seconds=interval_seconds)
        # A stale base (the app was closed longer than one interval) must
        # not fire a burst of instantly-overdue runs on restart - the next
        # run is always at least one full interval from *now*.
        if candidate <= now:
            candidate = now + timedelta(seconds=interval_seconds)
        return candidate

    return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
