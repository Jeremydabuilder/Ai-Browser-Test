"""Persistence for the Mission Execution Graph - see app/missions/graph.py
for the node model and app/missions/coordinator.py for what writes here as
a Multi-Agent Mission runs.

Restart safety lives here, not in the coordinator: recover_after_restart
is the one place that decides what an interrupted node means. It follows
exactly the same rule Phase 7's TaskRunner.recover_after_restart and Phase
8's watch failure handling already use - a node is never silently
resumed if there is any chance it half-finished a write.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.storage.database import Database

if TYPE_CHECKING:
    from app.missions.graph import GraphNode

MAX_TITLE_CHARS = 200
MAX_INSTRUCTIONS_CHARS = 4000
MAX_RESULT_SUMMARY_CHARS = 2000
MAX_ERROR_CHARS = 2000


def _row_to_node(row) -> "GraphNode":
    # Lazy import - the same circular-import guard app/storage/skills.py,
    # app/storage/scheduled_tasks.py and app/storage/watches.py all use.
    from app.missions.graph import GraphNode

    return GraphNode(
        id=row["id"], mission_id=row["mission_id"], node_type=row["node_type"],
        role=row["role"], title=row["title"], instructions=row["instructions"],
        dependencies=tuple(json.loads(row["dependencies"] or "[]")),
        state=row["state"], attempt_count=row["attempt_count"],
        write_attempted=bool(row["write_attempted"]),
        result_summary=row["result_summary"], error=row["error"],
        findings_added=row["findings_added"], plan_round=row["plan_round"],
        created_at=row["created_at"], started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MissionGraphStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create_node(
        self, mission_id: int, *, node_type: str, role: str, title: str,
        instructions: str, dependencies: list[int] | tuple[int, ...] = (),
        plan_round: int = 0,
    ) -> "GraphNode | None":
        now = _now()
        cursor = self._db.execute(
            "INSERT INTO mission_graph_nodes (mission_id, node_type, role, title, "
            "instructions, dependencies, state, plan_round, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (mission_id, node_type, role, title[:MAX_TITLE_CHARS],
             instructions[:MAX_INSTRUCTIONS_CHARS], json.dumps(list(dependencies)),
             plan_round, now))
        if cursor is None:
            return None
        return self.get(cursor.lastrowid)

    def get(self, node_id: int) -> "GraphNode | None":
        row = self._db.query_one("SELECT * FROM mission_graph_nodes WHERE id = ?", (node_id,))
        return _row_to_node(row) if row is not None else None

    def nodes_for_mission(self, mission_id: int) -> list["GraphNode"]:
        rows = self._db.query(
            "SELECT * FROM mission_graph_nodes WHERE mission_id = ? ORDER BY id ASC",
            (mission_id,))
        return [_row_to_node(row) for row in rows]

    def count_for_mission(self, mission_id: int) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM mission_graph_nodes WHERE mission_id = ?", (mission_id,))
        return int(row["n"]) if row is not None else 0

    def set_dependencies(self, node_id: int, dependencies: list[int]) -> None:
        self._db.execute(
            "UPDATE mission_graph_nodes SET dependencies = ? WHERE id = ?",
            (json.dumps(list(dependencies)), node_id))

    def set_state(self, node_id: int, state: str) -> None:
        self._db.execute(
            "UPDATE mission_graph_nodes SET state = ? WHERE id = ?", (state, node_id))

    def set_title(self, node_id: int, title: str) -> None:
        """Plan-preview editing (rename a step) - see MissionCoordinator.
        rename_task. Only meaningful before a node has ever run."""
        self._db.execute(
            "UPDATE mission_graph_nodes SET title = ? WHERE id = ?",
            (title[:MAX_TITLE_CHARS], node_id))

    def record_start(self, node_id: int) -> None:
        self._db.execute(
            "UPDATE mission_graph_nodes SET state = 'running', started_at = ?, "
            "attempt_count = attempt_count + 1 WHERE id = ?", (_now(), node_id))

    def record_terminal(
        self, node_id: int, *, state: str, result_summary: str = "", error: str = "",
        findings_added: int = 0,
    ) -> None:
        self._db.execute(
            "UPDATE mission_graph_nodes SET state = ?, completed_at = ?, result_summary = ?, "
            "error = ?, findings_added = ? WHERE id = ?",
            (state, _now(), result_summary[:MAX_RESULT_SUMMARY_CHARS],
             error[:MAX_ERROR_CHARS], findings_added, node_id))

    def set_write_attempted(self, node_id: int, value: bool) -> None:
        self._db.execute(
            "UPDATE mission_graph_nodes SET write_attempted = ? WHERE id = ?",
            (1 if value else 0, node_id))

    def reset_for_retry(self, node_id: int) -> None:
        """Bring a FAILED/SKIPPED/NEEDS_REVIEW node back to pending for a
        fresh attempt - never done automatically, only in response to an
        explicit user or coordinator retry action."""
        self._db.execute(
            "UPDATE mission_graph_nodes SET state = 'pending', error = '', "
            "write_attempted = 0 WHERE id = ?", (node_id,))

    def stranded_nodes(self) -> list["GraphNode"]:
        """Every node left RUNNING or WAITING_FOR_APPROVAL across every
        Mission - what a crash mid-run looks like the next time the app
        starts. See recover_after_restart, which is the only thing that
        should normally call this."""
        rows = self._db.query(
            "SELECT * FROM mission_graph_nodes WHERE state IN ('running', 'waiting_for_approval')")
        return [_row_to_node(row) for row in rows]


def recover_after_restart(store: MissionGraphStore) -> list["GraphNode"]:
    """Correct every node a crash left stranded. Called once at startup.

    Never guesses whether a write finished: a node that had already
    started a non-read-only tool call (write_attempted) always becomes
    NEEDS_REVIEW, no matter how it looked otherwise - the user must decide
    whether to retry it. A node that was still safely read-only work goes
    back to PENDING, eligible to be picked up (by an explicit retry/resume
    action - see MissionCoordinator) without anyone having to guess
    anything about it.
    """
    recovered = []
    for node in store.stranded_nodes():
        if node.write_attempted:
            store.set_state(node.id, "needs_review")
        else:
            store.set_state(node.id, "pending")
        recovered.append(node)
    return recovered
