"""Page Watches: persistence for app/watches/model.py's Watch.

watch_history is append-only but retention-limited (WatchStore trims to the
newest MAX_HISTORY_PER_WATCH rows per watch on every insert) - "a lightweight
history... not unlimited snapshots", per the phase spec. It holds one row per
*meaningful* change only, never one per check.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.storage.database import Database

if TYPE_CHECKING:
    from app.watches.model import Watch

MAX_TITLE_CHARS = 200
MAX_URL_CHARS = 2000
MAX_SELECTION_HINT_CHARS = 500
MAX_SUMMARY_CHARS = 2000
#: How many change-history rows to keep per watch - a record of recent
#: meaningful changes, not an unbounded log.
MAX_HISTORY_PER_WATCH = 50


def _row_to_watch(row) -> "Watch":
    # Lazy import - same circular-import trap as app/storage/skills.py and
    # app/storage/scheduled_tasks.py: app.watches imports nothing heavy at
    # module level, but keeping the pattern consistent costs nothing and
    # protects against it growing one later.
    from app.watches.model import Watch

    return Watch(
        id=row["id"], title=row["title"], url=row["url"], target_type=row["target_type"],
        selection_hint=row["selection_hint"], condition=row["condition"],
        condition_value=row["condition_value"],
        check_interval_seconds=row["check_interval_seconds"],
        baseline_hash=row["baseline_hash"], baseline_value=row["baseline_value"],
        last_observed_hash=row["last_observed_hash"],
        last_observed_value=row["last_observed_value"],
        last_checked_at=row["last_checked_at"], next_check_at=row["next_check_at"],
        state=row["state"], failure_count=row["failure_count"], mission_id=row["mission_id"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


class WatchStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(
        self, *, title: str, url: str, target_type: str, condition: str,
        check_interval_seconds: int, selection_hint: str = "", condition_value: str = "",
        next_check_at: str | None = None, mission_id: int | None = None,
    ) -> "Watch | None":
        title = (title or "").strip()[:MAX_TITLE_CHARS]
        url = (url or "").strip()[:MAX_URL_CHARS]
        if not title or not url:
            return None
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cursor = self._db.execute(
            "INSERT INTO watches (title, url, target_type, selection_hint, condition, "
            "condition_value, check_interval_seconds, state, failure_count, next_check_at, "
            "mission_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 0, ?, ?, ?, ?)",
            (title, url, target_type, (selection_hint or "")[:MAX_SELECTION_HINT_CHARS],
             condition, condition_value, check_interval_seconds, next_check_at, mission_id,
             now, now))
        if cursor is None:
            return None
        return self.get(cursor.lastrowid)

    def get(self, watch_id: int) -> "Watch | None":
        row = self._db.query_one("SELECT * FROM watches WHERE id = ?", (watch_id,))
        return _row_to_watch(row) if row is not None else None

    def all(self) -> list["Watch"]:
        rows = self._db.query(
            "SELECT * FROM watches ORDER BY COALESCE(next_check_at, '9999') ASC, id ASC")
        return [_row_to_watch(row) for row in rows]

    def due_watches(self, now: datetime) -> list["Watch"]:
        rows = self._db.query(
            "SELECT * FROM watches WHERE state = 'active' AND next_check_at IS NOT NULL "
            "AND next_check_at <= ? ORDER BY next_check_at ASC",
            (now.isoformat(),))
        return [_row_to_watch(row) for row in rows]

    def set_state(self, watch_id: int, state: str) -> None:
        self._db.execute(
            "UPDATE watches SET state = ?, updated_at = ? WHERE id = ?",
            (state, datetime.now(timezone.utc).isoformat(timespec="seconds"), watch_id))

    def set_mission_id(self, watch_id: int, mission_id: int) -> None:
        self._db.execute(
            "UPDATE watches SET mission_id = ?, updated_at = ? WHERE id = ?",
            (mission_id, datetime.now(timezone.utc).isoformat(timespec="seconds"), watch_id))

    def record_check(
        self, watch_id: int, *, state: str, next_check_at: str | None, last_checked_at: str,
        baseline_hash: str | None = None, baseline_value: str | None = None,
        last_observed_hash: str | None = None, last_observed_value: str | None = None,
        failure_count: int = 0,
    ) -> None:
        """Persist the result of one check - whether or not it changed
        anything. baseline_* is only ever set once, the first time a watch
        is checked; every check after that updates last_observed_* only."""
        fields = ["state = ?", "next_check_at = ?", "last_checked_at = ?",
                  "failure_count = ?", "updated_at = ?"]
        params: list = [state, next_check_at, last_checked_at, failure_count,
                        datetime.now(timezone.utc).isoformat(timespec="seconds")]
        if baseline_hash is not None:
            fields.append("baseline_hash = ?")
            params.append(baseline_hash)
        if baseline_value is not None:
            fields.append("baseline_value = ?")
            params.append(baseline_value)
        fields.append("last_observed_hash = ?")
        params.append(last_observed_hash)
        fields.append("last_observed_value = ?")
        params.append(last_observed_value)
        params.append(watch_id)
        self._db.execute(f"UPDATE watches SET {', '.join(fields)} WHERE id = ?", params)

    def remove(self, watch_id: int) -> None:
        self._db.execute("DELETE FROM watches WHERE id = ?", (watch_id,))

    # -- watch_history: a bounded record of meaningful changes --------------

    def record_change(
        self, watch_id: int, *, observed_at: str, summary: str,
        old_value: str | None, new_value: str | None,
    ) -> None:
        self._db.execute(
            "INSERT INTO watch_history (watch_id, observed_at, summary, old_value, new_value) "
            "VALUES (?, ?, ?, ?, ?)",
            (watch_id, observed_at, (summary or "")[:MAX_SUMMARY_CHARS], old_value, new_value))
        self._trim_history(watch_id)

    def _trim_history(self, watch_id: int) -> None:
        rows = self._db.query(
            "SELECT id FROM watch_history WHERE watch_id = ? ORDER BY observed_at DESC, id DESC",
            (watch_id,))
        stale = [row["id"] for row in rows[MAX_HISTORY_PER_WATCH:]]
        for row_id in stale:
            self._db.execute("DELETE FROM watch_history WHERE id = ?", (row_id,))

    def history_for(self, watch_id: int, limit: int = MAX_HISTORY_PER_WATCH) -> list[dict]:
        rows = self._db.query(
            "SELECT id, watch_id, observed_at, summary, old_value, new_value FROM watch_history "
            "WHERE watch_id = ? ORDER BY observed_at DESC, id DESC LIMIT ?",
            (watch_id, limit))
        return [dict(row) for row in rows]
