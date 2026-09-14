"""This device's own sync identity, and the registry of every device this
profile has ever synced with. A device id is a plain random id, never a
hardware fingerprint - Part 4 asks for an editable display name
("Wendy's Mac", "Jeremy's PC"), which only makes sense if identity is
just a label a person chose, not something derived from the machine.
"""

from __future__ import annotations

import platform
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.storage.settings import SettingsStore
    from app.storage.sync_store import SyncDeviceStore

#: Where this device's own id is remembered - a plain settings key, not a
#: secret: knowing a device id lets you see records exist, never decrypt
#: them (that needs the master key, kept in the OS keyring - see
#: app/sync/service.py).
_SETTINGS_KEY_DEVICE_ID = "sync_device_id"


@dataclass(frozen=True)
class DeviceIdentity:
    id: str
    name: str
    created_at: str
    last_sync_at: str | None = None


def _default_device_name() -> str:
    node = platform.node().strip()
    return node.split(".")[0] if node else "This Device"


def ensure_local_device(settings: "SettingsStore", devices: "SyncDeviceStore") -> DeviceIdentity:
    """Get-or-create this device's own identity - stable across restarts,
    never regenerated once created."""
    device_id = settings.get(_SETTINGS_KEY_DEVICE_ID, "") or ""
    if not device_id:
        device_id = str(uuid.uuid4())
        settings.set(_SETTINGS_KEY_DEVICE_ID, device_id)
    existing = devices.get(device_id)
    if existing is None:
        devices.upsert(device_id, _default_device_name())
        existing = devices.get(device_id)
    return DeviceIdentity(id=existing["id"], name=existing["name"],
                          created_at=existing["created_at"], last_sync_at=existing["last_sync_at"])


def rename_device(devices: "SyncDeviceStore", device_id: str, name: str) -> None:
    name = (name or "").strip()
    if name:
        devices.rename(device_id, name)
