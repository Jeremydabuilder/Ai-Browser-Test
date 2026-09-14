"""The sync orchestrator - ties a SyncProvider, the master key, this
device's id, and a set of per-domain adapters into one ``sync_once()``
call. Nothing here is Qt; MainWindow's sync controller (not built in this
pass beyond Settings -> Sync, see the checkpoint report) is what would put
this on a timer (Part 18) - this module itself never blocks on a network
call longer than the provider implementation does, and never touches the
GUI thread's event loop.

Order of operations per sync, chosen deliberately: **pull before push**.
Downloading and reconciling remote changes first means a local push only
ever uploads what genuinely still differs after reconciliation - a record
that turned out to be identical to what just arrived from a peer is never
re-uploaded needlessly.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.sync import crypto
from app.sync.conflicts import ConflictPolicy, Resolution, policy_for
from app.sync.providers.base import SyncProviderUnavailable
from app.sync.types import RecordType, SyncRecord

if TYPE_CHECKING:
    from app.storage.sync_store import (
        ConflictStore, GlobalIdStore, SyncVersionStore, TombstoneStore,
    )
    from app.sync.providers.base import SyncProvider


class SyncStatus:
    OFF = "off"
    SYNCING = "syncing"
    UP_TO_DATE = "up_to_date"
    OFFLINE = "offline"
    CONFLICT = "conflict"
    ERROR = "error"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_hash(payload: dict) -> str:
    """A stable hash of a record's plaintext payload - the 3-way-merge
    marker's unit of comparison. Canonical (sorted-key) JSON so the same
    logical content always hashes the same regardless of dict ordering."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SyncAdapter(ABC):
    """One per RecordType - translates between a domain store's rows and
    plain-dict sync payloads. Never touches the provider, encryption, or
    conflict policy directly; the engine calls these four methods only."""

    record_type: str
    #: True for a record type whose local id is *already* a stable,
    #: content-derived id shared by construction across every device (the
    #: Knowledge Graph's Topic/WebPage/PDF/File/Claim node ids, and this
    #: profile's Settings singleton) - the engine then uses the local id
    #: as the global id directly rather than minting an unrelated random
    #: one, which is what makes two devices that independently produce
    #: "the same" Topic/Claim/source node converge on one record instead
    #: of two.
    deterministic_ids: bool = False

    @abstractmethod
    def iter_local_ids(self) -> list[str]:
        """Every local id that currently exists and should be synced."""

    @abstractmethod
    def build_payload(self, local_id: str) -> dict | None:
        """The current syncable payload for ``local_id``, or None if it no
        longer exists locally."""

    @abstractmethod
    def apply_create_or_update(self, payload: dict, *, local_id: str | None,
                              global_id: str = "") -> str:
        """Create (local_id=None) or update (local_id given) a local row
        from a payload that just arrived (from a peer, or from resolving a
        conflict). ``global_id`` is always given - only load-bearing for a
        ``deterministic_ids`` adapter's create path, where it names the id
        the new local row must be created with (e.g. a Knowledge Graph
        node/edge, or the Settings singleton). Returns the local id used."""

    @abstractmethod
    def apply_delete(self, local_id: str) -> None:
        """Remove the local row entirely - a remote tombstone applied."""


@dataclass
class SyncResult:
    status: str
    uploaded: int = 0
    downloaded: int = 0
    deleted_remote: int = 0
    deleted_local: int = 0
    conflicts: int = 0
    error: str = ""


@dataclass
class _PendingUpload:
    global_id: str
    payload: dict
    deleted: bool


class SyncEngine:
    def __init__(self, *, provider: "SyncProvider", master_key: bytes, device_id: str,
                adapters: dict[str, SyncAdapter], global_ids: "GlobalIdStore",
                versions: "SyncVersionStore", tombstones: "TombstoneStore",
                conflicts: "ConflictStore", since_token: str | None = None,
                on_since_token: "callable | None" = None) -> None:
        self._provider = provider
        self._key = master_key
        self._device_id = device_id
        self._adapters = adapters
        self._global_ids = global_ids
        self._versions = versions
        self._tombstones = tombstones
        self._conflicts = conflicts
        self._since_token = since_token
        #: Called with the new since-token after every successful sync, so
        #: the caller can persist it (e.g. into SettingsStore) - kept
        #: outside this class rather than this class owning a SettingsStore
        #: directly, the same "ask for exactly what you need" shape every
        #: other collaborator in this codebase already follows.
        self._on_since_token = on_since_token

    # -- one full sync pass -------------------------------------------------
    def sync_once(self) -> SyncResult:
        try:
            entries, new_token = self._provider.list_changes(self._since_token)
        except SyncProviderUnavailable as exc:
            return SyncResult(status=SyncStatus.OFFLINE, error=str(exc))

        downloaded = 0
        deleted_local = 0
        conflicts = 0

        by_record_type: dict[str, list] = {}
        for entry in entries:
            record_type, _, global_id = entry.key.partition(":")
            by_record_type.setdefault(record_type, []).append((entry, global_id))

        for record_type, adapter in self._adapters.items():
            for entry, global_id in by_record_type.get(record_type, []):
                outcome = self._pull_one(adapter, record_type, global_id, entry.key)
                if outcome == "downloaded":
                    downloaded += 1
                elif outcome == "deleted":
                    deleted_local += 1
                elif outcome == "conflict":
                    conflicts += 1

        uploaded = 0
        deleted_remote = 0
        for record_type, adapter in self._adapters.items():
            up, dele = self._push_one_type(adapter, record_type)
            uploaded += up
            deleted_remote += dele

        self._since_token = new_token
        if self._on_since_token is not None:
            self._on_since_token(new_token)

        status = SyncStatus.CONFLICT if conflicts else SyncStatus.UP_TO_DATE
        return SyncResult(status=status, uploaded=uploaded, downloaded=downloaded,
                          deleted_remote=deleted_remote, deleted_local=deleted_local,
                          conflicts=conflicts)

    def _resolve_global_id(self, adapter: SyncAdapter, record_type: str, local_id: str) -> str:
        if adapter.deterministic_ids:
            self._global_ids.bind(record_type, local_id, local_id)
            return local_id
        return self._global_ids.ensure_global_id(record_type, local_id)

    # -- pull: one downloaded, decrypted remote record -----------------------
    def _pull_one(self, adapter: SyncAdapter, record_type: str, global_id: str,
                 storage_key: str) -> str:
        raw = self._provider.download(storage_key)
        if raw is None:
            return "noop"
        package = crypto.EncryptedPackage.from_bytes(raw)
        try:
            remote = crypto.decrypt_record(self._key, package, expected_key=storage_key)
        except crypto.CryptoError:
            # Part 22: tampered or wrong-key ciphertext - never applied,
            # never crashes the sync; the record is simply left alone
            # until an operator investigates. Surfaced as an error status
            # by the caller if it becomes the dominant outcome.
            return "noop"

        last_remote_version = self._versions.get_last_remote_version(record_type, global_id)
        if remote.version <= last_remote_version and last_remote_version > 0:
            # Part 22: a replay/rollback of an older version - ignored.
            return "noop"

        local_id = self._global_ids.get_local_id(record_type, global_id)

        if remote.deleted:
            if local_id is not None:
                adapter.apply_delete(local_id)
            self._tombstones.mark_deleted(record_type, global_id, remote.device_id)
            self._versions.set_last_remote_version(record_type, global_id, remote.version)
            self._versions.clear(record_type, global_id)
            return "deleted"

        if self._tombstones.is_deleted(record_type, global_id):
            # This device already deleted it; a deletion is never silently
            # un-done by a peer's stale update (Part 20).
            self._versions.set_last_remote_version(record_type, global_id, remote.version)
            return "noop"

        if local_id is None:
            new_local_id = adapter.apply_create_or_update(
                remote.payload, local_id=None, global_id=global_id)
            self._global_ids.bind(record_type, new_local_id, global_id)
            self._versions.set_last_known_hash(record_type, global_id, content_hash(remote.payload))
            self._versions.set_last_remote_version(record_type, global_id, remote.version)
            return "downloaded"

        local_payload = adapter.build_payload(local_id)
        if local_payload is None:
            # Deleted locally, not yet uploaded as a tombstone this cycle -
            # local deletion wins; the push phase will upload it shortly.
            self._versions.set_last_remote_version(record_type, global_id, remote.version)
            return "noop"

        last_known = self._versions.get_last_known_hash(record_type, global_id) or ""
        local_hash = content_hash(local_payload)
        remote_hash = content_hash(remote.payload)
        local_changed = local_hash != last_known
        remote_changed = remote_hash != last_known

        if not local_changed and not remote_changed:
            self._versions.set_last_remote_version(record_type, global_id, remote.version)
            return "noop"
        if remote_changed and not local_changed:
            adapter.apply_create_or_update(remote.payload, local_id=local_id, global_id=global_id)
            self._versions.set_last_known_hash(record_type, global_id, remote_hash)
            self._versions.set_last_remote_version(record_type, global_id, remote.version)
            return "downloaded"
        if local_changed and not remote_changed:
            self._versions.set_last_remote_version(record_type, global_id, remote.version)
            return "noop"  # keep local; pushed below

        # Both changed since the last agreed state - a genuine conflict.
        self._versions.set_last_remote_version(record_type, global_id, remote.version)
        return self._handle_conflict(adapter, record_type, global_id, local_payload, remote.payload)

    def _handle_conflict(self, adapter: SyncAdapter, record_type: str, global_id: str,
                         local_payload: dict, remote_payload: dict) -> str:
        policy = policy_for(record_type)
        local_id = self._global_ids.get_local_id(record_type, global_id)
        if policy == ConflictPolicy.KEEP_BOTH:
            duplicate_global_id = str(uuid.uuid4())
            new_local_id = adapter.apply_create_or_update(
                remote_payload, local_id=None, global_id=duplicate_global_id)
            new_global_id = self._global_ids.ensure_global_id(record_type, new_local_id)
            self._versions.set_last_known_hash(record_type, new_global_id,
                                               content_hash(remote_payload))
            return "downloaded"
        if policy == ConflictPolicy.NEWER_WINS:
            # Wall-clock comparison isn't tracked per-field here - "newer
            # wins" for this policy tier means the remote copy, since it
            # is what we just learned about; local keeps its own content
            # only until its own next push overwrites this again, which is
            # an accepted, documented limitation of a last-write-wins
            # policy (Part 9: "Start with simple rules").
            if local_id is not None:
                adapter.apply_create_or_update(remote_payload, local_id=local_id,
                                               global_id=global_id)
            self._versions.set_last_known_hash(record_type, global_id, content_hash(remote_payload))
            return "downloaded"
        # MANUAL - never silently drop either side (Part 9).
        existing = [c for c in self._conflicts.unresolved()
                   if c["record_type"] == record_type and c["global_id"] == global_id]
        if not existing:
            self._conflicts.add(record_type, global_id,
                                json.dumps(local_payload), json.dumps(remote_payload))
        return "conflict"

    # -- resolving a manual conflict (Part 9's three options) ---------------
    def resolve_conflict(self, conflict_id: int, resolution: str) -> None:
        conflict = next((c for c in self._conflicts.unresolved() if c["id"] == conflict_id), None)
        if conflict is None:
            return
        record_type = conflict["record_type"]
        global_id = conflict["global_id"]
        adapter = self._adapters[record_type]
        local_id = self._global_ids.get_local_id(record_type, global_id)
        local_payload = json.loads(conflict["local_payload"])
        remote_payload = json.loads(conflict["remote_payload"])

        if resolution == Resolution.KEEP_LOCAL:
            self._versions.set_last_known_hash(record_type, global_id, content_hash(local_payload))
        elif resolution == Resolution.KEEP_REMOTE:
            if local_id is not None:
                adapter.apply_create_or_update(remote_payload, local_id=local_id,
                                               global_id=global_id)
            self._versions.set_last_known_hash(record_type, global_id, content_hash(remote_payload))
        elif resolution == Resolution.KEEP_BOTH:
            duplicate_global_id = str(uuid.uuid4())
            new_local_id = adapter.apply_create_or_update(
                remote_payload, local_id=None, global_id=duplicate_global_id)
            new_global_id = self._global_ids.ensure_global_id(record_type, new_local_id)
            self._versions.set_last_known_hash(record_type, new_global_id,
                                               content_hash(remote_payload))
            self._versions.set_last_known_hash(record_type, global_id, content_hash(local_payload))
        self._conflicts.resolve(conflict_id)

    # -- push: everything still-locally-different after the pull phase -----
    def _push_one_type(self, adapter: SyncAdapter, record_type: str) -> tuple[int, int]:
        uploaded = 0
        deleted_remote = 0
        existing_local_ids = set(adapter.iter_local_ids())

        for local_id, global_id in self._global_ids.all_for_type(record_type):
            if local_id in existing_local_ids:
                continue
            if self._tombstones.is_deleted(record_type, global_id):
                continue  # already tombstoned (e.g. by a remote delete we just applied)
            self._upload_tombstone(record_type, global_id)
            deleted_remote += 1

        for local_id in existing_local_ids:
            global_id = self._resolve_global_id(adapter, record_type, local_id)
            payload = adapter.build_payload(local_id)
            if payload is None:
                continue
            last_known = self._versions.get_last_known_hash(record_type, global_id) or ""
            current_hash = content_hash(payload)
            # An unresolved manual conflict freezes this record's push side
            # until the user resolves it - neither side is overwritten.
            if any(c["record_type"] == record_type and c["global_id"] == global_id
                  for c in self._conflicts.unresolved()):
                continue
            if current_hash == last_known:
                continue
            version = self._versions.bump_local_version(record_type, global_id)
            record = SyncRecord(record_type=record_type, global_id=global_id, payload=payload,
                                modified_at=_now(), device_id=self._device_id, version=version)
            package = crypto.encrypt_record(self._key, record)
            self._provider.upload(crypto.storage_key(record_type, global_id), package.to_bytes())
            self._versions.set_last_known_hash(record_type, global_id, current_hash)
            uploaded += 1

        return uploaded, deleted_remote

    def _upload_tombstone(self, record_type: str, global_id: str) -> None:
        self._tombstones.mark_deleted(record_type, global_id, self._device_id)
        version = self._versions.bump_local_version(record_type, global_id)
        record = SyncRecord(record_type=record_type, global_id=global_id, payload={},
                            modified_at=_now(), device_id=self._device_id, deleted=True,
                            version=version)
        package = crypto.encrypt_record(self._key, record)
        self._provider.upload(crypto.storage_key(record_type, global_id), package.to_bytes())
        self._versions.clear(record_type, global_id)
