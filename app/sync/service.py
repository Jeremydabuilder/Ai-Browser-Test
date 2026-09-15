"""The one thing UI code constructs and holds for Encrypted Sync - wires
together the master key (OS keyring), this device's identity, the chosen
provider, every adapter, and the SyncEngine. Mirrors KnowledgeGraphService/
KnowledgeIndex's role for their own phases: a policy layer over otherwise
independent, unit-testable pieces (crypto.py, engine.py, providers/*).

Sync is off by default and never blocks browser startup (Part 17/19) -
constructing this class does nothing by itself; sync_now()/background
sync only ever run when explicitly enabled.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.agent.keys import ApiKeyStore, KeyringUnavailable
from app.sync import crypto
from app.sync.device import ensure_local_device, rename_device
from app.sync.engine import SyncEngine, SyncResult, SyncStatus
from app.sync.providers.local_folder import LocalFolderProvider

if TYPE_CHECKING:
    from app.storage.database import Database
    from app.storage.settings import SettingsStore

SETTINGS_KEY_ENABLED = "sync_enabled"
SETTINGS_KEY_PROVIDER = "sync_provider_kind"
SETTINGS_KEY_FOLDER = "sync_folder_path"
SETTINGS_KEY_SINCE_TOKEN = "sync_since_token"
SETTINGS_KEY_LAST_SYNC_AT = "sync_last_sync_at"

PROVIDER_LOCAL_FOLDER = "local_folder"

_MASTER_KEY_ACCOUNT = "sync-master-key"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SyncNotConfigured(Exception):
    """Sync is enabled but has no usable master key/provider yet - a
    caller should route the user to setup, never treat this as a crash."""


class SyncService:
    def __init__(self, db: "Database", settings: "SettingsStore", *,
                key_store: "ApiKeyStore | None" = None, force_disabled: bool = False) -> None:
        from app.storage.sync_store import (
            ConflictStore, GlobalIdStore, SyncDeviceStore, SyncVersionStore,
            OwnershipStore, TombstoneStore,
        )

        self._db = db
        self._settings = settings
        self._key_store = key_store or ApiKeyStore(account=_MASTER_KEY_ACCOUNT)
        #: Phase 22 Safe Mode (Part 14) - overrides the saved on/off
        #: setting to OFF for this run only, without touching it, so
        #: turning Safe Mode off again later leaves sync exactly as the
        #: user configured it.
        self._force_disabled = force_disabled
        self.devices = SyncDeviceStore(db)
        self.global_ids = GlobalIdStore(db)
        self.versions = SyncVersionStore(db)
        self.tombstones = TombstoneStore(db)
        self.conflicts = ConflictStore(db)
        self.ownership = OwnershipStore(db)
        self.device = ensure_local_device(settings, self.devices)
        self._last_status = SyncStatus.OFF

    # -- configuration -----------------------------------------------------
    @property
    def enabled(self) -> bool:
        if self._force_disabled:
            return False
        return (self._settings.get(SETTINGS_KEY_ENABLED, "") or "") == "1"

    @property
    def folder_path(self) -> str:
        return self._settings.get(SETTINGS_KEY_FOLDER, "") or ""

    @property
    def last_sync_at(self) -> str:
        return self._settings.get(SETTINGS_KEY_LAST_SYNC_AT, "") or ""

    @property
    def status(self) -> str:
        if not self.enabled:
            return SyncStatus.OFF
        return self._last_status

    def has_master_key(self) -> bool:
        try:
            return self._key_store.get_keyring_key() is not None
        except KeyringUnavailable:
            return False

    def set_folder(self, path: str) -> None:
        self._settings.set(SETTINGS_KEY_FOLDER, path)
        self._settings.set(SETTINGS_KEY_PROVIDER, PROVIDER_LOCAL_FOLDER)

    def rename_this_device(self, name: str) -> None:
        rename_device(self.devices, self.device.id, name)
        self.device = ensure_local_device(self._settings, self.devices)

    # -- setup / pairing / recovery -----------------------------------------
    def start_new_sync(self, folder: str) -> str:
        """Turn sync on for the FIRST device: generate a fresh master key,
        store it in the OS keyring, remember a recovery key wrapped by it
        in the sync folder, and return the recovery key for the user to
        write down (Part 5/23 - shown exactly once, like every other
        recovery-code flow in this codebase)."""
        master_key = crypto.generate_master_key()
        self._key_store.set_key(master_key.hex())
        recovery_key = crypto.generate_recovery_key()
        wrapped = crypto.wrap_master_key(master_key, recovery_key)
        provider = LocalFolderProvider(folder)
        provider.upload("recovery:wrapped_master_key", wrapped.to_bytes())
        self.set_folder(folder)
        self._settings.set(SETTINGS_KEY_ENABLED, "1")
        return recovery_key

    def join_with_recovery_key(self, folder: str, recovery_key: str) -> None:
        """Part 5 - a new device joining an existing sync set via the
        recovery key (Part 23's own recovery path doubles as the pairing
        mechanism here, since there is no PyBrowser-hosted pairing server
        in this phase - see the checkpoint report)."""
        provider = LocalFolderProvider(folder)
        raw = provider.download("recovery:wrapped_master_key")
        if raw is None:
            raise SyncNotConfigured("No PyBrowser sync data found in that folder.")
        wrapped = crypto.WrappedKey.from_bytes(raw)
        master_key = crypto.unwrap_master_key(wrapped, recovery_key)  # raises CryptoError if wrong
        self._key_store.set_key(master_key.hex())
        self.set_folder(folder)
        self._settings.set(SETTINGS_KEY_ENABLED, "1")

    def disconnect(self) -> None:
        """Part 16's "Disconnect this device" - stops syncing and forgets
        the master key on THIS device only; the sync folder and every
        other device are untouched."""
        self._settings.set(SETTINGS_KEY_ENABLED, "0")
        try:
            self._key_store.clear_key()
        except KeyringUnavailable:
            pass

    # -- the actual sync pass -----------------------------------------------
    def _adapters(self, *, missions=None, highlights=None, skills=None, workspaces=None,
                 scheduled_tasks=None, watches=None, graph_store=None) -> dict:
        from app.sync.adapters import (
            GraphEdgeAdapter, GraphNodeAdapter, HighlightAdapter, MissionAdapter,
            MissionFindingAdapter, ScheduledTaskAdapter, SettingsAdapter, SkillAdapter,
            WatchAdapter, WorkspaceAdapter,
        )
        from app.sync.types import RecordType

        # Ordering matters: a record type that references another (Finding
        # -> Mission, GraphEdge -> GraphNode) must be processed after the
        # type it depends on, both for pull (create parents first) and push
        # (parents get their global id allocated first) - see the module
        # docstring in engine.py.
        adapters: dict[str, object] = {}
        if missions is not None:
            adapters[RecordType.MISSION] = MissionAdapter(missions)
            adapters[RecordType.MISSION_FINDING] = MissionFindingAdapter(missions, self.global_ids)
        if highlights is not None:
            adapters[RecordType.HIGHLIGHT] = HighlightAdapter(highlights)
        if skills is not None:
            adapters[RecordType.SKILL] = SkillAdapter(skills)
        if workspaces is not None:
            adapters[RecordType.WORKSPACE] = WorkspaceAdapter(workspaces)
        if scheduled_tasks is not None:
            adapters[RecordType.SCHEDULED_TASK] = ScheduledTaskAdapter(
                scheduled_tasks, self.ownership, self.device.id)
        if watches is not None:
            adapters[RecordType.WATCH] = WatchAdapter(watches, self.ownership, self.device.id)
        if graph_store is not None:
            adapters[RecordType.GRAPH_NODE] = GraphNodeAdapter(graph_store)
            adapters[RecordType.GRAPH_EDGE] = GraphEdgeAdapter(graph_store)
        adapters[RecordType.SETTINGS] = SettingsAdapter(self._settings)
        return adapters

    def sync_now(self, **domain_stores) -> SyncResult:
        """Run one full sync pass. ``**domain_stores`` are whichever of
        missions/highlights/skills/workspaces/scheduled_tasks/watches/
        graph_store the caller has available - omit any the caller does
        not want synced this pass (e.g. a headless/test context)."""
        if not self.enabled:
            self._last_status = SyncStatus.OFF
            return SyncResult(status=SyncStatus.OFF)
        try:
            master_key = bytes.fromhex(self._key_store.get_keyring_key() or "")
        except KeyringUnavailable:
            self._last_status = SyncStatus.ERROR
            return SyncResult(status=SyncStatus.ERROR, error="OS keyring is unavailable.")
        if not master_key:
            self._last_status = SyncStatus.ERROR
            return SyncResult(status=SyncStatus.ERROR, error="No sync key is configured.")

        provider = LocalFolderProvider(self.folder_path)
        since_token = self._settings.get(SETTINGS_KEY_SINCE_TOKEN, "") or None

        def _persist_token(token: str) -> None:
            self._settings.set(SETTINGS_KEY_SINCE_TOKEN, token)

        engine = SyncEngine(
            provider=provider, master_key=master_key, device_id=self.device.id,
            adapters=self._adapters(**domain_stores), global_ids=self.global_ids,
            versions=self.versions, tombstones=self.tombstones, conflicts=self.conflicts,
            since_token=since_token, on_since_token=_persist_token)
        result = engine.sync_once()
        self._last_status = result.status
        if result.status != SyncStatus.OFFLINE:
            self._settings.set(SETTINGS_KEY_LAST_SYNC_AT, _now())
            self.devices.touch_last_sync(self.device.id)
        return result

    def resolve_conflict(self, conflict_id: int, resolution: str, **domain_stores) -> None:
        """Part 9's Keep this device / Keep other device / Keep both,
        applied outside a full sync pass - the next sync_now() then pushes
        or confirms the result normally."""
        master_key = bytes.fromhex(self._key_store.get_keyring_key() or "")
        provider = LocalFolderProvider(self.folder_path)
        engine = SyncEngine(
            provider=provider, master_key=master_key, device_id=self.device.id,
            adapters=self._adapters(**domain_stores), global_ids=self.global_ids,
            versions=self.versions, tombstones=self.tombstones, conflicts=self.conflicts)
        engine.resolve_conflict(conflict_id, resolution)
