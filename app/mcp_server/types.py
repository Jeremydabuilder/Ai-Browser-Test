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


class ClientType(str, Enum):
    """Which "Connect an AI" card paired this client - purely descriptive
    metadata for the Settings UI and config generators (Phase 12). Every
    type shares the SAME auth/permission/audit machinery; this never
    branches security behavior."""

    CHATGPT = "chatgpt"
    CLAUDE = "claude"
    CURSOR = "cursor"
    VSCODE = "vscode"
    GENERIC = "generic"

    @classmethod
    def labels(cls) -> dict["ClientType", str]:
        return {
            cls.CHATGPT: "ChatGPT", cls.CLAUDE: "Claude", cls.CURSOR: "Cursor",
            cls.VSCODE: "VS Code", cls.GENERIC: "Custom MCP Client",
        }


class ConnectionMethod(str, Enum):
    """How a client reaches the PyBrowser MCP server - see
    app/mcp_server/client_configs.py for what each one generates."""

    DIRECT_HTTP = "direct_http"          # Streamable HTTP straight to 127.0.0.1
    STDIO_BRIDGE = "stdio_bridge"        # app/mcp_server/stdio_bridge.py
    REMOTE_TUNNEL = "remote_tunnel"      # user-supplied HTTPS tunnel (opt-in)
    UNSET = ""


class VerificationStatus(str, Enum):
    """One shared vocabulary for "is this client actually connected" - see
    app/mcp_server/verification.py. Pairing a client only ever starts it
    at NOT_CONFIGURED; nothing here is set except by a real verification
    attempt or an explicit config-generation step."""

    NOT_CONFIGURED = "not_configured"
    CONFIG_GENERATED = "config_generated"
    REQUIRES_TUNNEL = "requires_tunnel"
    VERIFIED = "verified"
    AUTHENTICATION_FAILED = "authentication_failed"
    UNREACHABLE = "unreachable"

    @classmethod
    def labels(cls) -> dict["VerificationStatus", str]:
        return {
            cls.NOT_CONFIGURED: "Not configured", cls.CONFIG_GENERATED: "Config generated",
            cls.REQUIRES_TUNNEL: "Requires tunnel", cls.VERIFIED: "Verified",
            cls.AUTHENTICATION_FAILED: "Authentication failed",
            cls.UNREACHABLE: "Unreachable",
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
    client_type: str = ClientType.GENERIC.value
    connection_method: str = ""
    last_verified_at: str | None = None
    last_verified_status: str = VerificationStatus.NOT_CONFIGURED.value

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
