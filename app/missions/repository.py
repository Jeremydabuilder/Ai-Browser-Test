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
    MAX_ALTERNATIVES,
    MAX_CHALLENGE_SUMMARY,
    MAX_DECISION_CHARS,
    MAX_EVIDENCE,
    MAX_FINDING_CHARS,
    MAX_FINDINGS_PER_MISSION,
    MAX_POINT_CHARS,
    MAX_POINTS,
    MAX_RATIONALE_CHARS,
    MAX_TITLE,
    ChallengePoint,
    DecisionAlternative,
    DecisionEvidence,
    Mission,
    MissionChallenge,
    MissionDecision,
    PointKind,
    TargetKind,
    Verdict,
    MissionFinding,
    MissionPage,
    MissionStatus,
    PageSource,
    clean_goal,
    clean_title,
    clean_finding,
    collapse,
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
        decision = self.decision(mission_id) if with_pages else None
        challenges = tuple(self.challenges(mission_id)) if with_pages else ()
        return Mission(**dict(row), pages=pages, findings=found, decision=decision,
                       challenges=challenges)

    def recent(self, limit: int = 20, *, with_pages: bool = False) -> list[Mission]:
        """Missions, most recently touched first."""
        rows = self._db.query(
            self._MISSION_COLUMNS + f"WHERE {self._ALIVE} "
            "ORDER BY updated_at DESC, id DESC LIMIT ?", (limit,))
        return [
            Mission(**dict(row),
                    pages=tuple(self.pages(row["id"])) if with_pages else (),
                    findings=tuple(self.findings(row["id"])) if with_pages else (),
                    decision=self.decision(row["id"]) if with_pages else None)
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
                    findings=tuple(self.findings(row["id"])) if with_pages else (),
                    decision=self.decision(row["id"]) if with_pages else None)
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

    # -- decisions -------------------------------------------------------
    #
    # Append-only. `save_decision` inserts and supersedes; nothing here ever
    # rewrites a decision in place, because the question this table exists to
    # answer - "what did we decide, and on what?" - is a question about the
    # past.

    DECISION_SAVED = "saved"
    DECISION_TOO_LONG = "too_long"
    DECISION_NO_TEXT = "no_text"
    DECISION_UNKNOWN_EVIDENCE = "unknown_evidence"

    def save_decision(self, mission_id: int, decision: str, rationale: str,
                      evidence_ids: list[int] | None = None,
                      alternatives: list[tuple[str, str]] | None = None
                      ) -> tuple[str, MissionDecision | None]:
        """Record a decision, superseding whatever was decided before.

        Evidence ids are checked against this Mission's findings and a
        stranger is refused, not dropped: a decision citing evidence from
        somewhere else is worse than one citing none.
        """
        # collapse(), not clean_title(): clean_title truncates, and truncating
        # before a length check would silently store a shortened decision.
        decision = collapse(decision)
        rationale = collapse(rationale)
        if not decision or not rationale:
            return self.DECISION_NO_TEXT, None
        if len(decision) > MAX_DECISION_CHARS or len(rationale) > MAX_RATIONALE_CHARS:
            return self.DECISION_TOO_LONG, None

        cited = list(dict.fromkeys(evidence_ids or []))[:MAX_EVIDENCE]
        findings = {f.id: f for f in self.findings(mission_id)}
        unknown = [i for i in cited if i not in findings]
        if unknown:
            return self.DECISION_UNKNOWN_EVIDENCE, None

        stamp = now()
        current = self.decision(mission_id)
        if current is not None:
            self._db.execute(
                "UPDATE mission_decisions SET superseded_at = ? WHERE id = ?",
                (stamp, current.id))
        cursor = self._db.execute(
            "INSERT INTO mission_decisions "
            "(mission_id, decision, rationale, created_at, superseded_at) "
            "VALUES (?, ?, ?, ?, '')",
            (mission_id, decision, rationale, stamp))
        if cursor is None:
            return self.DECISION_NO_TEXT, None
        decision_id = int(cursor.lastrowid)

        for position, finding_id in enumerate(cited):
            finding = findings[finding_id]
            # The snapshot is taken here and never rewritten. This line is the
            # whole historical-accuracy guarantee.
            self._db.execute(
                "INSERT INTO decision_evidence "
                "(decision_id, finding_id, text, source, position) "
                "VALUES (?, ?, ?, ?, ?)",
                (decision_id, finding_id, finding.text, finding.source_domain, position))
        for position, (name, reason) in enumerate(
                (alternatives or [])[:MAX_ALTERNATIVES]):
            name = collapse(name)[:MAX_DECISION_CHARS]
            reason = collapse(reason)[:MAX_RATIONALE_CHARS]
            if not name:
                continue
            self._db.execute(
                "INSERT INTO decision_alternatives "
                "(decision_id, name, reason, position) VALUES (?, ?, ?, ?)",
                (decision_id, name, reason, position))
        self._touch_mission(mission_id)
        return self.DECISION_SAVED, self.get_decision(decision_id)

    def decision(self, mission_id: int) -> MissionDecision | None:
        """The live decision for a Mission, or None.

        Superseded rows are never returned: the product shows one decision.
        """
        row = self._db.query_one(
            "SELECT id FROM mission_decisions "
            "WHERE mission_id = ? AND superseded_at = ''", (mission_id,))
        return self.get_decision(int(row["id"])) if row else None

    def get_decision(self, decision_id: int) -> MissionDecision | None:
        row = self._db.query_one(
            "SELECT id, mission_id, decision, rationale, created_at, superseded_at "
            "FROM mission_decisions WHERE id = ?", (decision_id,))
        if row is None:
            return None
        return MissionDecision(
            **dict(row),
            alternatives=tuple(self._alternatives(decision_id)),
            evidence=tuple(self._evidence(decision_id)))

    def decision_history(self, mission_id: int) -> list[MissionDecision]:
        """Every decision this Mission has had, newest first.

        Not shown anywhere in the product. It exists because the rows do, and
        because a record nobody can read is not a record.
        """
        rows = self._db.query(
            "SELECT id FROM mission_decisions WHERE mission_id = ? "
            "ORDER BY created_at DESC, id DESC", (mission_id,))
        return [self.get_decision(int(row["id"])) for row in rows]

    def clear_decision(self, mission_id: int) -> bool:
        """Unset the current decision, keeping the record that it was made."""
        current = self.decision(mission_id)
        if current is None:
            return False
        self._db.execute("UPDATE mission_decisions SET superseded_at = ? WHERE id = ?",
                         (now(), current.id))
        self._touch_mission(mission_id)
        return True

    def _alternatives(self, decision_id: int) -> list[DecisionAlternative]:
        rows = self._db.query(
            "SELECT id, decision_id, name, reason, position FROM decision_alternatives "
            "WHERE decision_id = ? ORDER BY position, id", (decision_id,))
        return [DecisionAlternative(**dict(row)) for row in rows]

    def _evidence(self, decision_id: int) -> list[DecisionEvidence]:
        """Evidence with the live finding's text alongside the snapshot.

        The LEFT JOIN is what lets the UI say "this finding has changed since"
        or "this finding is gone" instead of quietly showing one of the two
        and hoping they still agree.
        """
        rows = self._db.query(
            "SELECT e.id, e.decision_id, e.finding_id, e.text, e.source, e.position, "
            "       f.text AS current_text "
            "FROM decision_evidence e "
            "LEFT JOIN mission_findings f ON f.id = e.finding_id "
            "WHERE e.decision_id = ? ORDER BY e.position, e.id", (decision_id,))
        return [DecisionEvidence(**dict(row)) for row in rows]

    # -- challenges ------------------------------------------------------
    #
    # A challenge never edits what it challenges. It is a second opinion filed
    # beside the first, so the user can read both; overwriting the original
    # would destroy the comparison the feature exists to make.

    CHALLENGE_SAVED = "saved"
    CHALLENGE_TOO_LONG = "too_long"
    CHALLENGE_NO_TEXT = "no_text"
    CHALLENGE_BAD_VERDICT = "bad_verdict"
    CHALLENGE_BAD_KIND = "bad_kind"
    CHALLENGE_UNKNOWN_TARGET = "unknown_target"

    def save_challenge(self, mission_id: int, target_kind: str, target_id: int,
                       claim: str, verdict: str, summary: str,
                       points: list[tuple[str, str, int | None]] | None = None
                       ) -> tuple[str, MissionChallenge | None]:
        """Record the result of attacking one claim, superseding any earlier one."""
        if target_kind not in TargetKind.ALL:
            return self.CHALLENGE_UNKNOWN_TARGET, None
        if verdict not in Verdict.ALL:
            return self.CHALLENGE_BAD_VERDICT, None
        summary = collapse(summary)
        claim = collapse(claim)
        if not summary or not claim:
            return self.CHALLENGE_NO_TEXT, None
        if len(summary) > MAX_CHALLENGE_SUMMARY:
            return self.CHALLENGE_TOO_LONG, None

        cleaned: list[tuple[str, str, int | None]] = []
        for kind, text, page_id in (points or [])[:MAX_POINTS]:
            if kind not in PointKind.ALL:
                return self.CHALLENGE_BAD_KIND, None
            text = collapse(text)
            if not text:
                continue
            if len(text) > MAX_POINT_CHARS:
                return self.CHALLENGE_TOO_LONG, None
            cleaned.append((kind, text, page_id))

        stamp = now()
        current = self.challenge(target_kind, target_id)
        if current is not None:
            self._db.execute(
                "UPDATE mission_challenges SET superseded_at = ? WHERE id = ?",
                (stamp, current.id))
        cursor = self._db.execute(
            "INSERT INTO mission_challenges "
            "(mission_id, target_kind, target_id, claim, verdict, summary, "
            " created_at, superseded_at) VALUES (?, ?, ?, ?, ?, ?, ?, '')",
            (mission_id, target_kind, target_id, claim, verdict, summary, stamp))
        if cursor is None:
            return self.CHALLENGE_NO_TEXT, None
        challenge_id = int(cursor.lastrowid)
        for position, (kind, text, page_id) in enumerate(cleaned):
            self._db.execute(
                "INSERT INTO challenge_points "
                "(challenge_id, kind, text, page_id, position) VALUES (?, ?, ?, ?, ?)",
                (challenge_id, kind, text, page_id, position))
        self._touch_mission(mission_id)
        return self.CHALLENGE_SAVED, self.get_challenge(challenge_id)

    def challenge(self, target_kind: str, target_id: int) -> MissionChallenge | None:
        """The live challenge against one claim, or None."""
        row = self._db.query_one(
            "SELECT id FROM mission_challenges "
            "WHERE target_kind = ? AND target_id = ? AND superseded_at = ''",
            (target_kind, target_id))
        return self.get_challenge(int(row["id"])) if row else None

    def challenges(self, mission_id: int) -> list[MissionChallenge]:
        """Every live challenge on a Mission."""
        rows = self._db.query(
            "SELECT id FROM mission_challenges "
            "WHERE mission_id = ? AND superseded_at = '' ORDER BY created_at, id",
            (mission_id,))
        return [self.get_challenge(int(row["id"])) for row in rows]

    def get_challenge(self, challenge_id: int) -> MissionChallenge | None:
        row = self._db.query_one(
            "SELECT id, mission_id, target_kind, target_id, claim, verdict, "
            "       summary, created_at, superseded_at "
            "FROM mission_challenges WHERE id = ?", (challenge_id,))
        if row is None:
            return None
        return MissionChallenge(**dict(row), points=tuple(self._points(challenge_id)))

    def clear_challenge(self, challenge_id: int) -> bool:
        """Retire a challenge, keeping the record that it was made."""
        challenge = self.get_challenge(challenge_id)
        if challenge is None or not challenge.live:
            return False
        self._db.execute("UPDATE mission_challenges SET superseded_at = ? WHERE id = ?",
                         (now(), challenge_id))
        self._touch_mission(challenge.mission_id)
        return True

    def _points(self, challenge_id: int) -> list[ChallengePoint]:
        rows = self._db.query(
            "SELECT c.id, c.challenge_id, c.kind, c.text, c.page_id, c.position, "
            "       COALESCE(p.url, '') AS source_url, "
            "       COALESCE(p.title, '') AS source_title "
            "FROM challenge_points c "
            "LEFT JOIN mission_pages p ON p.id = c.page_id "
            "WHERE c.challenge_id = ? ORDER BY c.position, c.id", (challenge_id,))
        return [ChallengePoint(**dict(row)) for row in rows]

    def _touch_mission(self, mission_id: int) -> None:
        self._db.execute("UPDATE missions SET updated_at = ? WHERE id = ?",
                         (now(), mission_id))
