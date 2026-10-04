"""Saved Team missions: one row each, the whole run as a JSON document.

Page text attached as a source is stored with the mission so Retry and the
history view work after a restart; Delete removes it. Stored text is capped
per source (see MAX_SOURCE_CHARS) and nothing here ever holds a credential.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from app.storage.database import Database
from app.team.model import Mission, MissionStatus, TaskStatus

MAX_SOURCE_CHARS = 30_000
MAX_GOAL_CHARS = 4000


@dataclass(frozen=True)
class HistoryRow:
    id: int
    goal: str
    status: str
    updated_at: float


class TeamStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, mission: Mission) -> int:
        """Insert a new mission and return (and set) its id."""
        mission.goal = mission.goal[:MAX_GOAL_CHARS]
        cursor = self._db.execute(
            "INSERT INTO team_missions (goal, status, created_at, updated_at, data_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (mission.goal, mission.status, mission.created_at, mission.updated_at,
             json.dumps(self._bounded(mission.to_dict()))))
        if cursor is None:
            return 0
        mission.id = int(cursor.lastrowid)
        return mission.id

    @staticmethod
    def _bounded(data: dict) -> dict:
        for source in data.get("sources", []):
            if len(source.get("text", "")) > MAX_SOURCE_CHARS:
                source["text"] = source["text"][:MAX_SOURCE_CHARS]
        return data

    def save_dict(self, mission_id: int, data: dict, mission: Mission | None = None) -> None:
        if not mission_id:
            return
        self._db.execute(
            "UPDATE team_missions SET goal = ?, status = ?, updated_at = ?, data_json = ? WHERE id = ?",
            (str(data.get("goal", ""))[:MAX_GOAL_CHARS], data.get("status", MissionStatus.DRAFT),
             float(data.get("updated_at") or time.time()), json.dumps(self._bounded(data)), mission_id))

    def load(self, mission_id: int) -> Mission | None:
        row = self._db.query_one("SELECT id, data_json FROM team_missions WHERE id = ?", (mission_id,))
        if row is None:
            return None
        try:
            mission = Mission.from_dict(json.loads(row["data_json"]))
        except (ValueError, TypeError):
            return None
        mission.id = row["id"]
        return mission

    def history(self, limit: int = 50) -> list[HistoryRow]:
        rows = self._db.query(
            "SELECT id, goal, status, updated_at FROM team_missions ORDER BY updated_at DESC, id DESC LIMIT ?",
            (limit,))
        return [HistoryRow(r["id"], r["goal"], r["status"], r["updated_at"]) for r in rows]

    def delete(self, mission_id: int) -> None:
        self._db.execute("DELETE FROM team_missions WHERE id = ?", (mission_id,))

    def recover_after_restart(self) -> int:
        """A mission left planning/running by a closed app becomes INTERRUPTED
        (its running tasks go back to pending; finished work is kept)."""
        rows = self._db.query(
            "SELECT id FROM team_missions WHERE status IN (?, ?)",
            (MissionStatus.PLANNING, MissionStatus.RUNNING))
        for row in rows:
            mission = self.load(row["id"])
            if mission is None:
                continue
            mission.status = MissionStatus.INTERRUPTED
            for task in mission.tasks:
                if task.status == TaskStatus.RUNNING:
                    task.status = TaskStatus.PENDING
            self.save_dict(mission.id, mission.to_dict(), mission)
        return len(rows)
