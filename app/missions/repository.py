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
    MAX_FINDING_CHARS,
    MAX_FINDINGS_PER_MISSION,
    MAX_TITLE,
    Mission,
    MissionFinding,
    MissionPage,
    MissionStatus,
    PageSource,
    clean_goal,
    clean_title,
    clean_finding,
    finding_key,
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

    #: Columns every Mission read selects, and the clause that hides deleted
    #: ones. Written once so a new query cannot forget the filter - which is
    #: the failure mode of soft delete everywhere it has ever been done badly.
    _MISSION_COLUMNS = ("SELECT id, title, goal, status, created_at, updated_at "
                        "FROM missions ")
    _ALIVE = "deleted_at = ''"

    def get(self, mission_id: int, *, with_pages: bool = True) -> Mission | None:
        row = self._db.query_one(
            self._MISSION_COLUMNS + f"WHERE id = ? AND {self._ALIVE}",
            (mission_id,))
        if row is None:
            return None
        pages = tuple(self.pages(mission_id)) if with_pages else ()
        found = tuple(self.findings(mission_id)) if with_pages else ()
        return Mission(**dict(row), pages=pages, findings=found)

    def recent(self, limit: int = 20, *, with_pages: bool = False) -> list[Mission]:
        """Missions, most recently touched first."""
        rows = self._db.query(
            self._MISSION_COLUMNS + f"WHERE {self._ALIVE} "
            "ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,))
        return [
            Mission(**dict(row),
                    pages=tuple(self.pages(row["id"])) if with_pages else (),
                    findings=tuple(self.findings(row["id"])) if with_pages else ())
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
        """Destroy a Mission and everything under it. Irreversible.

        Reached only from an explicit "delete permanently" - `soft_delete` is
        what the Delete button does.
        """
        # mission_pages and mission_findings cascade: PRAGMA foreign_keys is ON
        # (see database.py).
        self._db.execute("DELETE FROM missions WHERE id = ?", (mission_id,))

    def count(self) -> int:
        row = self._db.query_one(
            f"SELECT COUNT(*) AS n FROM missions WHERE {self._ALIVE}")
        return int(row["n"]) if row else 0

    # -- the library -----------------------------------------------------
    def search(self, query: str, limit: int = 200, *,
               with_pages: bool = False) -> list[Mission]:
        """Missions matching ``query`` across their goal and their contents.

        One method, because the Library, and later anything that wants to ask
        "what do we know about X?", must query the same corpus the same way.
        Swapping this for FTS5 when Mission counts justify it is then a change
        to one query rather than a hunt through the UI.

        Matching is a case-insensitive substring over the Mission's own title
        and goal, its findings, and its pages' titles and URLs. Ordered by
        where the match landed - a Mission called "Tennis Shoes" outranks one
        that merely visited a page about them.
        """
        needle = " ".join((query or "").split()).lower()
        if not needle:
            return self.recent(limit, with_pages=with_pages)
        like = f"%{needle}%"
        rows = self._db.query(
            self._MISSION_COLUMNS.replace("SELECT", "SELECT DISTINCT") +
            f"""WHERE {self._ALIVE} AND (
                    LOWER(title) LIKE ? OR LOWER(goal) LIKE ?
                 OR id IN (SELECT mission_id FROM mission_findings
                            WHERE LOWER(text) LIKE ?)
                 OR id IN (SELECT mission_id FROM mission_pages
                            WHERE LOWER(title) LIKE ? OR LOWER(url) LIKE ?))
                ORDER BY
                  CASE WHEN LOWER(title) LIKE ? THEN 0
                       WHEN LOWER(goal) LIKE ? THEN 1
                       ELSE 2 END,
                  updated_at DESC, id DESC
                LIMIT ?""",
            (like, like, like, like, like, like, like, limit))
        return [
            Mission(**dict(row),
                    pages=tuple(self.pages(row["id"])) if with_pages else (),
                    findings=tuple(self.findings(row["id"])) if with_pages else ())
            for row in rows
        ]

    def soft_delete(self, mission_id: int) -> bool:
        """Hide a Mission without destroying it.

        The default meaning of "delete" here, because a Mission is the record
        of a decision and the reasons behind it. See the schema note.
        """
        cursor = self._db.execute(
            f"UPDATE missions SET deleted_at = ? WHERE id = ? AND {self._ALIVE}",
            (now(), mission_id))
        return bool(cursor is not None and cursor.rowcount)

    def restore(self, mission_id: int) -> bool:
        cursor = self._db.execute(
            "UPDATE missions SET deleted_at = '' WHERE id = ?", (mission_id,))
        return bool(cursor is not None and cursor.rowcount)

    def is_deleted(self, mission_id: int) -> bool:
        row = self._db.query_one("SELECT deleted_at FROM missions WHERE id = ?",
                                 (mission_id,))
        return bool(row and row["deleted_at"])

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

    # -- findings --------------------------------------------------------
    #
    # A finding is model-authored prose about untrusted page content. This
    # class stores it; it never decides what deserves to be one, and it never
    # sees a web page.

    #: What add_finding did, so the caller can tell the model and the user
    #: apart from each other without parsing a sentence.
    SAVED = "saved"
    UPDATED = "updated"          # same key already present, text refreshed
    TOO_LONG = "too_long"
    FULL = "full"
    NO_TEXT = "no_text"

    def add_finding(self, mission_id: int, text: str,
                    page_id: int | None = None) -> tuple[str, MissionFinding | None]:
        """Record a discovery. Returns (outcome, finding).

        Over-length findings are REFUSED, not truncated. Cutting
        "$129 until Friday" down to "$129" would store a fact with its
        qualifier removed, and a wrong fact in the user's board is worse than
        one extra tool call.
        """
        text = clean_finding(text)
        if not text:
            return self.NO_TEXT, None
        if len(text) > MAX_FINDING_CHARS:
            return self.TOO_LONG, None

        key = finding_key(text)
        existing = self.find_finding(mission_id, key)
        if existing is not None:
            # The same discovery again. Refresh the wording and the source -
            # a second sighting on a better page is worth keeping - but not
            # created_at, because this is the same finding.
            self._db.execute(
                "UPDATE mission_findings SET text = ?, page_id = COALESCE(?, page_id), "
                "updated_at = ? WHERE id = ?",
                (text, page_id, now(), existing.id))
            self._touch_mission(mission_id)
            return self.UPDATED, self.get_finding(existing.id)

        if self.finding_count(mission_id) >= MAX_FINDINGS_PER_MISSION:
            return self.FULL, None
        stamp = now()
        cursor = self._db.execute(
            "INSERT INTO mission_findings "
            "(mission_id, page_id, text, key, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (mission_id, page_id, text, key, stamp, stamp))
        if cursor is None:
            return self.NO_TEXT, None
        self._touch_mission(mission_id)
        return self.SAVED, self.get_finding(int(cursor.lastrowid))

    def edit_finding(self, finding_id: int, text: str) -> tuple[str, MissionFinding | None]:
        """Reword a finding, keeping its dedup key in step with its text.

        If the new wording collides with another finding in the same Mission,
        the edit is REFUSED rather than resolved by guesswork. Merging would
        silently delete a row the user did not ask to lose, and letting it
        through would violate UNIQUE. Refusing is the only outcome that
        destroys nothing and is the same every time.
        """
        text = clean_finding(text)
        if not text:
            return self.NO_TEXT, None
        if len(text) > MAX_FINDING_CHARS:
            return self.TOO_LONG, None
        current = self.get_finding(finding_id)
        if current is None:
            return self.NO_TEXT, None

        key = finding_key(text)
        clash = self.find_finding(current.mission_id, key)
        if clash is not None and clash.id != finding_id:
            return "duplicate", clash
        self._db.execute(
            "UPDATE mission_findings SET text = ?, key = ?, updated_at = ? WHERE id = ?",
            (text, key, now(), finding_id))
        self._touch_mission(current.mission_id)
        return self.UPDATED, self.get_finding(finding_id)

    #: Every finding, with its source page joined on. A LEFT JOIN because
    #: page_id is nullable by design.
    _FINDING_COLUMNS = (
        "SELECT f.id, f.mission_id, f.text, f.key, f.page_id, "
        "       f.created_at, f.updated_at, "
        "       COALESCE(p.url, '') AS source_url, "
        "       COALESCE(p.title, '') AS source_title "
        "FROM mission_findings f "
        "LEFT JOIN mission_pages p ON p.id = f.page_id ")

    def findings(self, mission_id: int) -> list[MissionFinding]:
        rows = self._db.query(
            self._FINDING_COLUMNS + "WHERE f.mission_id = ? ORDER BY f.created_at, f.id",
            (mission_id,))
        return [MissionFinding(**dict(row)) for row in rows]

    def get_finding(self, finding_id: int) -> MissionFinding | None:
        row = self._db.query_one(self._FINDING_COLUMNS + "WHERE f.id = ?", (finding_id,))
        return MissionFinding(**dict(row)) if row else None

    def find_finding(self, mission_id: int, key: str) -> MissionFinding | None:
        row = self._db.query_one(
            self._FINDING_COLUMNS + "WHERE f.mission_id = ? AND f.key = ?",
            (mission_id, key))
        return MissionFinding(**dict(row)) if row else None

    def finding_count(self, mission_id: int) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS n FROM mission_findings WHERE mission_id = ?",
            (mission_id,))
        return int(row["n"]) if row else 0

    def remove_finding(self, finding_id: int) -> bool:
        cursor = self._db.execute("DELETE FROM mission_findings WHERE id = ?",
                                  (finding_id,))
        return bool(cursor is not None and cursor.rowcount)

    def _touch_mission(self, mission_id: int) -> None:
        self._db.execute("UPDATE missions SET updated_at = ? WHERE id = ?",
                         (now(), mission_id))
