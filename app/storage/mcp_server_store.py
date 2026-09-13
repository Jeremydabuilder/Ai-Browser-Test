"""Persistence for Phase 11 (PyBrowser as an MCP Server): paired external
clients and the audit trail of what they did - see app/mcp_server/auth.py
and app/mcp_server/audit.py for the code that reads/writes through here.

Only a token's SHA-256 hash is ever stored (never the plaintext pairing
token, never a keyring-retrievable secret) - see PairedClient.token_hash
and app/mcp_server/auth.py's verify_token. Audit rows are meant to be safe
to display in the Settings "View activity" list as-is: callers are expected
to have already redacted anything sensitive before calling record().
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.storage.database import Database

if TYPE_CHECKING:
    from app.mcp_server.types import AuditEntry, PairedClient

MAX_DETAIL_CHARS = 2000


def _row_to_client(row) -> "PairedClient":
    from app.mcp_server.types import PairedClient

    return PairedClient(
        id=row["id"], display_name=row["display_name"], token_hash=row["token_hash"],
        capabilities=tuple(json.loads(row["capabilities"] or "[]")),
        created_at=row["created_at"], last_used_at=row["last_used_at"],
        revoked=bool(row["revoked"]), client_type=row["client_type"],
        connection_method=row["connection_method"],
        last_verified_at=row["last_verified_at"],
        last_verified_status=row["last_verified_status"],
    )


def _row_to_audit(row) -> "AuditEntry":
    from app.mcp_server.types import AuditEntry

    return AuditEntry(
        id=row["id"], client_id=row["client_id"], tool=row["tool"], outcome=row["outcome"],
        duration_ms=row["duration_ms"], approval_result=row["approval_result"],
        detail=row["detail"], created_at=row["created_at"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class McpServerAccessStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- paired clients ----------------------------------------------------

    def create_client(
        self, client_id: str, *, display_name: str, token_hash: str,
        capabilities: list[str] | tuple[str, ...] = (),
        client_type: str = "generic", connection_method: str = "",
    ) -> "PairedClient | None":
        self._db.execute(
            "INSERT INTO mcp_server_clients (id, display_name, token_hash, capabilities, "
            "created_at, revoked, client_type, connection_method) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
            (client_id, display_name, token_hash, json.dumps(list(capabilities)), _now(),
             client_type, connection_method))
        return self.get_client(client_id)

    def get_client(self, client_id: str) -> "PairedClient | None":
        row = self._db.query_one("SELECT * FROM mcp_server_clients WHERE id = ?", (client_id,))
        return _row_to_client(row) if row is not None else None

    def list_clients(self) -> list["PairedClient"]:
        rows = self._db.query("SELECT * FROM mcp_server_clients ORDER BY created_at ASC")
        return [_row_to_client(row) for row in rows]

    def find_by_token_hash(self, token_hash: str) -> "PairedClient | None":
        row = self._db.query_one(
            "SELECT * FROM mcp_server_clients WHERE token_hash = ?", (token_hash,))
        return _row_to_client(row) if row is not None else None

    def touch_last_used(self, client_id: str) -> None:
        self._db.execute(
            "UPDATE mcp_server_clients SET last_used_at = ? WHERE id = ?", (_now(), client_id))

    def revoke(self, client_id: str) -> None:
        self._db.execute(
            "UPDATE mcp_server_clients SET revoked = 1 WHERE id = ?", (client_id,))

    def set_capabilities(self, client_id: str, capabilities: list[str]) -> None:
        self._db.execute(
            "UPDATE mcp_server_clients SET capabilities = ? WHERE id = ?",
            (json.dumps(list(capabilities)), client_id))

    def record_verification(self, client_id: str, status: str) -> None:
        """The ONE place a client's verification state changes - always the
        outcome of a real check (app/mcp_server/verification.py), never a
        side effect of pairing or config generation."""
        self._db.execute(
            "UPDATE mcp_server_clients SET last_verified_at = ?, last_verified_status = ? "
            "WHERE id = ?", (_now(), status, client_id))

    def set_connection_method(self, client_id: str, connection_method: str) -> None:
        self._db.execute(
            "UPDATE mcp_server_clients SET connection_method = ? WHERE id = ?",
            (connection_method, client_id))

    # -- audit log -----------------------------------------------------------

    def record(
        self, *, client_id: str | None, tool: str, outcome: str, duration_ms: int = 0,
        approval_result: str | None = None, detail: str = "",
    ) -> None:
        self._db.execute(
            "INSERT INTO mcp_server_audit (client_id, tool, outcome, duration_ms, "
            "approval_result, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (client_id, tool, outcome, duration_ms, approval_result,
             detail[:MAX_DETAIL_CHARS], _now()))

    def recent_audit(self, limit: int = 200) -> list["AuditEntry"]:
        rows = self._db.query(
            "SELECT * FROM mcp_server_audit ORDER BY created_at DESC, id DESC LIMIT ?", (limit,))
        return [_row_to_audit(row) for row in rows]
