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
from typing import Any, Callable, Iterable, Sequence

SCHEMA_VERSION = 22

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

-- A highlight outlives the page it came from on purpose: url/title/text are
-- copied in at save time, not looked up later, so a saved highlight is still
-- usable as context long after the original page has changed or vanished.
CREATE TABLE IF NOT EXISTS highlights (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    text       TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_highlights_created_at ON highlights(created_at DESC);

-- Custom Skills only - a built-in Skill (app/agent/skills.py's
-- BUILTIN_SKILLS) is a Python constant and never has a row here. That is
-- the entire "built-in Skills are immutable" guarantee: there is nothing
-- in this table to edit or delete for one, only to duplicate into a new
-- custom row.
CREATE TABLE IF NOT EXISTS skills (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    instructions        TEXT NOT NULL DEFAULT '',
    allowed_tools       TEXT,                 -- JSON list, or NULL = unrestricted
    output_schema       TEXT,                 -- JSON object, or NULL
    preferred_provider  TEXT NOT NULL DEFAULT '',
    preferred_model     TEXT NOT NULL DEFAULT '',
    default_context_kinds TEXT NOT NULL DEFAULT '[]',
    schema_version      INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

-- Scheduled Missions (Phase 7): a scheduling layer on top of the Mission
-- system, not a second one - see app/missions/scheduler.py and
-- app/missions/task_runner.py. mission_id is nullable because a schedule
-- can be created before the Mission it will start exists yet.
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id          INTEGER,
    mission_title       TEXT NOT NULL DEFAULT '',
    goal                TEXT NOT NULL,
    schedule_kind       TEXT NOT NULL,
    schedule_at         TEXT,
    time_of_day         TEXT,
    weekday             INTEGER,
    interval_seconds    INTEGER,
    state               TEXT NOT NULL DEFAULT 'queued',
    next_run_at         TEXT,
    last_run_at         TEXT,
    last_duration_s     REAL,
    last_error          TEXT,
    write_attempted     INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    FOREIGN KEY (mission_id) REFERENCES missions(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_next_run ON scheduled_tasks(next_run_at);

-- One row per fire, kept even after scheduled_tasks itself changes - the
-- audit trail a single task row's last_run_at/last_error cannot show.
CREATE TABLE IF NOT EXISTS task_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    outcome     TEXT NOT NULL DEFAULT 'running',
    error       TEXT,
    FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_task_runs_task ON task_runs(task_id, started_at DESC);

-- Page Watches (Phase 8): monitor a page or a selected section of one over
-- time - see app/watches/. Never a full-page copy: baseline/last-observed
-- are a hash plus a small condition-specific derived value (a number, a
-- true/false, or a short preview), not the page itself.
CREATE TABLE IF NOT EXISTS watches (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    title                 TEXT NOT NULL,
    url                   TEXT NOT NULL,
    target_type           TEXT NOT NULL,
    selection_hint        TEXT NOT NULL DEFAULT '',
    condition             TEXT NOT NULL,
    condition_value       TEXT NOT NULL DEFAULT '',
    check_interval_seconds INTEGER NOT NULL,
    baseline_hash         TEXT,
    baseline_value        TEXT,
    last_observed_hash    TEXT,
    last_observed_value   TEXT,
    last_checked_at       TEXT,
    next_check_at         TEXT,
    state                 TEXT NOT NULL DEFAULT 'active',
    failure_count         INTEGER NOT NULL DEFAULT 0,
    mission_id            INTEGER,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL,
    FOREIGN KEY (mission_id) REFERENCES missions(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_watches_next_check ON watches(next_check_at);

-- One row per meaningful change only - never one per check. Retention is
-- enforced in code (WatchStore trims to the newest N per watch), not here.
CREATE TABLE IF NOT EXISTS watch_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id    INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    summary     TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    FOREIGN KEY (watch_id) REFERENCES watches(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_watch_history_watch ON watch_history(watch_id, observed_at DESC);

-- Mission Execution Graph (Phase 10): the persisted, restart-safe shape of
-- the plan MissionCoordinator builds - see app/missions/graph.py and
-- app/missions/coordinator.py. dependencies is a JSON array of other node
-- ids in this same table; resolving "is a node ready" is a live scheduling
-- question the coordinator answers itself, never computed here.
CREATE TABLE IF NOT EXISTS mission_graph_nodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id      INTEGER NOT NULL,
    node_type       TEXT NOT NULL,
    role            TEXT NOT NULL,
    title           TEXT NOT NULL,
    instructions    TEXT NOT NULL,
    dependencies    TEXT NOT NULL DEFAULT '[]',
    state           TEXT NOT NULL DEFAULT 'pending',
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    write_attempted INTEGER NOT NULL DEFAULT 0,
    result_summary  TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT '',
    findings_added  INTEGER NOT NULL DEFAULT 0,
    plan_round      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    completed_at    TEXT,
    FOREIGN KEY (mission_id) REFERENCES missions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mission_graph_nodes_mission ON mission_graph_nodes(mission_id);

-- PyBrowser-as-MCP-Server (Phase 11): paired external clients and the audit
-- trail of what they did. token_hash is a SHA-256 hex digest - the plaintext
-- pairing token is shown to the user exactly once and never persisted, see
-- app/mcp_server/auth.py. capabilities is a JSON array of capability names.
-- client_type/connection_method are Phase 12 (External AI Clients) metadata -
-- which client card paired this client and how it connects. Purely
-- descriptive: every client, regardless of type, uses the SAME auth/
-- permission/audit machinery - see app/mcp_server/client_configs.py.
-- last_verified_* records the outcome of the one shared verification
-- engine (app/mcp_server/verification.py) - "token created" is never
-- conflated with "connected".
CREATE TABLE IF NOT EXISTS mcp_server_clients (
    id                  TEXT PRIMARY KEY,
    display_name        TEXT NOT NULL,
    token_hash          TEXT NOT NULL,
    capabilities        TEXT NOT NULL DEFAULT '[]',
    created_at          TEXT NOT NULL,
    last_used_at        TEXT,
    revoked             INTEGER NOT NULL DEFAULT 0,
    client_type         TEXT NOT NULL DEFAULT 'generic',
    connection_method   TEXT NOT NULL DEFAULT '',
    last_verified_at    TEXT,
    last_verified_status TEXT NOT NULL DEFAULT 'not_configured'
);

-- detail is a short, redacted, human-readable summary - never a raw secret
-- or a full page payload, see app/mcp_server/audit.py.
CREATE TABLE IF NOT EXISTS mcp_server_audit (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id        TEXT,
    tool             TEXT NOT NULL,
    outcome          TEXT NOT NULL,
    duration_ms      INTEGER NOT NULL DEFAULT 0,
    approval_result  TEXT,
    detail           TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcp_server_audit_created_at ON mcp_server_audit(created_at DESC);

-- Semantic History / Local RAG (Phase 13) - see app/knowledge/. Off by
-- default (settings.semantic_history_enabled); every row here comes from
-- content the user already chose to keep (history, Missions, findings,
-- highlights, PDFs/files explicitly added) - never a raw arbitrary page
-- body. embedding is a JSON array from a local, dependency-free hashed
-- bag-of-words vector (app/knowledge/embeddings.py) - nothing leaves the
-- machine. content_hash lets re-indexing skip unchanged chunks.
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type   TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    parent_id     TEXT,
    chunk_index   INTEGER NOT NULL DEFAULT 0,
    title         TEXT NOT NULL DEFAULT '',
    location      TEXT NOT NULL DEFAULT '',
    text          TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    embedding     TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_source
    ON knowledge_chunks(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_parent ON knowledge_chunks(parent_id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Missions: a goal the user is working on, and the pages that served it.
-- Pages are addressed by URL, never by tab id: a tab id is an in-memory
-- counter that means nothing after a restart, and holding one would make a
-- mission corruptible by closing a tab.
-- parent_id/branch_name: a Mission may branch from another - "Trip Plan" into
-- "Budget", "Comfort", "Fastest" - each evolving independently from there.
-- ON DELETE SET NULL rather than CASCADE: deleting a parent must not take its
-- branches down with it: each branch is a full copy of the state it branched
-- from, not a view onto the parent, so it stands on its own.
--
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
    next_ref   INTEGER NOT NULL DEFAULT 1,
    parent_id  INTEGER REFERENCES missions(id) ON DELETE SET NULL,
    branch_name TEXT NOT NULL DEFAULT '',
    progress   TEXT NOT NULL DEFAULT '',
    result     TEXT NOT NULL DEFAULT '',
    follow_ups TEXT NOT NULL DEFAULT '[]',
    constraints TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_missions_updated ON missions(updated_at DESC);

CREATE TABLE IF NOT EXISTS mission_pages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    url        TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'agent',
    note       TEXT NOT NULL DEFAULT '',
    outcome    TEXT NOT NULL DEFAULT '',
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

-- What the mission has not settled yet - see MissionQuestion. UNIQUE only
-- while a question is genuinely a duplicate is not enforceable in plain SQL
-- (the same wording answered once and raised again is legitimate), so
-- dedup happens in the repository layer against OPEN rows only, not here.
CREATE TABLE IF NOT EXISTS mission_questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id  INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    text        TEXT NOT NULL,
    key         TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',
    answer      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    answered_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mission_questions_mission
    ON mission_questions(mission_id, created_at);

-- What Py did, or tried to do - the persisted twin of AgentSession's
-- transient Step. page_id is ON DELETE SET NULL: losing the page a step
-- touched costs the "open this tab" link, never the record that it happened.
CREATE TABLE IF NOT EXISTS mission_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id  INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    tool_name   TEXT NOT NULL DEFAULT '',
    outcome     TEXT NOT NULL DEFAULT 'done',
    page_id     INTEGER          REFERENCES mission_pages(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mission_actions_mission
    ON mission_actions(mission_id, created_at);

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
-- A taught sequence of the agent's own tool calls, saved while "Teach Py" was
-- active, so it can be replayed later with different inputs. Belongs to a
-- Mission, like everything else the agent produces.
CREATE TABLE IF NOT EXISTS routines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_routines_mission ON routines(mission_id, created_at);

CREATE TABLE IF NOT EXISTS routine_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    routine_id  INTEGER NOT NULL REFERENCES routines(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0,
    tool_name   TEXT NOT NULL,
    args        TEXT NOT NULL DEFAULT '{}',
    description TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_routine_steps ON routine_steps(routine_id, position);
-- A written prediction of what choosing one option would lead to, made
-- BEFORE anything is done - so options can be compared before one is picked.
-- Simulate first, execute second: this table is the simulation. Writing one
-- never performs the option it describes and is never evidence of
-- permission - see the trust note in app/agent/prompt.py.
CREATE TABLE IF NOT EXISTS mission_ghost_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    option     TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'medium',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ghost_runs_mission
    ON mission_ghost_runs(mission_id, created_at);

CREATE TABLE IF NOT EXISTS ghost_run_effects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ghost_run_id INTEGER NOT NULL REFERENCES mission_ghost_runs(id) ON DELETE CASCADE,
    text         TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'neutral',
    position     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ghost_run_effects
    ON ghost_run_effects(ghost_run_id, position);
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
def _migrate_20_add_client_columns(conn: sqlite3.Connection) -> None:
    """Add mcp_server_clients' Phase 12 columns one at a time, tolerating
    a column that already exists.

    Ordinarily these are genuinely new columns on an existing Phase 11
    profile. But a *test* profile built via a fresh ``Database(path)``
    call already gets the current, final table shape (client_type and
    friends included) from ``_SCHEMA`` - such a test then winds the file
    back to an older ``user_version`` without necessarily dropping this
    unrelated, newer table first (mcp_server_clients did not exist at all
    at that older version, so a real profile from that era never has this
    conflict). Replaying every migration from scratch then hits an
    "ALTER TABLE ... ADD COLUMN" for a column that is already there.
    SQLite's ADD COLUMN has no "IF NOT EXISTS" form (unlike CREATE TABLE),
    so this is done in Python instead of a single executescript string -
    every other migration in this dict either creates a table (naturally
    idempotent via IF NOT EXISTS) or alters a table the wind-back tests
    already restore to its older, column-less shape.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(mcp_server_clients)")}
    for column, definition in (
        ("client_type", "TEXT NOT NULL DEFAULT 'generic'"),
        ("connection_method", "TEXT NOT NULL DEFAULT ''"),
        ("last_verified_at", "TEXT"),
        ("last_verified_status", "TEXT NOT NULL DEFAULT 'not_configured'"),
    ):
        if column not in existing:
            conn.execute(f"ALTER TABLE mcp_server_clients ADD COLUMN {column} {definition}")


_MIGRATIONS: dict[int, str | Callable[[sqlite3.Connection], None]] = {
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
    # v9 -> v10: ghost runs (Reality Engine).
    9: """
-- A written prediction of what choosing one option would lead to, made
-- BEFORE anything is done - so options can be compared before one is picked.
-- Simulate first, execute second: this table is the simulation. Writing one
-- never performs the option it describes and is never evidence of
-- permission - see the trust note in app/agent/prompt.py.
CREATE TABLE IF NOT EXISTS mission_ghost_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    option     TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'medium',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ghost_runs_mission
    ON mission_ghost_runs(mission_id, created_at);

CREATE TABLE IF NOT EXISTS ghost_run_effects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ghost_run_id INTEGER NOT NULL REFERENCES mission_ghost_runs(id) ON DELETE CASCADE,
    text         TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'neutral',
    position     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ghost_run_effects
    ON ghost_run_effects(ghost_run_id, position);
""",
    # v8 -> v9: branching. parent_id/branch_name on missions.
    8: """
    ALTER TABLE missions ADD COLUMN parent_id INTEGER REFERENCES missions(id) ON DELETE SET NULL;
    ALTER TABLE missions ADD COLUMN branch_name TEXT NOT NULL DEFAULT '';
    """,
    # v7 -> v8: routines (Teach Py).
    7: """
-- A taught sequence of the agent's own tool calls, saved while "Teach Py" was
-- active, so it can be replayed later with different inputs. Belongs to a
-- Mission, like everything else the agent produces.
CREATE TABLE IF NOT EXISTS routines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_routines_mission ON routines(mission_id, created_at);

CREATE TABLE IF NOT EXISTS routine_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    routine_id  INTEGER NOT NULL REFERENCES routines(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0,
    tool_name   TEXT NOT NULL,
    args        TEXT NOT NULL DEFAULT '{}',
    description TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_routine_steps ON routine_steps(routine_id, position);
-- A written prediction of what choosing one option would lead to, made
-- BEFORE anything is done - so options can be compared before one is picked.
-- Simulate first, execute second: this table is the simulation. Writing one
-- never performs the option it describes and is never evidence of
-- permission - see the trust note in app/agent/prompt.py.
CREATE TABLE IF NOT EXISTS mission_ghost_runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    option     TEXT NOT NULL,
    confidence TEXT NOT NULL DEFAULT 'medium',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ghost_runs_mission
    ON mission_ghost_runs(mission_id, created_at);

CREATE TABLE IF NOT EXISTS ghost_run_effects (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ghost_run_id INTEGER NOT NULL REFERENCES mission_ghost_runs(id) ON DELETE CASCADE,
    text         TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'neutral',
    position     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ghost_run_effects
    ON ghost_run_effects(ghost_run_id, position);
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
    # v10 -> v11: mission progress/result/follow_ups, and a persisted action
    # log (mission_actions) - the durable twin of AgentSession's Step.
    10: """
    ALTER TABLE missions ADD COLUMN progress TEXT NOT NULL DEFAULT '';
    ALTER TABLE missions ADD COLUMN result TEXT NOT NULL DEFAULT '';
    ALTER TABLE missions ADD COLUMN follow_ups TEXT NOT NULL DEFAULT '[]';

    CREATE TABLE IF NOT EXISTS mission_actions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        mission_id  INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
        description TEXT NOT NULL,
        tool_name   TEXT NOT NULL DEFAULT '',
        outcome     TEXT NOT NULL DEFAULT 'done',
        page_id     INTEGER          REFERENCES mission_pages(id) ON DELETE SET NULL,
        created_at  TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_mission_actions_mission
        ON mission_actions(mission_id, created_at);
    """,
    # v11 -> v12: Mission questions - what a mission has not settled yet,
    # tracked apart from findings. Identical to the block in _SCHEMA above.
    11: """
    CREATE TABLE IF NOT EXISTS mission_questions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        mission_id  INTEGER NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
        text        TEXT NOT NULL,
        key         TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'open',
        answer      TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        answered_at TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_mission_questions_mission
        ON mission_questions(mission_id, created_at);
    """,
    12: """
    ALTER TABLE mission_pages ADD COLUMN outcome TEXT NOT NULL DEFAULT '';
    """,
    13: """
    ALTER TABLE missions ADD COLUMN constraints TEXT NOT NULL DEFAULT '[]';
    """,
    14: """
    CREATE TABLE IF NOT EXISTS highlights (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        url        TEXT NOT NULL,
        title      TEXT NOT NULL DEFAULT '',
        text       TEXT NOT NULL,
        note       TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_highlights_created_at ON highlights(created_at DESC);
    """,
    15: """
    CREATE TABLE IF NOT EXISTS skills (
        id                  TEXT PRIMARY KEY,
        name                TEXT NOT NULL,
        description         TEXT NOT NULL DEFAULT '',
        instructions        TEXT NOT NULL DEFAULT '',
        allowed_tools       TEXT,
        output_schema       TEXT,
        preferred_provider  TEXT NOT NULL DEFAULT '',
        preferred_model     TEXT NOT NULL DEFAULT '',
        default_context_kinds TEXT NOT NULL DEFAULT '[]',
        schema_version      INTEGER NOT NULL DEFAULT 1,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL
    );
    """,
    16: """
    CREATE TABLE IF NOT EXISTS scheduled_tasks (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        mission_id          INTEGER,
        mission_title       TEXT NOT NULL DEFAULT '',
        goal                TEXT NOT NULL,
        schedule_kind       TEXT NOT NULL,
        schedule_at         TEXT,
        time_of_day         TEXT,
        weekday             INTEGER,
        interval_seconds    INTEGER,
        state               TEXT NOT NULL DEFAULT 'queued',
        next_run_at         TEXT,
        last_run_at         TEXT,
        last_duration_s     REAL,
        last_error          TEXT,
        write_attempted     INTEGER NOT NULL DEFAULT 0,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL,
        FOREIGN KEY (mission_id) REFERENCES missions(id) ON DELETE SET NULL
    );
    CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_next_run ON scheduled_tasks(next_run_at);

    -- One row per fire, kept even after scheduled_tasks itself changes -
    -- the audit trail a single task row's last_run_at/last_error cannot
    -- show (only the most recent run, not the history of every run).
    CREATE TABLE IF NOT EXISTS task_runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id     INTEGER NOT NULL,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        outcome     TEXT NOT NULL DEFAULT 'running',
        error       TEXT,
        FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_task_runs_task ON task_runs(task_id, started_at DESC);
    """,
    17: """
    CREATE TABLE IF NOT EXISTS watches (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        title                 TEXT NOT NULL,
        url                   TEXT NOT NULL,
        target_type           TEXT NOT NULL,
        selection_hint        TEXT NOT NULL DEFAULT '',
        condition             TEXT NOT NULL,
        condition_value       TEXT NOT NULL DEFAULT '',
        check_interval_seconds INTEGER NOT NULL,
        baseline_hash         TEXT,
        baseline_value        TEXT,
        last_observed_hash    TEXT,
        last_observed_value   TEXT,
        last_checked_at       TEXT,
        next_check_at         TEXT,
        state                 TEXT NOT NULL DEFAULT 'active',
        failure_count         INTEGER NOT NULL DEFAULT 0,
        mission_id            INTEGER,
        created_at            TEXT NOT NULL,
        updated_at            TEXT NOT NULL,
        FOREIGN KEY (mission_id) REFERENCES missions(id) ON DELETE SET NULL
    );
    CREATE INDEX IF NOT EXISTS idx_watches_next_check ON watches(next_check_at);

    -- One row per meaningful change only - never one per check. Retention
    -- is enforced in code (WatchStore trims to the newest N per watch).
    CREATE TABLE IF NOT EXISTS watch_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        watch_id    INTEGER NOT NULL,
        observed_at TEXT NOT NULL,
        summary     TEXT NOT NULL,
        old_value   TEXT,
        new_value   TEXT,
        FOREIGN KEY (watch_id) REFERENCES watches(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_watch_history_watch ON watch_history(watch_id, observed_at DESC);
    """,
    18: """
    CREATE TABLE IF NOT EXISTS mission_graph_nodes (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        mission_id      INTEGER NOT NULL,
        node_type       TEXT NOT NULL,
        role            TEXT NOT NULL,
        title           TEXT NOT NULL,
        instructions    TEXT NOT NULL,
        dependencies    TEXT NOT NULL DEFAULT '[]',
        state           TEXT NOT NULL DEFAULT 'pending',
        attempt_count   INTEGER NOT NULL DEFAULT 0,
        write_attempted INTEGER NOT NULL DEFAULT 0,
        result_summary  TEXT NOT NULL DEFAULT '',
        error           TEXT NOT NULL DEFAULT '',
        findings_added  INTEGER NOT NULL DEFAULT 0,
        plan_round      INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL,
        started_at      TEXT,
        completed_at    TEXT,
        FOREIGN KEY (mission_id) REFERENCES missions(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_mission_graph_nodes_mission ON mission_graph_nodes(mission_id);
    """,
    19: """
    CREATE TABLE IF NOT EXISTS mcp_server_clients (
        id             TEXT PRIMARY KEY,
        display_name   TEXT NOT NULL,
        token_hash     TEXT NOT NULL,
        capabilities   TEXT NOT NULL DEFAULT '[]',
        created_at     TEXT NOT NULL,
        last_used_at   TEXT,
        revoked        INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS mcp_server_audit (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id        TEXT,
        tool             TEXT NOT NULL,
        outcome          TEXT NOT NULL,
        duration_ms      INTEGER NOT NULL DEFAULT 0,
        approval_result  TEXT,
        detail           TEXT NOT NULL DEFAULT '',
        created_at       TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_mcp_server_audit_created_at ON mcp_server_audit(created_at DESC);
    """,
    20: _migrate_20_add_client_columns,
    21: """
    CREATE TABLE IF NOT EXISTS knowledge_chunks (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        source_type   TEXT NOT NULL,
        source_id     TEXT NOT NULL,
        parent_id     TEXT,
        chunk_index   INTEGER NOT NULL DEFAULT 0,
        title         TEXT NOT NULL DEFAULT '',
        location      TEXT NOT NULL DEFAULT '',
        text          TEXT NOT NULL,
        content_hash  TEXT NOT NULL,
        timestamp     TEXT NOT NULL,
        embedding     TEXT NOT NULL,
        created_at    TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_source
        ON knowledge_chunks(source_type, source_id);
    CREATE INDEX IF NOT EXISTS idx_knowledge_chunks_parent ON knowledge_chunks(parent_id);
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
                    migration = _MIGRATIONS[step]
                    if callable(migration):
                        migration(conn)
                    else:
                        conn.executescript(migration)
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
