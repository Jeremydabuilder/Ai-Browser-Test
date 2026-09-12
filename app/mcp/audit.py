"""A safe activity/audit log for MCP tool calls.

Deliberately minimal: what happened, not what was sent. No tool arguments,
no results, no secrets - just enough to answer "what did Py do through
which server, when, under whose permission, and did it work" without ever
needing to look at a payload. Persisted the same way McpServerStore and
McpPermissionStore are: one JSON blob in the ordinary settings table, since
none of this is a secret, capped so a long-lived profile's log cannot grow
without bound.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Any

from app.storage.settings import SettingsStore

KEY_MCP_AUDIT_LOG = "mcp_audit_log"

#: Oldest entries are trimmed, not refused - the same policy
#: MAX_ACTIONS_PER_MISSION already uses for Mission history.
MAX_AUDIT_ENTRIES = 500

#: "Permission decision" values - why this call was (or was not) allowed to
#: run, at the moment it was attempted. Never confused with Permission
#: (Allow/Ask/Deny) itself: a decision is a historical fact about one call,
#: a Permission is the current standing rule that produced it.
DECISION_AUTO = "auto"                          # read-only, never asked
DECISION_ALLOWED_REMEMBERED = "allowed_remembered"
DECISION_ALLOWED_ONCE = "allowed_once"           # approved live, this call only
DECISION_DENIED_REMEMBERED = "denied_remembered"
DECISION_DENIED_ONCE = "denied_once"             # declined live, this call only

#: "Outcome" values - what actually happened, only meaningful once a call
#: was allowed to run at all; a denied call is always "not_executed".
OUTCOME_SUCCESS = "success"
OUTCOME_ERROR = "error"
OUTCOME_TIMEOUT = "timeout"
OUTCOME_NOT_EXECUTED = "not_executed"

_DENIED_DECISIONS = (DECISION_DENIED_REMEMBERED, DECISION_DENIED_ONCE)


@dataclass(frozen=True)
class McpAuditEntry:
    timestamp: float           # time.time(), seconds since epoch
    server_id: str
    #: A display-name snapshot, taken at record time - still readable after
    #: the server itself is later renamed or removed.
    server_name: str
    tool_name: str
    sensitivity: str
    #: None when no Mission was active for this call.
    mission_id: int | None
    mission_title: str
    decision: str
    outcome: str
    #: None for a denied call (nothing ran to time), or when the duration
    #: genuinely was not captured.
    duration_ms: float | None = None
    error_code: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision not in _DENIED_DECISIONS

    @property
    def is_error(self) -> bool:
        return self.outcome in (OUTCOME_ERROR, OUTCOME_TIMEOUT)


def _entry_from_json(data: dict[str, Any]) -> McpAuditEntry | None:
    try:
        return McpAuditEntry(
            timestamp=float(data["timestamp"]),
            server_id=str(data["server_id"]),
            server_name=str(data.get("server_name", data["server_id"])),
            tool_name=str(data["tool_name"]),
            sensitivity=str(data.get("sensitivity", "unknown")),
            mission_id=data.get("mission_id"),
            mission_title=str(data.get("mission_title", "")),
            decision=str(data["decision"]),
            outcome=str(data["outcome"]),
            duration_ms=(float(data["duration_ms"])
                        if data.get("duration_ms") is not None else None),
            error_code=str(data.get("error_code", "")),
        )
    except (KeyError, TypeError, ValueError):
        return None


class McpAuditStore:
    """Append-only (with a cap) log of MCP call attempts.

    ``settings`` may be None, the same defensive posture every other
    settings-backed store in this package takes - every method degrades to
    an empty list / no-op rather than raising.
    """

    def __init__(self, settings: SettingsStore | None) -> None:
        self._settings = settings

    def _load(self) -> list[McpAuditEntry]:
        if self._settings is None:
            return []
        raw = self._settings.get(KEY_MCP_AUDIT_LOG, "[]")
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(items, list):
            return []
        entries = [_entry_from_json(item) for item in items if isinstance(item, dict)]
        return [e for e in entries if e is not None]

    def _write(self, entries: list[McpAuditEntry]) -> None:
        if self._settings is None:
            return
        trimmed = entries[-MAX_AUDIT_ENTRIES:]
        self._settings.set(KEY_MCP_AUDIT_LOG, json.dumps(
            [asdict(e) for e in trimmed], ensure_ascii=False))

    def record(self, *, server_id: str, server_name: str, tool_name: str, sensitivity: str,
              mission_id: int | None, mission_title: str, decision: str, outcome: str,
              duration_ms: float | None = None, error_code: str = "") -> None:
        entries = self._load()
        entries.append(McpAuditEntry(
            timestamp=time.time(), server_id=server_id, server_name=server_name,
            tool_name=tool_name, sensitivity=sensitivity, mission_id=mission_id,
            mission_title=mission_title, decision=decision, outcome=outcome,
            duration_ms=duration_ms, error_code=error_code))
        self._write(entries)

    def entries(self, *, server_id: str | None = None, mission_id: int | None = None,
               allowed: bool | None = None, errors_only: bool = False,
               sensitivity_in: tuple[str, ...] | None = None,
               limit: int | None = None) -> list[McpAuditEntry]:
        """Newest first, optionally filtered. Every filter is an AND."""
        out = list(reversed(self._load()))
        if server_id is not None:
            out = [e for e in out if e.server_id == server_id]
        if mission_id is not None:
            out = [e for e in out if e.mission_id == mission_id]
        if allowed is not None:
            out = [e for e in out if e.allowed == allowed]
        if errors_only:
            out = [e for e in out if e.is_error]
        if sensitivity_in is not None:
            out = [e for e in out if e.sensitivity in sensitivity_in]
        if limit is not None:
            out = out[:limit]
        return out

    def clear(self) -> None:
        self._write([])

    def clear_server(self, server_id: str) -> None:
        self._write([e for e in self._load() if e.server_id != server_id])
