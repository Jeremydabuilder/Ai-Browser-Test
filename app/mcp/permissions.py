"""Per-tool MCP permission decisions: Allow/Ask/Deny, optionally remembered.

This module only decides *whether a stored decision exists* for a given
tool. It has no opinion about confirmation UI, sensitivity classification,
or what happens when there is no stored decision (that is
safety.default_permission and, one layer up, ToolRegistry/AgentSession's
existing confirmation flow - the exact same one browser tools use). Kept
separate for the same reason config.py's server storage is: a permission
row is a preference, not a secret, so it lives in the plain settings JSON
blob, never the keyring.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from app.mcp.types import Scope
from app.storage.settings import SettingsStore

KEY_MCP_PERMISSIONS = "mcp_permissions"


@dataclass(frozen=True)
class PermissionRecord:
    server_id: str
    tool_name: str
    #: The tool's schema_fingerprint at the moment this decision was made -
    #: see McpToolDescriptor.schema_fingerprint. A record whose fingerprint
    #: no longer matches the tool's *current* schema is never matched by
    #: decision_for, which is what invalidates a remembered decision when a
    #: server changes a tool's shape after a reconnect.
    schema_fingerprint: str
    permission: str          # Permission.ALLOW or Permission.DENY (ASK is never stored)
    scope: str                # Scope.MISSION or Scope.ALWAYS (ONCE is never stored)
    #: Set only when scope == Scope.MISSION.
    mission_id: int | None = None


def _record_from_json(data: dict[str, Any]) -> PermissionRecord | None:
    try:
        return PermissionRecord(
            server_id=str(data["server_id"]),
            tool_name=str(data["tool_name"]),
            schema_fingerprint=str(data.get("schema_fingerprint", "")),
            permission=str(data["permission"]),
            scope=str(data["scope"]),
            mission_id=data.get("mission_id"),
        )
    except (KeyError, TypeError, ValueError):
        # A hand-edited or corrupted row must not crash Settings or the
        # agent loop - it is simply dropped, same as a bad server config row.
        return None


class McpPermissionStore:
    """CRUD for remembered per-tool permission decisions.

    ``settings`` may be None (no profile database yet) - every method then
    degrades to an empty list / no-op, the same defensive posture
    McpServerStore already takes.
    """

    def __init__(self, settings: SettingsStore | None) -> None:
        self._settings = settings

    def _load(self) -> list[PermissionRecord]:
        if self._settings is None:
            return []
        raw = self._settings.get(KEY_MCP_PERMISSIONS, "[]")
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(items, list):
            return []
        records = [_record_from_json(item) for item in items if isinstance(item, dict)]
        return [r for r in records if r is not None]

    def _write(self, records: list[PermissionRecord]) -> None:
        if self._settings is None:
            return
        self._settings.set(KEY_MCP_PERMISSIONS, json.dumps(
            [asdict(r) for r in records], ensure_ascii=False))

    def decision_for(self, server_id: str, tool_name: str, schema_fingerprint: str,
                     *, mission_id: int | None) -> str | None:
        """The remembered Allow/Deny for this exact tool *and* schema shape,
        or None when there is no applicable cached decision - meaning
        "ask", exactly as if nothing had ever been remembered.

        A Scope.MISSION record only ever matches while ``mission_id`` is the
        same Mission it was recorded against; once that Mission is no longer
        active (or no Mission is), it is inert - not deleted, just not
        consulted, so resuming the same Mission later still honours it.
        """
        for record in self._load():
            if record.server_id != server_id or record.tool_name != tool_name:
                continue
            if record.schema_fingerprint != schema_fingerprint:
                continue  # the tool's shape changed since this was recorded
            if record.scope == Scope.ALWAYS:
                return record.permission
            if (record.scope == Scope.MISSION and mission_id is not None
                    and record.mission_id == mission_id):
                return record.permission
        return None

    def remember(self, server_id: str, tool_name: str, schema_fingerprint: str,
                permission: str, scope: str, *, mission_id: int | None = None) -> None:
        """Persist a decision. A no-op for Scope.ONCE - "just this time" is
        never written anywhere, by construction rather than by a caller
        remembering not to call this."""
        if scope == Scope.ONCE:
            return

        def _same_slot(record: PermissionRecord) -> bool:
            if record.server_id != server_id or record.tool_name != tool_name:
                return False
            if scope == Scope.ALWAYS:
                return record.scope == Scope.ALWAYS
            return record.scope == Scope.MISSION and record.mission_id == mission_id

        records = [r for r in self._load() if not _same_slot(r)]
        records.append(PermissionRecord(
            server_id=server_id, tool_name=tool_name,
            schema_fingerprint=schema_fingerprint, permission=permission,
            scope=scope, mission_id=mission_id))
        self._write(records)

    def clear(self, server_id: str, tool_name: str) -> None:
        """Forget every remembered decision for one tool, of any scope."""
        records = [r for r in self._load()
                  if not (r.server_id == server_id and r.tool_name == tool_name)]
        self._write(records)

    def forget_server(self, server_id: str) -> None:
        """Called when a server is removed - its permissions go with it.
        Also the implementation of "Reset this server"'s permissions from
        the global Settings view, which does the same thing without
        removing the server itself."""
        records = [r for r in self._load() if r.server_id != server_id]
        self._write(records)

    def clear_all(self) -> None:
        """"Reset all MCP permissions" - every server, every tool."""
        self._write([])

    def all_for_server(self, server_id: str) -> list[PermissionRecord]:
        return [r for r in self._load() if r.server_id == server_id]

    def all(self) -> list[PermissionRecord]:
        return self._load()
