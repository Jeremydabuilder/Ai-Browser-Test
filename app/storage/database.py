"""Thin SQLite wrapper shared by every store.

Design notes
------------
* One connection for the whole app. The UI is single-threaded, but WebEngine
  callbacks can arrive from helper threads, so ``check_same_thread=False`` plus
  an explicit lock keeps things safe without dragging in an ORM.
* **Writes that happen on every page load are queued to a background thread.**
  A single INSERT measures ~0.3 ms, so this is not about throughput; it is
  about never letting an fsync stall on a slow or busy disk block the GUI
  thread. User-initiated writes (bookmarks, settings) stay synchronous because
  the UI reads them back immediately and must see its own change.
* Reads call ``flush()`` first, so "queued in the background" is never visible
  as missing data.
* Schema creation is idempotent and versioned via ``PRAGMA user_version``.
  ``_SCHEMA`` is what a brand-new profile gets; ``_MIGRATIONS`` is how an
  existing one catches up. See ``_apply_schema``.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_VERSION = 7

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    visited_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_visited_at ON history(visited_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_url ON history(url);

CREATE TABLE IF NOT EXISTS bookmarks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT NOT NULL UNIQUE,
    title      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Missions: a goal the user is working on, and the pages that served it.
-- Pages are addressed by URL, never by tab id: a tab id is an in-memory
-- counter that means nothing after a restart, and holding one would make a
-- mission corruptible by closing a tab.
-- next_ref is the next finding reference this mission will issue: a high-water
-- mark rather than a count, because deleting the highest-numbered finding must
-- not hand its number to the next one - a citation written last month would
-- start pointing at something else.
--
-- Note for future edits: SQLite re-parses a table's definition on
-- ALTER TABLE ... DROP COLUMN, and a comment sitting between the last column
-- and the closing bracket makes that fail. Keep the prose up here.
--
-- deleted_at is a soft delete, and it is not bookkeeping: a Mission is the
-- record of a decision, and "why did we rule that out?" is a question people
-- ask months later. Deleting rows on request would answer it with silence.
-- Users who genuinely want the data gone get a separate, explicit permanent
-- delete.
CREATE TABLE IF NOT EXISTS missions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    goal       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT NOT NULL DEFAULT '',
    next_ref   INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_missions_updated ON missions(updated_at DESC);

CREATE TABLE IF NOT EXISTS mission_pages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    url        TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'agent',
    note       TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    UNIQUE(mission_id, url)
);
CREATE INDEX IF NOT EXISTS idx_mission_pages_mission
    ON mission_pages(mission_id, last_seen DESC);

-- What Py discovered, and which page it came from. page_id is ON DELETE SET
-- NULL rather than CASCADE: losing a source costs the attribution, never the
-- discovery. UNIQUE(mission_id, key) is what makes deduplication a constraint
-- rather than a hopeful check.
-- `ref` is the mission-local number a finding is known by - F1, F2, F3. It is
-- what the user and the model see; the row id never leaves this layer. Refs
-- are assigned once and never reused, so deleting F2 leaves a permanent gap
-- rather than repointing every citation that mentioned it.
CREATE TABLE IF NOT EXISTS mission_findings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    page_id    INTEGER          REFERENCES mission_pages(id) ON DELETE SET NULL,
    text       TEXT NOT NULL,
    key        TEXT NOT NULL,
    ref        INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(mission_id, key)
);
CREATE INDEX IF NOT EXISTS idx_mission_findings_mission
    ON mission_findings(mission_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_finding_ref
    ON mission_findings(mission_id, ref);

-- What was decided, and the reasons a person can read. Deliberately holds no
-- model reasoning: `rationale` is the sentence shown to the user.
--
-- Append-only. Editing a decision inserts a new row and stamps the old one
-- superseded, because "we changed our mind, and here is what we used to
-- think" is part of the record. The partial unique index makes "at most one
-- live decision per mission" a guarantee of the database rather than a
-- convention of the code.
CREATE TABLE IF NOT EXISTS mission_decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id    INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    decision      TEXT NOT NULL,
    rationale     TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    superseded_at TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_live_decision
    ON mission_decisions(mission_id) WHERE superseded_at = '';

-- What a decision takes for granted, said out loud. User-visible data, and
-- the one part of "why" that a rationale paragraph cannot be decomposed into.
CREATE TABLE IF NOT EXISTS decision_assumptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL REFERENCES mission_decisions(id) ON DELETE CASCADE,
    text        TEXT NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_decision_assumptions
    ON decision_assumptions(decision_id, position);

CREATE TABLE IF NOT EXISTS decision_alternatives (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL REFERENCES mission_decisions(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    reason      TEXT NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_decision_alternatives
    ON decision_alternatives(decision_id, position);

-- Evidence is both a reference and a snapshot. The reference keeps the
-- decision connected to the live board; the snapshot keeps it honest, because
-- a finding edited afterwards must not silently rewrite what the decision was
-- made on. finding_id is ON DELETE SET NULL so a deleted finding costs the
-- link, never the record of what was believed.
CREATE TABLE IF NOT EXISTS decision_evidence (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL REFERENCES mission_decisions(id) ON DELETE CASCADE,
    finding_id  INTEGER          REFERENCES mission_findings(id) ON DELETE SET NULL,
    text        TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT '',
    position    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_decision_evidence
    ON decision_evidence(decision_id, position);

-- The result of trying to prove a claim wrong. Never replaces what it
-- challenges: the original finding or decision is left as it was, and this
-- sits beside it so the user can see both and judge.
--
-- target_id is a plain integer, not a foreign key, and `claim` snapshots the
-- challenged text. A polymorphic FK would buy nothing and cost the history:
-- delete the finding and the challenge should still say what it was made
-- against. Append-only, like decisions.
CREATE TABLE IF NOT EXISTS mission_challenges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id    INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    target_kind   TEXT NOT NULL,
    target_id     INTEGER NOT NULL,
    claim         TEXT NOT NULL,
    verdict       TEXT NOT NULL,
    summary       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    superseded_at TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_live_challenge
    ON mission_challenges(target_kind, target_id) WHERE superseded_at = '';
CREATE INDEX IF NOT EXISTS idx_mission_challenges
    ON mission_challenges(mission_id, created_at);

CREATE TABLE IF NOT EXISTS challenge_points (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    challenge_id INTEGER NOT NULL REFERENCES mission_challenges(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    text         TEXT NOT NULL,
    page_id      INTEGER          REFERENCES mission_pages(id) ON DELETE SET NULL,
    position     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_challenge_points
    ON challenge_points(challenge_id, position);
"""

#: How a profile at version N becomes a profile at version N+1.
#:
#: Adding tables to ``_SCHEMA`` alone would appear to work - every statement
#: there is IF NOT EXISTS, so an old profile picks new tables up on the next
#: launch - but only for pure additions. The first time a column has to change
#: there would be nowhere to put the ALTER, and the version stamp would have
#: been lying about what the file contains. So the ladder exists from the
#: first migration rather than from the first awkward one.
#:
#: Rules: each step is idempotent, each runs inside one transaction, and a step
#: is never edited once it has shipped - a mistake is fixed by adding the next
#: step, because someone's profile has already run the old one.
_MIGRATIONS: dict[int, str] = {
    # v1 -> v2: Missions. Identical to the block in _SCHEMA above, which is
    # what makes it safe to run on a profile that somehow already has them.
    1: """
    CREATE TABLE IF NOT EXISTS missions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        title      TEXT NOT NULL,
        goal       TEXT NOT NULL,
        status     TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_missions_updated ON missions(updated_at DESC);

    CREATE TABLE IF NOT EXISTS mission_pages (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        mission_id INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
        url        TEXT NOT NULL,
        title      TEXT NOT NULL DEFAULT '',
        source     TEXT NOT NULL DEFAULT 'agent',
        note       TEXT NOT NULL DEFAULT '',
        first_seen TEXT NOT NULL,
        last_seen  TEXT NOT NULL,
        UNIQUE(mission_id, url)
    );
    CREATE INDEX IF NOT EXISTS idx_mission_pages_mission
        ON mission_pages(mission_id, last_seen DESC);
    """,
    # v2 -> v3: Mission findings.
    2: """
    CREATE TABLE IF NOT EXISTS mission_findings (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        mission_id INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
        page_id    INTEGER          REFERENCES mission_pages(id) ON DELETE SET NULL,
        text       TEXT NOT NULL,
        key        TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(mission_id, key)
    );
    CREATE INDEX IF NOT EXISTS idx_mission_findings_mission
        ON mission_findings(mission_id, created_at);
    """,
    # v3 -> v4: soft delete. See the note in _SCHEMA.
    3: """
    ALTER TABLE missions ADD COLUMN deleted_at TEXT NOT NULL DEFAULT '';
    """,
    # v6 -> v7: the evidence graph - finding refs and decision assumptions.
    #
    # The backfill numbers existing findings per mission in created_at order,
    # with the row id as the tie-break, so two runs of this migration on the
    # same data produce the same refs. Anything else would mean a citation
    # written before an upgrade pointing somewhere else after it.
    6: """
    ALTER TABLE mission_findings ADD COLUMN ref INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE missions ADD COLUMN next_ref INTEGER NOT NULL DEFAULT 1;

    UPDATE mission_findings SET ref = (
        SELECT COUNT(*) FROM mission_findings AS earlier
        WHERE earlier.mission_id = mission_findings.mission_id
          AND (earlier.created_at < mission_findings.created_at
               OR (earlier.created_at = mission_findings.created_at
                   AND earlier.id <= mission_findings.id))
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_finding_ref
        ON mission_findings(mission_id, ref);

    UPDATE missions SET next_ref = 1 + COALESCE(
        (SELECT MAX(ref) FROM mission_findings WHERE mission_id = missions.id), 0);

    CREATE TABLE IF NOT EXISTS decision_assumptions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id INTEGER NOT NULL REFERENCES mission_decisions(id) ON DELETE CASCADE,
        text        TEXT NOT NULL,
        position    INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_decision_assumptions
        ON decision_assumptions(decision_id, position);
    """,
    # v5 -> v6: challenge mode. See the notes in _SCHEMA.
    5: """
    -- The result of trying to prove a claim wrong. Never replaces what it
    -- challenges: the original finding or decision is left as it was, and this
    -- sits beside it so the user can see both and judge.
    --
    -- target_id is a plain integer, not a foreign key, and `claim` snapshots the
    -- challenged text. A polymorphic FK would buy nothing and cost the history:
    -- delete the finding and the challenge should still say what it was made
    -- against. Append-only, like decisions.
    CREATE TABLE IF NOT EXISTS mission_challenges (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        mission_id    INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
        target_kind   TEXT NOT NULL,
        target_id     INTEGER NOT NULL,
        claim         TEXT NOT NULL,
        verdict       TEXT NOT NULL,
        summary       TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        superseded_at TEXT NOT NULL DEFAULT ''
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_one_live_challenge
        ON mission_challenges(target_kind, target_id) WHERE superseded_at = '';
    CREATE INDEX IF NOT EXISTS idx_mission_challenges
        ON mission_challenges(mission_id, created_at);
    
    CREATE TABLE IF NOT EXISTS challenge_points (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        challenge_id INTEGER NOT NULL REFERENCES mission_challenges(id) ON DELETE CASCADE,
        kind         TEXT NOT NULL,
        text         TEXT NOT NULL,
        page_id      INTEGER          REFERENCES mission_pages(id) ON DELETE SET NULL,
        position     INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_challenge_points
        ON challenge_points(challenge_id, position);
    """,
    # v4 -> v5: decision memory. See the notes in _SCHEMA.
    4: """
    -- What was decided, and the reasons a person can read. Deliberately holds no
    -- model reasoning: `rationale` is the sentence shown to the user.
    --
    -- Append-only. Editing a decision inserts a new row and stamps the old one
    -- superseded, because "we changed our mind, and here is what we used to
    -- think" is part of the record. The partial unique index makes "at most one
    -- live decision per mission" a guarantee of the database rather than a
    -- convention of the code.
    CREATE TABLE IF NOT EXISTS mission_decisions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        mission_id    INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
        decision      TEXT NOT NULL,
        rationale     TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        superseded_at TEXT NOT NULL DEFAULT ''
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_one_live_decision
        ON mission_decisions(mission_id) WHERE superseded_at = '';
    
    -- What a decision takes for granted, said out loud. User-visible data, and
-- the one part of "why" that a rationale paragraph cannot be decomposed into.
CREATE TABLE IF NOT EXISTS decision_assumptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL REFERENCES mission_decisions(id) ON DELETE CASCADE,
    text        TEXT NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_decision_assumptions
    ON decision_assumptions(decision_id, position);

CREATE TABLE IF NOT EXISTS decision_alternatives (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id INTEGER NOT NULL REFERENCES mission_decisions(id) ON DELETE CASCADE,
        name        TEXT NOT NULL,
        reason      TEXT NOT NULL,
        position    INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_decision_alternatives
        ON decision_alternatives(decision_id, position);
    
    -- Evidence is both a reference and a snapshot. The reference keeps the
    -- decision connected to the live board; the snapshot keeps it honest, because
    -- a finding edited afterwards must not silently rewrite what the decision was
    -- made on. finding_id is ON DELETE SET NULL so a deleted finding costs the
    -- link, never the record of what was believed.
    CREATE TABLE IF NOT EXISTS decision_evidence (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id INTEGER NOT NULL REFERENCES mission_decisions(id) ON DELETE CASCADE,
        finding_id  INTEGER          REFERENCES mission_findings(id) ON DELETE SET NULL,
        text        TEXT NOT NULL,
        source      TEXT NOT NULL DEFAULT '',
        position    INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_decision_evidence
        ON decision_evidence(decision_id, position);
    """,
}

_STOP = object()


class Database:
    """Owns the sqlite3 connection, plus a background writer for hot-path writes."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = self._open_or_recover()

        self._writes: queue.Queue = queue.Queue()
        self._writer = threading.Thread(
            target=self._writer_loop, name="sqlite-writer", daemon=True
        )
        self._writer.start()

    def _connect(self) -> sqlite3.Connection:
        """Open the file and configure the connection.

        sqlite3.connect() succeeds on any file - it does not read it - so a
        corrupt database first shows up at the PRAGMA below. When that happens
        the connection object still exists and still holds the file open, so it
        is closed here rather than left for the caller: on Windows an open
        handle makes the file impossible to rename or delete, which broke the
        corrupt-database recovery entirely.
        """
        conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10.0)
        try:
            conn.row_factory = sqlite3.Row
            # WAL lets a read proceed while a write is in flight - the right
            # default for a desktop app that writes on every page load.
            conn.execute("PRAGMA journal_mode=WAL")
            # With WAL, NORMAL means we fsync at checkpoints rather than on
            # every commit. The worst case is losing the last few history rows
            # after an OS crash, which is an acceptable trade for never
            # stalling the UI.
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
        except BaseException:
            conn.close()
            raise
        return conn

    def _open_or_recover(self) -> sqlite3.Connection:
        """Open the database, quarantining and recreating it if it is corrupt.

        A truncated or non-SQLite file at this path would otherwise make the
        whole application fail to start, and losing history is a far better
        outcome than a browser that will not launch. Note that the failure can
        surface as early as the first PRAGMA, so both the connect and the
        schema step are covered here.
        """
        conn = None
        try:
            conn = self._connect()
            self._apply_schema(conn)
            return conn
        except sqlite3.DatabaseError:
            # Close whatever is still open before touching the file. _connect()
            # cleans up after itself, but _apply_schema() can fail on a
            # connection that opened cleanly, and that one is ours to close.
            #
            # This matters only on Windows, and it matters completely: a file
            # with an open handle cannot be renamed or deleted there, so the
            # quarantine below raised PermissionError (WinError 32) and the
            # recovery failed - meaning a corrupt database stopped the browser
            # starting, the exact outcome this method exists to prevent. POSIX
            # allows renaming an open file, which is why it went unnoticed.
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

        # Move the unusable file aside rather than deleting it, so a user who
        # cares can still try to recover it by hand.
        quarantine = self.path.with_name(self.path.name + ".corrupt")
        try:
            self.path.replace(quarantine)
        except OSError:
            # Still unmovable - a permission problem, or an antivirus holding
            # it open. Deleting is the fallback, and if that fails too we let
            # the error out: at that point the disk is telling us something the
            # browser cannot work around, and a clear failure beats a silent
            # one.
            self.path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)

        conn = self._connect()
        self._apply_schema(conn)
        return conn

    def _apply_schema(self, conn: sqlite3.Connection) -> None:
        """Bring the file up to ``SCHEMA_VERSION``, whatever it is now.

        A fresh file reports user_version 0 and gets ``_SCHEMA`` outright. An
        existing one climbs the ladder one step at a time. Both end stamped
        with the same number, and both are safe to run repeatedly.

        This also runs on the corrupt-recovery path, where the file has just
        been recreated empty - so it must work from zero as well as from any
        shipped version.
        """
        with self._lock:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                conn.executescript(_SCHEMA)
            elif version > SCHEMA_VERSION:
                # A newer PyBrowser wrote this profile. Its tables are a
                # superset of ours, so leave the stamp alone and carry on
                # rather than downgrading a file we do not understand.
                conn.commit()
                return
            else:
                for step in range(version, SCHEMA_VERSION):
                    conn.executescript(_MIGRATIONS[step])
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()

    # -- background writer ----------------------------------------------
    def _writer_loop(self) -> None:
        while True:
            item = self._writes.get()
            try:
                if item is _STOP:
                    return
                try:
                    with self._lock:
                        if callable(item):
                            # A task that needs to read-then-write atomically
                            # runs entirely on this thread, under the lock.
                            item(self._conn)
                        else:
                            sql, params = item
                            self._conn.execute(sql, params)
                        self._conn.commit()
                except sqlite3.Error:
                    # A failed history write must never take the browser down.
                    pass
            finally:
                self._writes.task_done()

    def submit(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Queue a fire-and-forget write. Never blocks the caller."""
        if self._closed:
            return
        self._writes.put((sql, tuple(params)))

    def submit_task(self, task) -> None:
        """Queue a callable that receives the connection. Never blocks."""
        if self._closed:
            return
        self._writes.put(task)

    def flush(self, timeout: float = 5.0) -> None:
        """Wait for queued writes to land. Called before any read."""
        if self._closed:
            return
        done = threading.Event()
        # join() has no timeout, so drain via a sentinel write instead.
        self._writes.put(("SELECT 1", ()))
        deadline = threading.Timer(timeout, done.set)
        deadline.start()
        try:
            while not self._writes.empty() and not done.is_set():
                done.wait(0.002)
        finally:
            deadline.cancel()

    # -- synchronous access ----------------------------------------------
    #
    # Reads and writes after close() are no-ops rather than errors. Qt delivers
    # queued signals during shutdown - a urlChanged arriving after the database
    # has gone was enough to raise "Cannot operate on a closed database" out of
    # a UI slot and take the window down on the way out. Nothing useful can be
    # stored at that point, and crashing on exit helps nobody.
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor | None:
        if self._closed:
            return None
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        if self._closed:
            return []
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        if self._closed:
            return
        with self._lock:
            self._conn.executemany(sql, seq)
            self._conn.commit()

    def close(self) -> None:
        if self._closed:
            return
        self.flush()
        self._closed = True
        self._writes.put(_STOP)
        self._writer.join(timeout=5.0)
        with self._lock:
            self._conn.close()
