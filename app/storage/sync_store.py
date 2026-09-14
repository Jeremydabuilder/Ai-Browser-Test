"""Raw SQL for Phase 20 Encrypted Sync - device registry, the local-id <->
global-id mapping, per-record merge markers, tombstones, and pending manual
conflicts. Thin and policy-free, the same shape every other ``*Store`` in
this package already uses; app/sync/ is where the actual sync logic lives.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.storage.database import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SyncDeviceStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def upsert(self, device_id: str, name: str, *, last_sync_at: str | None = None) -> None:
        existing = self.get(device_id)
        created_at = existing["created_at"] if existing is not None else _now()
        self._db.execute(
            "INSERT INTO sync_devices (id, name, created_at, last_sync_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "last_sync_at=COALESCE(excluded.last_sync_at, sync_devices.last_sync_at)",
            (device_id, name, created_at, last_sync_at))

    def get(self, device_id: str) -> dict[str, Any] | None:
        row = self._db.query_one(
            "SELECT id, name, created_at, last_sync_at FROM sync_devices WHERE id = ?",
            (device_id,))
        return dict(row) if row is not None else None

    def rename(self, device_id: str, name: str) -> None:
        self._db.execute("UPDATE sync_devices SET name = ? WHERE id = ?", (name, device_id))

    def touch_last_sync(self, device_id: str) -> None:
        self._db.execute(
            "UPDATE sync_devices SET last_sync_at = ? WHERE id = ?", (_now(), device_id))

    def all(self) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT id, name, created_at, last_sync_at FROM sync_devices ORDER BY created_at")
        return [dict(row) for row in rows]

    def remove(self, device_id: str) -> None:
        self._db.execute("DELETE FROM sync_devices WHERE id = ?", (device_id,))


class GlobalIdStore:
    """Local id <-> stable global id, per record type. A global id is
    allocated exactly once per local row and never regenerated - see the
    module docstring."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def get_global_id(self, record_type: str, local_id: str) -> str | None:
        row = self._db.query_one(
            "SELECT global_id FROM sync_global_ids WHERE record_type = ? AND local_id = ?",
            (record_type, str(local_id)))
        return row["global_id"] if row is not None else None

    def get_local_id(self, record_type: str, global_id: str) -> str | None:
        row = self._db.query_one(
            "SELECT local_id FROM sync_global_ids WHERE record_type = ? AND global_id = ?",
            (record_type, global_id))
        return row["local_id"] if row is not None else None

    def ensure_global_id(self, record_type: str, local_id: str) -> str:
        existing = self.get_global_id(record_type, local_id)
        if existing is not None:
            return existing
        new_id = str(uuid.uuid4())
        self.bind(record_type, local_id, new_id)
        return new_id

    def bind(self, record_type: str, local_id: str, global_id: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO sync_global_ids (record_type, local_id, global_id) "
            "VALUES (?, ?, ?)", (record_type, str(local_id), global_id))

    def unbind_local(self, record_type: str, local_id: str) -> None:
        self._db.execute(
            "DELETE FROM sync_global_ids WHERE record_type = ? AND local_id = ?",
            (record_type, str(local_id)))

    def all_for_type(self, record_type: str) -> list[tuple[str, str]]:
        rows = self._db.query(
            "SELECT local_id, global_id FROM sync_global_ids WHERE record_type = ?",
            (record_type,))
        return [(row["local_id"], row["global_id"]) for row in rows]


class SyncVersionStore:
    """The 3-way-merge marker (last_known_hash) plus Part 22's rollback/
    replay guard: local_version is this device's own monotonic counter for
    the record (bumped every local change, sent as part of the encrypted
    package's AAD - see app/sync/crypto.py); last_remote_version is the
    highest remote version this device has ever accepted for that record,
    so a provider that replays an old encrypted blob is refused rather
    than silently un-doing a later change."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def _row(self, record_type: str, global_id: str) -> dict[str, Any] | None:
        row = self._db.query_one(
            "SELECT last_known_hash, local_version, last_remote_version FROM sync_versions "
            "WHERE record_type = ? AND global_id = ?", (record_type, global_id))
        return dict(row) if row is not None else None

    def get_last_known_hash(self, record_type: str, global_id: str) -> str | None:
        row = self._row(record_type, global_id)
        return row["last_known_hash"] if row is not None else None

    def get_local_version(self, record_type: str, global_id: str) -> int:
        row = self._row(record_type, global_id)
        return int(row["local_version"]) if row is not None else 0

    def get_last_remote_version(self, record_type: str, global_id: str) -> int:
        row = self._row(record_type, global_id)
        return int(row["last_remote_version"]) if row is not None else 0

    def _ensure_row(self, record_type: str, global_id: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO sync_versions "
            "(record_type, global_id, last_known_hash, local_version, last_remote_version, "
            " updated_at) VALUES (?, ?, '', 0, 0, ?)", (record_type, global_id, _now()))

    def bump_local_version(self, record_type: str, global_id: str) -> int:
        self._ensure_row(record_type, global_id)
        self._db.execute(
            "UPDATE sync_versions SET local_version = local_version + 1, updated_at = ? "
            "WHERE record_type = ? AND global_id = ?", (_now(), record_type, global_id))
        return self.get_local_version(record_type, global_id)

    def set_last_known_hash(self, record_type: str, global_id: str, content_hash: str) -> None:
        self._ensure_row(record_type, global_id)
        self._db.execute(
            "UPDATE sync_versions SET last_known_hash = ?, updated_at = ? "
            "WHERE record_type = ? AND global_id = ?",
            (content_hash, _now(), record_type, global_id))

    def set_last_remote_version(self, record_type: str, global_id: str, version: int) -> None:
        self._ensure_row(record_type, global_id)
        self._db.execute(
            "UPDATE sync_versions SET last_remote_version = ?, updated_at = ? "
            "WHERE record_type = ? AND global_id = ?",
            (version, _now(), record_type, global_id))

    def clear(self, record_type: str, global_id: str) -> None:
        self._db.execute(
            "DELETE FROM sync_versions WHERE record_type = ? AND global_id = ?",
            (record_type, global_id))


class TombstoneStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def mark_deleted(self, record_type: str, global_id: str, device_id: str) -> None:
        self._db.execute(
            "INSERT INTO sync_tombstones (record_type, global_id, deleted_at, device_id) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(record_type, global_id) DO UPDATE SET "
            "deleted_at=excluded.deleted_at, device_id=excluded.device_id",
            (record_type, global_id, _now(), device_id))

    def is_deleted(self, record_type: str, global_id: str) -> bool:
        row = self._db.query_one(
            "SELECT 1 FROM sync_tombstones WHERE record_type = ? AND global_id = ?",
            (record_type, global_id))
        return row is not None

    def all_since(self, record_type: str, since: str | None) -> list[dict[str, Any]]:
        if since:
            rows = self._db.query(
                "SELECT record_type, global_id, deleted_at, device_id FROM sync_tombstones "
                "WHERE record_type = ? AND deleted_at > ? ORDER BY deleted_at",
                (record_type, since))
        else:
            rows = self._db.query(
                "SELECT record_type, global_id, deleted_at, device_id FROM sync_tombstones "
                "WHERE record_type = ? ORDER BY deleted_at", (record_type,))
        return [dict(row) for row in rows]

    def purge_older_than(self, cutoff_iso: str) -> int:
        """Expire tombstones past the retention window (Part 20: "should
        expire only after a safe retention period"). Returns how many were
        purged."""
        cursor = self._db.execute(
            "DELETE FROM sync_tombstones WHERE deleted_at < ?", (cutoff_iso,))
        return cursor.rowcount if cursor is not None else 0


class OwnershipStore:
    """Part 11/12: which device actually executes/polls a synced Scheduled
    Task or Watch. No row for a given (record_type, local_id) means "run
    locally, unconditionally" - the pre-sync, single-device default this
    table is designed to never change unless sync is actually turned on
    for that record."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, record_type: str, local_id: str) -> str | None:
        row = self._db.query_one(
            "SELECT execution_device FROM sync_task_ownership "
            "WHERE record_type = ? AND local_id = ?", (record_type, str(local_id)))
        return row["execution_device"] if row is not None else None

    def set_if_unset(self, record_type: str, local_id: str, device_id: str) -> None:
        """"Default existing schedules to their creation device" (Part 11) -
        never overwrites an already-set owner."""
        self._db.execute(
            "INSERT OR IGNORE INTO sync_task_ownership (record_type, local_id, "
            "execution_device) VALUES (?, ?, ?)", (record_type, str(local_id), device_id))

    def set(self, record_type: str, local_id: str, device_id: str) -> None:
        self._db.execute(
            "INSERT INTO sync_task_ownership (record_type, local_id, execution_device) "
            "VALUES (?, ?, ?) ON CONFLICT(record_type, local_id) DO UPDATE SET "
            "execution_device=excluded.execution_device", (record_type, str(local_id), device_id))

    def is_owned_by(self, record_type: str, local_id: str, device_id: str) -> bool:
        """True if this device may execute/poll this record - either it is
        the recorded owner, or (no owner recorded at all - sync never
        touched this row) the pre-sync default of "always run locally"."""
        owner = self.get(record_type, local_id)
        return owner is None or owner == device_id

    def remove(self, record_type: str, local_id: str) -> None:
        self._db.execute(
            "DELETE FROM sync_task_ownership WHERE record_type = ? AND local_id = ?",
            (record_type, str(local_id)))


class ConflictStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, record_type: str, global_id: str, local_payload: str, remote_payload: str) -> int:
        cursor = self._db.execute(
            "INSERT INTO sync_conflicts (record_type, global_id, local_payload, "
            "remote_payload, detected_at, resolved) VALUES (?, ?, ?, ?, ?, 0)",
            (record_type, global_id, local_payload, remote_payload, _now()))
        return cursor.lastrowid if cursor is not None else -1

    def unresolved(self) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT id, record_type, global_id, local_payload, remote_payload, detected_at "
            "FROM sync_conflicts WHERE resolved = 0 ORDER BY detected_at")
        return [dict(row) for row in rows]

    def resolve(self, conflict_id: int) -> None:
        self._db.execute("UPDATE sync_conflicts SET resolved = 1 WHERE id = ?", (conflict_id,))
