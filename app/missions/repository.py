"""Missions in SQLite.

One store class per feature, same shape as BookmarkStore and HistoryStore: it
owns the SQL and nothing else. No Qt, no agent, no policy about *when* a page
should be associated - that judgement belongs to MissionService.

Writes here are synchronous rather than queued. The background writer exists so
that a history row on every page load never stalls the GUI thread; a Mission is
created or renamed by a person pressing a button, and the UI reads it back
immediately afterwards, so it has to see its own change. Page association is
the one hot path, and it is still only as frequent as the agent's actions.
"""

from __future__ import annotations

from app.missions.model import (
    MAX_TITLE,
    Mission,
    MissionPage,
    MissionStatus,
    PageSource,
    clean_goal,
    clean_title,
    is_associable,
    now,
    page_key,
)
from app.storage.database import Database

#: How many pages one Mission may accumulate. A Mission is a task, not a
#: crawl; past a few hundred pages something has gone wrong and the panel
#: would be unusable anyway.
MAX_PAGES_PER_MISSION = 500


class MissionStore:
    """Read and write Missions. Never decides what a Mission *means*."""

    def __init__(self, db: Database) -> None:
        self._db = db

    # -- missions --------------------------------------------------------
    def create(self, title: str, goal: str) -> Mission | None:
        """Insert a Mission. Returns it, or None if the goal was empty."""
        goal = clean_goal(goal)
        title = clean_title(title)
        if not goal or not title:
            return None
        stamp = now()
        cursor = self._db.execute(
            "INSERT INTO missions (title, goal, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, goal, MissionStatus.ACTIVE, stamp, stamp),
        )
        if cursor is None:                     # database closed during shutdown
            return None
        return Mission(id=int(cursor.lastrowid), title=title, goal=goal,
                       status=MissionStatus.ACTIVE, created_at=stamp, updated_at=stamp)

    def get(self, mission_id: int, *, with_pages: bool = True) -> Mission | None:
        row = self._db.query_one(
            "SELECT id, title, goal, status, created_at, updated_at "
            "FROM missions WHERE id = ?", (mission_id,))
        if row is None:
            return None
        pages = tuple(self.pages(mission_id)) if with_pages else ()
        return Mission(**dict(row), pages=pages)

    def recent(self, limit: int = 20, *, with_pages: bool = False) -> list[Mission]:
        """Missions, most recently touched first."""
        rows = self._db.query(
            "SELECT id, title, goal, status, created_at, updated_at "
            "FROM missions ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,))
        return [
            Mission(**dict(row),
                    pages=tuple(self.pages(row["id"])) if with_pages else ())
            for row in rows
        ]

    def set_status(self, mission_id: int, status: str) -> bool:
        if status not in MissionStatus.ALL:
            return False
        return self._touch(mission_id, "status = ?", (status,))

    def rename(self, mission_id: int, title: str) -> bool:
        """Give a Mission a title the user chose.

        title_from_goal() is a local heuristic and will sometimes be wrong, so
        this is not an advanced feature - it is the correction for a guess the
        product makes on the user's behalf. An empty new title is refused
        rather than stored, because a Mission with no name is unusable.
        """
        title = clean_title(title)
        if not title:
            return False
        return self._touch(mission_id, "title = ?", (title,))

    def set_goal(self, mission_id: int, goal: str) -> bool:
        goal = clean_goal(goal)
        if not goal:
            return False
        return self._touch(mission_id, "goal = ?", (goal,))

    def delete(self, mission_id: int) -> None:
        # mission_pages cascades: PRAGMA foreign_keys is ON (see database.py).
        self._db.execute("DELETE FROM missions WHERE id = ?", (mission_id,))

    def count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM missions")
        return int(row["n"]) if row else 0

    def _touch(self, mission_id: int, assignment: str, params: tuple) -> bool:
        """Apply one column change and refresh updated_at in the same statement."""
        cursor = self._db.execute(
            f"UPDATE missions SET {assignment}, updated_at = ? WHERE id = ?",
            (*params, now(), mission_id))
        return bool(cursor is not None and cursor.rowcount)

    # -- pages -----------------------------------------------------------
    def add_page(self, mission_id: int, url: str, title: str = "",
                 source: str = PageSource.AGENT) -> MissionPage | None:
        """Record a page against a Mission, or refresh the one already there.

        Identity is ``page_key(url)``, not the raw string - see model.py. The
        UNIQUE constraint is the real guard; the lookup below is what lets a
        revisit update the title instead of failing.
        """
        key = page_key(url)
        if not is_associable(key):
            return None
        if source not in PageSource.ALL:
            source = PageSource.AGENT
        title = clean_title(title)[:MAX_TITLE]
        stamp = now()

        existing = self.find_page(mission_id, key)
        if existing is not None:
            # A page seen again is the same page. Keep first_seen, and keep the
            # earlier source: a page Py opened does not become a page Py merely
            # read because it was read afterwards.
            self._db.execute(
                "UPDATE mission_pages SET title = CASE WHEN ? <> '' THEN ? ELSE title END, "
                "last_seen = ? WHERE id = ?",
                (title, title, stamp, existing.id))
            self._touch_mission(mission_id)
            return self.find_page(mission_id, key)

        if self.page_count(mission_id) >= MAX_PAGES_PER_MISSION:
            return None
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO mission_pages "
            "(mission_id, url, title, source, note, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?, '', ?, ?)",
            (mission_id, key, title, source, stamp, stamp))
        if cursor is None:
            return None
        self._touch_mission(mission_id)
        return self.find_page(mission_id, key)

    def pages(self, mission_id: int) -> list[MissionPage]:
        rows = self._db.query(
            "SELECT id, mission_id, url, title, source, note, first_seen, last_seen "
            "FROM mission_pages WHERE mission_id = ? ORDER BY first_seen, id",
            (mission_id,))
        return [MissionPage(**dict(row)) for row in rows]

    def find_page(self, mission_id: int, url: str) -> MissionPage | None:
        row = self._db.query_one(
            "SELECT id, mission_id, url, title, source, note, first_seen, last_seen "
            "FROM mission_pages WHERE mission_id = ? AND url = ?",
            (mission_id, page_key(url)))
        return MissionPage(**dict(row)) if row else None

    def page_count(self, mission_id: int) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM mission_pages WHERE mission_id = ?", (mission_id,))
        return int(row["n"]) if row else 0

    def remove_page(self, page_id: int) -> None:
        """Forget one page. Note that closing its tab does NOT call this:
        a Mission remembers where it went, whether or not the tab is still open."""
        self._db.execute("DELETE FROM mission_pages WHERE id = ?", (page_id,))

    def _touch_mission(self, mission_id: int) -> None:
        self._db.execute("UPDATE missions SET updated_at = ? WHERE id = ?",
                         (now(), mission_id))
