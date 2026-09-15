"""Raw SQL for Phase 21 Collaboration / Shared Missions - sharing state,
participants/roles, comments, and the activity feed. Thin and policy-free,
same shape as every other ``*Store`` in this package; app/collaboration/
is where the actual sharing/permission/merge logic lives.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.storage.database import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class Sharing:
    mission_id: int
    global_id: str
    owner_device_id: str
    folder_path: str
    since_token: str | None
    viewers_may_comment: bool
    created_at: str
    stopped_at: str | None

    @property
    def is_active(self) -> bool:
        return not self.stopped_at


@dataclass(frozen=True)
class Participant:
    mission_id: int
    device_id: str
    display_name: str
    role: str
    joined_at: str
    removed_at: str | None

    @property
    def is_active(self) -> bool:
        return not self.removed_at


@dataclass(frozen=True)
class Comment:
    id: str
    mission_id: int
    target_type: str
    target_id: str
    author_device_id: str
    author_name: str
    body: str
    created_at: str


@dataclass(frozen=True)
class ActivityEntry:
    id: str
    mission_id: int
    kind: str
    actor_device_id: str
    actor_name: str
    summary: str
    created_at: str


class MissionSharingStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    _COLUMNS = ("mission_id", "global_id", "owner_device_id", "folder_path", "since_token",
               "viewers_may_comment", "created_at", "stopped_at")

    @classmethod
    def _row_to_sharing(cls, row) -> Sharing:
        data = dict(row)
        data["viewers_may_comment"] = bool(data["viewers_may_comment"])
        return Sharing(**data)

    def start(self, mission_id: int, global_id: str, owner_device_id: str,
             folder_path: str, *, viewers_may_comment: bool = False) -> Sharing:
        self._db.execute(
            "INSERT INTO mission_collaboration (mission_id, global_id, owner_device_id, "
            "folder_path, since_token, viewers_may_comment, created_at, stopped_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, NULL) "
            "ON CONFLICT(mission_id) DO UPDATE SET global_id=excluded.global_id, "
            "owner_device_id=excluded.owner_device_id, folder_path=excluded.folder_path, "
            "viewers_may_comment=excluded.viewers_may_comment, stopped_at=NULL",
            (mission_id, global_id, owner_device_id, folder_path, int(viewers_may_comment), _now()))
        return self.get(mission_id)

    def get(self, mission_id: int) -> Sharing | None:
        row = self._db.query_one(
            f"SELECT {', '.join(self._COLUMNS)} FROM mission_collaboration WHERE mission_id = ?",
            (mission_id,))
        return self._row_to_sharing(row) if row is not None else None

    def get_by_global_id(self, global_id: str) -> Sharing | None:
        row = self._db.query_one(
            f"SELECT {', '.join(self._COLUMNS)} FROM mission_collaboration WHERE global_id = ?",
            (global_id,))
        return self._row_to_sharing(row) if row is not None else None

    def all_active(self) -> list[Sharing]:
        rows = self._db.query(
            f"SELECT {', '.join(self._COLUMNS)} FROM mission_collaboration "
            "WHERE stopped_at IS NULL")
        return [self._row_to_sharing(row) for row in rows]

    def set_since_token(self, mission_id: int, token: str) -> None:
        self._db.execute(
            "UPDATE mission_collaboration SET since_token = ? WHERE mission_id = ?",
            (token, mission_id))

    def stop(self, mission_id: int) -> None:
        self._db.execute(
            "UPDATE mission_collaboration SET stopped_at = ? WHERE mission_id = ?",
            (_now(), mission_id))


class ParticipantStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, mission_id: int, device_id: str, display_name: str, role: str) -> Participant:
        self._db.execute(
            "INSERT INTO mission_participants (mission_id, device_id, display_name, role, "
            "joined_at, removed_at) VALUES (?, ?, ?, ?, ?, NULL) "
            "ON CONFLICT(mission_id, device_id) DO UPDATE SET "
            "display_name=excluded.display_name, removed_at=NULL",
            (mission_id, device_id, display_name, role, _now()))
        return self.get(mission_id, device_id)

    def get(self, mission_id: int, device_id: str) -> Participant | None:
        row = self._db.query_one(
            "SELECT mission_id, device_id, display_name, role, joined_at, removed_at "
            "FROM mission_participants WHERE mission_id = ? AND device_id = ?",
            (mission_id, device_id))
        return Participant(**dict(row)) if row is not None else None

    def all(self, mission_id: int) -> list[Participant]:
        rows = self._db.query(
            "SELECT mission_id, device_id, display_name, role, joined_at, removed_at "
            "FROM mission_participants WHERE mission_id = ? ORDER BY joined_at", (mission_id,))
        return [Participant(**dict(row)) for row in rows]

    def apply_sync(self, mission_id: int, device_id: str, display_name: str, role: str,
                   joined_at: str, removed_at: str | None) -> Participant:
        """Write exactly the fields a synced participant record carries -
        unlike ``add()``, which always resets ``removed_at`` to NULL (the
        right behavior for *this* device registering someone locally, the
        wrong one for applying another device's already-synced state,
        which might be a removal)."""
        self._db.execute(
            "INSERT INTO mission_participants (mission_id, device_id, display_name, role, "
            "joined_at, removed_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(mission_id, device_id) DO UPDATE SET "
            "display_name=excluded.display_name, role=excluded.role, "
            "removed_at=excluded.removed_at",
            (mission_id, device_id, display_name, role, joined_at, removed_at))
        return self.get(mission_id, device_id)

    def set_role(self, mission_id: int, device_id: str, role: str) -> None:
        self._db.execute(
            "UPDATE mission_participants SET role = ? WHERE mission_id = ? AND device_id = ?",
            (role, mission_id, device_id))

    def remove(self, mission_id: int, device_id: str) -> None:
        """Part 17: marks the participant removed for *future* access -
        their row (and everything they already contributed) is kept."""
        self._db.execute(
            "UPDATE mission_participants SET removed_at = ? "
            "WHERE mission_id = ? AND device_id = ?", (_now(), mission_id, device_id))


class CommentStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, mission_id: int, target_type: str, target_id: str, author_device_id: str,
           author_name: str, body: str, *, comment_id: str | None = None,
           created_at: str | None = None) -> Comment:
        comment_id = comment_id or new_id()
        created_at = created_at or _now()
        self._db.execute(
            "INSERT OR IGNORE INTO mission_comments (id, mission_id, target_type, target_id, "
            "author_device_id, author_name, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (comment_id, mission_id, target_type, target_id, author_device_id, author_name,
             body, created_at))
        return self.get(comment_id)

    def get(self, comment_id: str) -> Comment | None:
        row = self._db.query_one(
            "SELECT id, mission_id, target_type, target_id, author_device_id, author_name, "
            "body, created_at FROM mission_comments WHERE id = ?", (comment_id,))
        return Comment(**dict(row)) if row is not None else None

    def for_mission(self, mission_id: int) -> list[Comment]:
        rows = self._db.query(
            "SELECT id, mission_id, target_type, target_id, author_device_id, author_name, "
            "body, created_at FROM mission_comments WHERE mission_id = ? ORDER BY created_at",
            (mission_id,))
        return [Comment(**dict(row)) for row in rows]

    def for_target(self, mission_id: int, target_type: str, target_id: str) -> list[Comment]:
        rows = self._db.query(
            "SELECT id, mission_id, target_type, target_id, author_device_id, author_name, "
            "body, created_at FROM mission_comments "
            "WHERE mission_id = ? AND target_type = ? AND target_id = ? ORDER BY created_at",
            (mission_id, target_type, target_id))
        return [Comment(**dict(row)) for row in rows]


class ActivityStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, mission_id: int, kind: str, actor_device_id: str, actor_name: str,
           summary: str, *, activity_id: str | None = None,
           created_at: str | None = None) -> ActivityEntry:
        activity_id = activity_id or new_id()
        created_at = created_at or _now()
        self._db.execute(
            "INSERT OR IGNORE INTO mission_activity (id, mission_id, kind, actor_device_id, "
            "actor_name, summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (activity_id, mission_id, kind, actor_device_id, actor_name, summary, created_at))
        return self.get(activity_id)

    def get(self, activity_id: str) -> ActivityEntry | None:
        row = self._db.query_one(
            "SELECT id, mission_id, kind, actor_device_id, actor_name, summary, created_at "
            "FROM mission_activity WHERE id = ?", (activity_id,))
        return ActivityEntry(**dict(row)) if row is not None else None

    def for_mission(self, mission_id: int, *, limit: int = 200) -> list[ActivityEntry]:
        rows = self._db.query(
            "SELECT id, mission_id, kind, actor_device_id, actor_name, summary, created_at "
            "FROM mission_activity WHERE mission_id = ? ORDER BY created_at DESC LIMIT ?",
            (mission_id, limit))
        return [ActivityEntry(**dict(row)) for row in rows]
