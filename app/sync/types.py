"""Phase 20 Encrypted Sync - the syncable data model.

Part 1's own distinction, made explicit here so every adapter agrees on it:

* **Syncable** - Mission metadata/findings, Highlights, Skills (incl.
  recorded workflows), Workspace metadata/tab order/groups, scheduled-task
  and Watch *definitions*, Knowledge Graph metadata/relationships, and a
  small allowlisted subset of settings.
* **Local-only** - transient GUI state, process ids, temporary screenshots,
  the Mission execution/DAG runtime state (Part 10: never merge that
  blindly across devices), isolated-profile cookies/login sessions.
* **Never synced, ever, by default** - API keys, passwords, bearer tokens,
  OAuth refresh tokens, MCP pairing tokens. There is no code path in this
  package that reads app.agent.keys/app.agent.credentials/keyring-stored
  secrets at all - see ``assert_no_secrets`` for the belt-and-suspenders
  scan every payload goes through before it is ever encrypted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class RecordType:
    """One string per syncable domain object. Kept as plain constants
    (like NodeType/EdgeType in app.knowledge_graph.types) so a record type
    serializes straight into a payload with no extra conversion step."""

    MISSION = "mission"
    MISSION_FINDING = "mission_finding"
    HIGHLIGHT = "highlight"
    SKILL = "skill"
    WORKSPACE = "workspace"
    SCHEDULED_TASK = "scheduled_task"
    WATCH = "watch"
    GRAPH_NODE = "graph_node"
    GRAPH_EDGE = "graph_edge"
    SETTINGS = "settings"
    #: Phase 21 - Mission collaboration. Both are append-only/immutable
    #: once created (a comment is never edited in place; an activity entry
    #: is a fact about something that already happened), so they merge by
    #: stable id exactly like MISSION_FINDING/HIGHLIGHT.
    MISSION_COMMENT = "mission_comment"
    MISSION_ACTIVITY = "mission_activity"
    #: Phase 21 - who is on a shared Mission and what role they hold. A
    #: participant row's natural key (mission, device) is already globally
    #: meaningful (device ids are unique per Phase 20 device identity), so
    #: this uses deterministic ids like MISSION_COMMENT/MISSION_ACTIVITY -
    #: see app.collaboration.adapters.ParticipantAdapter.
    MISSION_PARTICIPANT = "mission_participant"

    ALL = frozenset({
        MISSION, MISSION_FINDING, HIGHLIGHT, SKILL, WORKSPACE, SCHEDULED_TASK,
        WATCH, GRAPH_NODE, GRAPH_EDGE, SETTINGS, MISSION_COMMENT, MISSION_ACTIVITY,
        MISSION_PARTICIPANT,
    })


#: Part 16/21: the only settings keys ever eligible to sync. Everything
#: else in SettingsStore - which includes no secrets itself (those live in
#: the OS keyring, never in SettingsStore - see app/agent/keys.py) but does
#: include purely local/device-specific things (window geometry, the local
#: sync folder path, this device's own id) - is excluded by never being on
#: this list, not by a runtime secret-detector. Reviewed by a human, not
#: grown by pattern-matching.
SYNCABLE_SETTINGS_KEYS = frozenset({
    "semantic_history_enabled",
    "new_tab_url",
})

#: Part 21's belt-and-suspenders scan: key *names* that must never appear
#: in a syncable payload, regardless of which adapter built it. Every
#: adapter in app/sync/adapters.py builds its payload from named fields it
#: chose deliberately (never "whatever attributes this object happens to
#: have"), so this should never fire in practice - it exists to fail loudly
#: in tests/CI if a future adapter ever does something careless, rather
#: than silently syncing a secret.
_SECRET_KEY_MARKERS = (
    "api_key", "apikey", "password", "passwd", "secret", "token", "bearer",
    "refresh_token", "access_token", "credential", "keyring", "pairing_code",
    "pairing_token", "private_key", "session_cookie", "cookie",
)


class SyncPayloadError(ValueError):
    """A payload about to be encrypted and synced contains something that
    must never leave this device unencrypted-adjacent, or at all."""


def assert_no_secrets(payload: Any, *, path: str = "") -> None:
    """Recursively refuse a payload whose keys look like secret material.
    Raises ``SyncPayloadError`` naming the offending path - never silently
    strips the field, since a silently-dropped field is a bug an adapter
    author would never notice."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in _SECRET_KEY_MARKERS):
                raise SyncPayloadError(
                    f"Refusing to sync field '{path}.{key}' - its name looks like a secret.")
            assert_no_secrets(value, path=f"{path}.{key}")
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            assert_no_secrets(item, path=f"{path}[{index}]")


@dataclass(frozen=True)
class SyncRecord:
    """One syncable object, plaintext, ready to be encrypted. ``global_id``
    is the stable id from GlobalIdStore - never a raw local row id, which
    means nothing outside this one profile."""

    record_type: str
    global_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    modified_at: str = ""
    device_id: str = ""
    deleted: bool = False
    #: This device's own monotonic counter for this record - Part 22's
    #: rollback/counter metadata, bound into the encrypted package's AAD
    #: (see crypto.py) so a provider replaying an old blob is detectable.
    version: int = 0

    def __post_init__(self) -> None:
        if not self.deleted:
            assert_no_secrets(self.payload)


@dataclass(frozen=True)
class EncryptedPackage:
    """What actually gets handed to a SyncProvider - opaque to it. See
    app/sync/crypto.py for how one of these is produced/opened."""

    schema_version: int
    content_type: str
    nonce: bytes
    ciphertext: bytes

    def to_bytes(self) -> bytes:
        """A tiny, explicit binary framing - version and content-type as a
        fixed 2-byte header, then a 12-byte nonce, then ciphertext. Not a
        general serialization format; this is the only thing ever written
        to a provider, so it does not need to be one."""
        content_type_bytes = self.content_type.encode("utf-8")
        if len(content_type_bytes) > 255:
            raise ValueError("content_type too long")
        header = bytes([self.schema_version & 0xFF, len(content_type_bytes)])
        return header + content_type_bytes + self.nonce + self.ciphertext

    @classmethod
    def from_bytes(cls, data: bytes) -> "EncryptedPackage":
        if len(data) < 2:
            raise ValueError("encrypted package too short")
        schema_version = data[0]
        content_type_len = data[1]
        offset = 2 + content_type_len
        if len(data) < offset + 12:
            raise ValueError("encrypted package truncated")
        content_type = data[2:offset].decode("utf-8")
        nonce = data[offset:offset + 12]
        ciphertext = data[offset + 12:]
        return cls(schema_version=schema_version, content_type=content_type,
                   nonce=nonce, ciphertext=ciphertext)
