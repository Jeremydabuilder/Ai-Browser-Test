"""Shared types for the PyBrowser MCP server: the capability model, and the
plain data records that flow to/from app/storage/mcp_server_store.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Capability(str, Enum):
    """The least-privilege permission categories an external client can be
    granted. A newly paired client starts with none of these - see
    app/mcp_server/auth.py's pair_client."""

    READ_PAGES = "read_pages"
    READ_TABS = "read_tabs"
    NAVIGATE = "navigate"
    OPEN_TABS = "open_tabs"
    CREATE_MISSION = "create_mission"
    READ_MISSIONS = "read_missions"

    @classmethod
    def labels(cls) -> dict["Capability", str]:
        return {
            cls.READ_PAGES: "Read pages",
            cls.READ_TABS: "Read tabs",
            cls.NAVIGATE: "Navigate",
            cls.OPEN_TABS: "Open tabs",
            cls.CREATE_MISSION: "Create Missions",
            cls.READ_MISSIONS: "Read Mission data",
        }


@dataclass
class PairedClient:
    id: str
    display_name: str
    token_hash: str
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    created_at: str = ""
    last_used_at: str | None = None
    revoked: bool = False

    def has(self, capability: Capability | str) -> bool:
        value = capability.value if isinstance(capability, Capability) else capability
        return value in self.capabilities


@dataclass
class AuditEntry:
    id: int
    client_id: str | None
    tool: str
    outcome: str
    duration_ms: int
    approval_result: str | None
    detail: str
    created_at: str
