"""Conflict policy (Part 9/10) - "start with simple rules". Every syncable
record type gets exactly one policy, chosen once here rather than left to
each adapter to decide differently:

* ``KEEP_BOTH`` - append-oriented records that merge cleanly by their own
  stable id (Mission findings, Highlights): a genuine same-record edit on
  two devices is rare, and duplicating rather than guessing which edit to
  drop is always safe.
* ``NEWER_WINS`` - records where a lost concurrent edit is low-stakes and
  "the more recent one, by wall clock, is a reasonable default" (Workspace
  tab/group metadata, Scheduled-task/Watch *definitions* - never their
  ownership/execution-device field, which is handled separately, see
  app/sync/adapters.py).
* ``MANUAL`` - a genuine, user-meaningful edit that must never be silently
  dropped (Mission goal/result, Skill instructions/output schema): surfaced
  in Settings -> Sync for the user to pick Keep this device / Keep other
  device / Keep both (Part 9's own three options).
"""

from __future__ import annotations

from app.sync.types import RecordType


class ConflictPolicy:
    KEEP_BOTH = "keep_both"
    NEWER_WINS = "newer_wins"
    MANUAL = "manual"


POLICY_BY_RECORD_TYPE: dict[str, str] = {
    RecordType.MISSION: ConflictPolicy.MANUAL,
    RecordType.MISSION_FINDING: ConflictPolicy.KEEP_BOTH,
    RecordType.HIGHLIGHT: ConflictPolicy.KEEP_BOTH,
    RecordType.SKILL: ConflictPolicy.MANUAL,
    RecordType.WORKSPACE: ConflictPolicy.NEWER_WINS,
    RecordType.SCHEDULED_TASK: ConflictPolicy.NEWER_WINS,
    RecordType.WATCH: ConflictPolicy.NEWER_WINS,
    RecordType.GRAPH_NODE: ConflictPolicy.NEWER_WINS,
    RecordType.GRAPH_EDGE: ConflictPolicy.NEWER_WINS,
    RecordType.SETTINGS: ConflictPolicy.NEWER_WINS,
    # Phase 21 - append-only/immutable once created, same as findings/highlights.
    RecordType.MISSION_COMMENT: ConflictPolicy.KEEP_BOTH,
    RecordType.MISSION_ACTIVITY: ConflictPolicy.KEEP_BOTH,
    #: A role change or removal only ever comes from the owner, so a real
    #: concurrent edit is rare; "the most recent write wins" is a
    #: reasonable default rather than building manual resolution for a
    #: single-writer field (Part 9: "start with simple rules").
    RecordType.MISSION_PARTICIPANT: ConflictPolicy.NEWER_WINS,
}


def policy_for(record_type: str) -> str:
    return POLICY_BY_RECORD_TYPE.get(record_type, ConflictPolicy.MANUAL)


class Resolution:
    KEEP_LOCAL = "keep_local"
    KEEP_REMOTE = "keep_remote"
    KEEP_BOTH = "keep_both"
