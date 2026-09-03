"""Routines in SQLite. Same shape as MissionStore: owns the SQL, no policy."""

from __future__ import annotations

import json

from app.missions.model import now
from app.routines.model import (
    MAX_ARG_CHARS,
    MAX_NAME_CHARS,
    MAX_STEPS,
    Routine,
    RoutineStep,
    collapse,
)
from app.storage.database import Database


class RoutineStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- routines ----------------------------------------------------------
    def create(self, mission_id: int, name: str,
              steps: list[tuple[str, dict, str]]) -> Routine | None:
        """Save a taught sequence. ``steps`` is (tool_name, args, description).

        Refuses rather than truncates: a Routine cut short mid-recording would
        replay a task it does not actually perform, silently.
        """
        name = collapse(name)[:MAX_NAME_CHARS]
        if not name or not steps or len(steps) > MAX_STEPS:
            return None
        for _tool, args, _desc in steps:
            if len(json.dumps(args, ensure_ascii=False)) > MAX_ARG_CHARS:
                return None

        stamp = now()
        cursor = self._db.execute(
            "INSERT INTO routines (mission_id, name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)", (mission_id, name, stamp, stamp))
        if cursor is None:
            return None
        routine_id = int(cursor.lastrowid)
        for position, (tool_name, args, description) in enumerate(steps):
            self._db.execute(
                "INSERT INTO routine_steps "
                "(routine_id, position, tool_name, args, description) "
                "VALUES (?, ?, ?, ?, ?)",
                (routine_id, position, tool_name,
                 json.dumps(args, ensure_ascii=False), description))
        return self.get(routine_id)

    def get(self, routine_id: int) -> Routine | None:
        row = self._db.query_one(
            "SELECT id, mission_id, name, created_at, updated_at "
            "FROM routines WHERE id = ?", (routine_id,))
        if row is None:
            return None
        return Routine(**dict(row), steps=tuple(self._steps(routine_id)))

    def for_mission(self, mission_id: int) -> list[Routine]:
        rows = self._db.query(
            "SELECT id FROM routines WHERE mission_id = ? "
            "ORDER BY created_at, id", (mission_id,))
        return [self.get(int(row["id"])) for row in rows]

    def rename(self, routine_id: int, name: str) -> bool:
        name = collapse(name)[:MAX_NAME_CHARS]
        if not name:
            return False
        cursor = self._db.execute(
            "UPDATE routines SET name = ?, updated_at = ? WHERE id = ?",
            (name, now(), routine_id))
        return bool(cursor is not None and cursor.rowcount)

    def delete(self, routine_id: int) -> None:
        # routine_steps cascades: PRAGMA foreign_keys is ON.
        self._db.execute("DELETE FROM routines WHERE id = ?", (routine_id,))

    def _steps(self, routine_id: int) -> list[RoutineStep]:
        rows = self._db.query(
            "SELECT id, routine_id, position, tool_name, args, description "
            "FROM routine_steps WHERE routine_id = ? ORDER BY position, id",
            (routine_id,))
        steps = []
        for row in rows:
            data = dict(row)
            try:
                args = json.loads(data.pop("args") or "{}")
            except (TypeError, ValueError):
                args = {}
            steps.append(RoutineStep(**data, args=args if isinstance(args, dict) else {}))
        return steps
