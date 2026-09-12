"""Plain data types shared across the MCP package. No Qt, no I/O.

Kept separate from connection_manager.py and protocol.py so tests (and the
adapter, and the safety classifier) can import these without dragging in
asyncio, subprocess, or Qt at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class Transport:
    """The two transports MCP currently specifies. No others are supported -
    in particular, no bare HTTP+SSE dual-endpoint transport, which the
    current spec (2026-07-28) treats as legacy."""

    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"

    ALL = (STDIO, STREAMABLE_HTTP)


class ConnectionState:
    """Health of one configured server's connection.

    Deliberately more granular than "connected/disconnected" - see the
    request for the exact vocabulary the Settings UI needs.
    """

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    AUTH_REQUIRED = "auth_required"
    ERROR = "error"
    RECONNECTING = "reconnecting"
    DISABLED = "disabled"


class Sensitivity:
    """How consequential an MCP tool looks, judged by PyBrowser - never by
    the server's own description. See app/mcp/safety.py.

    Mirrors app/browser/safety.py's NORMAL/ELEVATED/SENSITIVE three-tier
    model in spirit, but MCP tools get a finer WRITE/DESTRUCTIVE split and
    an explicit UNKNOWN, because there is no DOM to fall back on the way
    browser actions have - a tool this classifier cannot place at all must
    fail closed, not default to "probably fine".
    """

    READ_ONLY = "read_only"
    WRITE = "write"
    SENSITIVE = "sensitive"
    DESTRUCTIVE = "destructive"
    UNKNOWN = "unknown"

    #: Phase 1 exposes only this to the agent loop. Everything else is
    #: discovered, shown in Settings, and refused before it ever reaches
    #: ToolRegistry.
    AGENT_VISIBLE = frozenset({READ_ONLY})


@dataclass(frozen=True)
class McpToolDescriptor:
    """One tool a connected server advertised via tools/list."""

    server_id: str
    name: str                       #: the server's own name, unnamespaced
    description: str = ""           #: untrusted - never shown as instructions to the model
    input_schema: dict[str, Any] = field(default_factory=dict)
    sensitivity: str = Sensitivity.UNKNOWN

    @property
    def namespaced_name(self) -> str:
        """``mcp.<server_id>.<tool_name>`` - see the adapter for why."""
        return f"mcp.{self.server_id}.{self.name}"

    @property
    def agent_visible(self) -> bool:
        return self.sensitivity in Sensitivity.AGENT_VISIBLE


@dataclass(frozen=True)
class McpServerConfig:
    """One configured server. Persisted via app/mcp/config.py.

    Secrets never live on this object once loaded from disk in plaintext -
    ``auth_secret_ref`` names a keyring entry (see config.py), it is not the
    secret itself. ``env`` holds only non-secret environment variables for a
    stdio server; a secret env var is named in ``secret_env_vars`` and its
    value is fetched from the keyring at spawn time, never stored in ``env``.
    """

    id: str
    name: str
    transport: str                  #: one of Transport.ALL
    enabled: bool = True

    # stdio
    command: str = ""
    args: tuple[str, ...] = field(default_factory=tuple)
    env: dict[str, str] = field(default_factory=dict)
    #: Names of environment variables a stdio server needs that hold a
    #: secret - the value is stored in the keyring under this server's own
    #: service name, never here.
    secret_env_vars: tuple[str, ...] = field(default_factory=tuple)

    # streamable HTTP
    url: str = ""
    #: Name of the HTTP header a secret is sent as (e.g. "Authorization"),
    #: when the server needs one. The value itself lives in the keyring.
    auth_header: str = ""

    #: Per-call timeout, seconds. Generous default: an external tool call
    #: (a real GitHub/Drive-style API round trip) is not a page load.
    call_timeout_s: float = 30.0
    #: Handshake/connect timeout, seconds.
    connect_timeout_s: float = 10.0


@dataclass
class McpCallResult:
    """The outcome of one tools/call, before it is rendered for the model."""

    ok: bool
    #: Plain text/JSON content the tool returned - already the server's own
    #: structured content, never executed or treated as instructions.
    content: Any = None
    error_code: str = ""
    error_message: str = ""
    #: True if the server itself reported isError, distinguishing "the tool
    #: ran and reported failure" from a transport-level failure.
    tool_reported_error: bool = False
