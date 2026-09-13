"""Safe-summary audit logging for external MCP calls - a thin wrapper over
app/storage/mcp_server_store.py's ``record``. Every field it writes is
already something safe to show verbatim in the Settings "View activity"
list: a tool name, an outcome keyword, a duration, an approval result, and
a short detail string this module itself builds - never a raw secret,
never a full page payload.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.storage.mcp_server_store import McpServerAccessStore

MAX_DETAIL_CHARS = 200


def record_call(
    store: "McpServerAccessStore", *, client_id: str | None, tool: str, outcome: str,
    duration_ms: int, approval_result: str | None = None, detail: str = "",
) -> None:
    """``outcome`` is one of a small fixed vocabulary - "ok", "denied",
    "error", "unauthenticated", "forbidden" - never the tool's actual
    result payload. ``detail`` is a short, already-safe summary (an error
    code, a tool name) - callers must not pass page text or tokens here."""
    store.record(
        client_id=client_id, tool=tool, outcome=outcome, duration_ms=duration_ms,
        approval_result=approval_result, detail=detail[:MAX_DETAIL_CHARS])
