"""Scheduled Missions: persistence for the scheduling/task layer described in
app/missions/scheduler.py. This store knows nothing about timers or the
agent - it only reads and writes rows. See app/missions/task_runner.py for
the part that decides when a due task actually fires.

task_runs is a separate, append-only history table: a ScheduledTask row can
only ever show its own most recent run (last_run_at/last_error), so a
"Mission history/audit" view needs its own table of one row per fire.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.storage.database import Database

if TYPE_CHECKING:
    from app.missions.scheduler import ScheduledTask

MAX_GOAL_CHARS = 4000
MAX_ERROR_CHARS = 4000


def _row_to_task(row) -> "ScheduledTask":
    # Imported lazily, not at module level - app.missions (via
    # app.missions.service -> app.agent.* -> ... -> app.storage.database) is
    # reached again while app.storage's own __init__ is still mid-import the
    # first time anything imports app.storage. Same trap as app/storage/skills.py.
    from app.missions.scheduler import ScheduledTask

    return ScheduledTask(
        id=row["id"], mission_id=row["mission_id"], mission_title=row["mission_title"],
        goal=row["goal"], schedule_kind=row["schedule_kind"], schedule_at=row["schedule_at"],
        time_of_day=row["time_of_day"], weekday=row["weekday"],
        interval_seconds=row["interval_seconds"], state=row["state"],
        next_run_at=row["next_run_at"], last_run_at=row["last_run_at"],
        last_duration_s=row["last_duration_s"], last_error=row["last_error"],
        write_attempted=bool(row["write_attempted"]),
        created_at=row["created_at"], updated_at=row["updated_at"],
        workspace_id=row["workspace_id"] if "workspace_id" in row.keys() else None,
    )


class ScheduledTaskStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(
        self, *, goal: str, schedule_kind: str, mission_id: int | None = None,
        mission_title: str = "", schedule_at: str | None = None,
        time_of_day: str | None = None, weekday: int | None = None,
        interval_seconds: int | None = None, next_run_at: str | None = None,
        state: str = "queued", workspace_id: str | None = None,
    ) -> "ScheduledTask | None":
        goal = (goal or "").strip()[:MAX_GOAL_CHARS]
        if not goal:
            return None
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cursor = self._db.execute(
            "INSERT INTO scheduled_tasks (mission_id, mission_title, goal, schedule_kind, "
            "schedule_at, time_of_day, weekday, interval_seconds, state, next_run_at, "
            "write_attempted, workspace_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (mission_id, mission_title, goal, schedule_kind, schedule_at, time_of_day,
             weekday, interval_seconds, state, next_run_at, workspace_id, now, now),
        )
        if cursor is None:
            return None
        return self.get(cursor.lastrowid)

    def get(self, task_id: int) -> "ScheduledTask | None":
        row = self._db.query_one("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))
        return _row_to_task(row) if row is not None else None

    def all(self) -> list["ScheduledTask"]:
        rows = self._db.query(
            "SELECT * FROM scheduled_tasks ORDER BY "
            "COALESCE(next_run_at, '9999') ASC, id ASC")
        return [_row_to_task(row) for row in rows]

    def due_tasks(self, now: datetime) -> list["ScheduledTask"]:
        """Queued tasks whose time has come, earliest first."""
        rows = self._db.query(
            "SELECT * FROM scheduled_tasks WHERE state = 'queued' AND next_run_at IS NOT NULL "
            "AND next_run_at <= ? ORDER BY next_run_at ASC",
            (now.isoformat(),))
        return [_row_to_task(row) for row in rows]

    def running_tasks(self) -> list["ScheduledTask"]:
        rows = self._db.query("SELECT * FROM scheduled_tasks WHERE state = 'running'")
        return [_row_to_task(row) for row in rows]

    def set_state(self, task_id: int, state: str) -> None:
        self._db.execute(
            "UPDATE scheduled_tasks SET state = ?, updated_at = ? WHERE id = ?",
            (state, datetime.now(timezone.utc).isoformat(timespec="seconds"), task_id))

    def set_write_attempted(self, task_id: int, write_attempted: bool) -> None:
        self._db.execute(
            "UPDATE scheduled_tasks SET write_attempted = ?, updated_at = ? WHERE id = ?",
            (1 if write_attempted else 0,
             datetime.now(timezone.utc).isoformat(timespec="seconds"), task_id))

    def set_mission_id(self, task_id: int, mission_id: int, mission_title: str = "") -> None:
        self._db.execute(
            "UPDATE scheduled_tasks SET mission_id = ?, mission_title = ?, updated_at = ? "
            "WHERE id = ?",
            (mission_id, mission_title,
             datetime.now(timezone.utc).isoformat(timespec="seconds"), task_id))

    def record_run_result(
        self, task_id: int, *, state: str, next_run_at: str | None,
        last_run_at: str, duration_s: float, error: str | None = None,
    ) -> None:
        """A run just finished (successfully or not): update the task's own
        summary fields and reset write_attempted for the run that is about
        to be scheduled next."""
        error = (error or "")[:MAX_ERROR_CHARS] or None
        self._db.execute(
            "UPDATE scheduled_tasks SET state = ?, next_run_at = ?, last_run_at = ?, "
            "last_duration_s = ?, last_error = ?, write_attempted = 0, updated_at = ? "
            "WHERE id = ?",
            (state, next_run_at, last_run_at, duration_s, error,
             datetime.now(timezone.utc).isoformat(timespec="seconds"), task_id))

    def remove(self, task_id: int) -> None:
        self._db.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))

    # -- task_runs: append-only audit history -------------------------------

    def record_run_start(self, task_id: int, started_at: str) -> int | None:
        cursor = self._db.execute(
            "INSERT INTO task_runs (task_id, started_at, outcome) VALUES (?, ?, 'running')",
            (task_id, started_at))
        return cursor.lastrowid if cursor is not None else None

    def record_run_finish(
        self, run_id: int, *, finished_at: str, outcome: str, error: str | None = None,
    ) -> None:
        error = (error or "")[:MAX_ERROR_CHARS] or None
        self._db.execute(
            "UPDATE task_runs SET finished_at = ?, outcome = ?, error = ? WHERE id = ?",
            (finished_at, outcome, error, run_id))

    def runs_for_task(self, task_id: int, limit: int = 50) -> list[dict]:
        rows = self._db.query(
            "SELECT id, task_id, started_at, finished_at, outcome, error FROM task_runs "
            "WHERE task_id = ? ORDER BY started_at DESC LIMIT ?",
            (task_id, limit))
        return [dict(row) for row in rows]
