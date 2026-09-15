"""SyncAdapters for Phase 21's two collaboration record types - reusing
app.sync.engine.SyncAdapter's exact four-method contract, the same as
every Phase 20 adapter. Both are scoped to one Mission (the collaboration
SyncEngine syncs exactly one shared Mission's records, never a whole
profile - see app/collaboration/service.py) and both use
``deterministic_ids``: a comment/activity row's own id is already a
uuid assigned at creation, so it IS the sync global id - no separate
id-mapping table needed, the same trick app.sync.adapters.SettingsAdapter
uses for its singleton.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.sync.engine import SyncAdapter
from app.sync.types import RecordType

if TYPE_CHECKING:
    from app.storage.collaboration_store import ActivityStore, CommentStore, ParticipantStore


class ParticipantAdapter(SyncAdapter):
    """Who is on a shared Mission and what role they hold - synced so
    every device (not just the owner's) sees the current participant
    list, and so a removal or role change made on one device reaches the
    others (Part 2/14/17). A participant row's own natural key (mission,
    device) is already globally meaningful - device ids are unique per
    Phase 20 device identity - so this uses deterministic ids exactly
    like CommentAdapter/ActivityAdapter, keyed by device_id rather than a
    generated uuid."""

    deterministic_ids = True
    record_type = RecordType.MISSION_PARTICIPANT

    def __init__(self, store: "ParticipantStore", mission_id: int) -> None:
        self._store = store
        self._mission_id = mission_id

    def iter_local_ids(self) -> list[str]:
        return [p.device_id for p in self._store.all(self._mission_id)]

    def build_payload(self, local_id: str) -> dict | None:
        participant = self._store.get(self._mission_id, local_id)
        if participant is None:
            return None
        return {
            "display_name": participant.display_name, "role": participant.role,
            "joined_at": participant.joined_at, "removed_at": participant.removed_at,
        }

    def apply_create_or_update(self, payload: dict, *, local_id: str | None,
                              global_id: str = "") -> str | None:
        device_id = local_id or global_id
        if not device_id:
            return None
        participant = self._store.apply_sync(
            self._mission_id, device_id, payload.get("display_name", ""),
            payload.get("role", ""), payload.get("joined_at", ""), payload.get("removed_at"))
        return participant.device_id

    def apply_delete(self, local_id: str) -> None:
        # A participant is never deleted by peer sync, only marked
        # removed (a normal field update, handled above) - their prior
        # membership is a fact of history, not something to erase.
        pass

    def in_scope(self, local_id: str, global_id: str) -> bool:
        return self._store.get(self._mission_id, local_id) is not None


class CommentAdapter(SyncAdapter):
    deterministic_ids = True
    record_type = RecordType.MISSION_COMMENT

    def __init__(self, store: "CommentStore", mission_id: int) -> None:
        self._store = store
        self._mission_id = mission_id

    def iter_local_ids(self) -> list[str]:
        return [c.id for c in self._store.for_mission(self._mission_id)]

    def build_payload(self, local_id: str) -> dict | None:
        comment = self._store.get(local_id)
        if comment is None or comment.mission_id != self._mission_id:
            return None
        return {
            "target_type": comment.target_type, "target_id": comment.target_id,
            "author_device_id": comment.author_device_id, "author_name": comment.author_name,
            "body": comment.body, "created_at": comment.created_at,
        }

    def apply_create_or_update(self, payload: dict, *, local_id: str | None,
                              global_id: str = "") -> str | None:
        comment_id = local_id or global_id
        if not comment_id:
            return None
        comment = self._store.add(
            self._mission_id, payload.get("target_type", ""), payload.get("target_id", ""),
            payload.get("author_device_id", ""), payload.get("author_name", ""),
            payload.get("body", ""), comment_id=comment_id,
            created_at=payload.get("created_at"))
        return comment.id

    def apply_delete(self, local_id: str) -> None:
        # Comments are never deleted by a peer's sync in this phase - a
        # comment is a record of something someone said, kept even after
        # a participant is removed (Part 17: their prior contribution is
        # not retroactively erased).
        pass

    def in_scope(self, local_id: str, global_id: str) -> bool:
        comment = self._store.get(local_id)
        return comment is not None and comment.mission_id == self._mission_id


class ActivityAdapter(SyncAdapter):
    deterministic_ids = True
    record_type = RecordType.MISSION_ACTIVITY

    def __init__(self, store: "ActivityStore", mission_id: int) -> None:
        self._store = store
        self._mission_id = mission_id

    def iter_local_ids(self) -> list[str]:
        return [a.id for a in self._store.for_mission(self._mission_id, limit=100_000)]

    def build_payload(self, local_id: str) -> dict | None:
        entry = self._store.get(local_id)
        if entry is None or entry.mission_id != self._mission_id:
            return None
        return {
            "kind": entry.kind, "actor_device_id": entry.actor_device_id,
            "actor_name": entry.actor_name, "summary": entry.summary,
            "created_at": entry.created_at,
        }

    def apply_create_or_update(self, payload: dict, *, local_id: str | None,
                              global_id: str = "") -> str | None:
        activity_id = local_id or global_id
        if not activity_id:
            return None
        entry = self._store.add(
            self._mission_id, payload.get("kind", ""), payload.get("actor_device_id", ""),
            payload.get("actor_name", ""), payload.get("summary", ""),
            activity_id=activity_id, created_at=payload.get("created_at"))
        return entry.id

    def apply_delete(self, local_id: str) -> None:
        pass

    def in_scope(self, local_id: str, global_id: str) -> bool:
        entry = self._store.get(local_id)
        return entry is not None and entry.mission_id == self._mission_id
