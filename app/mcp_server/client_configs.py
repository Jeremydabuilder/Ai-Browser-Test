"""Config generators for the "Connect an AI" setup flows (Phase 12, Part
5). Pure text/dict generation, no I/O, no storage access, no secrets
persisted anywhere by this module - it only ever formats a token the
caller already holds (typically right after pairing, in the same UI flow
that showed it once) into the shape a given client expects.

This module never writes to another application's configuration file.
Every function returns text meant for a "Copy config" button; the user
pastes it themselves. Keeping config generation here, separate from
server.py/tools.py, is what Part 5 asks for: adapters, not a second
implementation of anything MCP-related.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass

from app.mcp_server.types import Capability

DEFAULT_SERVER_NAME = "pybrowser"


def mcp_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/mcp"


@dataclass
class GeneratedConfig:
    """What a "Set up" card shows: a short explanation, the config text to
    copy, and whether a manual step (paste elsewhere) is still needed."""

    title: str
    instructions: str
    config_text: str
    requires_tunnel: bool = False


def cursor_config(url: str, token: str, server_name: str = DEFAULT_SERVER_NAME) -> GeneratedConfig:
    """Cursor's mcp.json: root key "mcpServers", Streamable HTTP with an
    Authorization header - see docs/external_ai_clients.md for the
    verified support level."""
    payload = {
        "mcpServers": {
            server_name: {
                "url": url,
                "type": "streamableHttp",
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }
    return GeneratedConfig(
        title="Cursor",
        instructions=(
            "In Cursor, open Settings -> MCP -> Add new MCP server, or paste this into "
            "your mcp.json (Cursor Settings > MCP). Cursor connects directly over "
            "Streamable HTTP - no tunnel needed since Cursor runs on this machine."),
        config_text=json.dumps(payload, indent=2))


def vscode_config(url: str, token: str, server_name: str = DEFAULT_SERVER_NAME) -> GeneratedConfig:
    """VS Code's mcp.json: root key "servers" (NOT "mcpServers" - the
    single most common copy-paste mistake between editors, per current
    docs), same Streamable HTTP + Authorization header shape."""
    payload = {
        "servers": {
            server_name: {
                "url": url,
                "type": "http",
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }
    return GeneratedConfig(
        title="VS Code",
        instructions=(
            'Run "MCP: Open User Configuration" from the Command Palette, or create '
            ".vscode/mcp.json in your workspace, and paste this in. VS Code connects "
            "directly over Streamable HTTP - no tunnel needed."),
        config_text=json.dumps(payload, indent=2))


def claude_desktop_bridge_config(
    url: str, token: str, server_name: str = DEFAULT_SERVER_NAME,
) -> GeneratedConfig:
    """Claude Desktop's remote-connector path requires a publicly
    reachable server and defaults to OAuth (see
    docs/external_ai_clients.md) - neither fits a localhost, bearer-token
    server. Claude Desktop's LOCAL server path, however, spawns a command
    over stdio, which is exactly what the optional bridge
    (app/mcp_server/stdio_bridge.py) adapts to this server - no browser
    or Mission logic duplicated, transport only."""
    payload = {
        "mcpServers": {
            server_name: {
                "command": sys.executable or "python3",
                "args": ["-m", "app.mcp_server.stdio_bridge"],
                "env": {"PYBROWSER_MCP_URL": url, "PYBROWSER_MCP_TOKEN": token},
            }
        }
    }
    return GeneratedConfig(
        title="Claude Desktop",
        instructions=(
            "Claude Desktop's remote-connector flow needs a public HTTPS server and "
            "OAuth, which does not fit a local, bearer-token server. Instead, add this "
            "to claude_desktop_config.json (Claude menu -> Settings -> Developer -> Edit "
            "Config) - Claude Desktop will launch a small local bridge process that "
            "forwards to this same PyBrowser MCP server; no browser or Mission logic "
            "is duplicated."),
        config_text=json.dumps(payload, indent=2))


def chatgpt_connector_info(url: str) -> GeneratedConfig:
    """ChatGPT's Developer Mode custom connectors require an HTTPS,
    publicly reachable MCP server (see docs/external_ai_clients.md) -
    PyBrowser's server is deliberately localhost-only, so this is always
    "requires tunnel": PyBrowser does not provide or manage a tunnel
    itself. The user must supply their own (e.g. a personal reverse
    proxy) and accepts that trade-off explicitly."""
    return GeneratedConfig(
        title="ChatGPT",
        instructions=(
            "ChatGPT's custom connectors (Settings -> Apps & Connectors -> Developer "
            "mode, on a paid plan) only reach servers over the public internet via "
            "HTTPS - it cannot reach a localhost address directly. PyBrowser will not "
            "create a public tunnel for you. If you choose to expose this local MCP "
            "server through your own HTTPS tunnel, use its public URL "
            f"({url.replace('127.0.0.1', '<your-tunnel-host>')}) as the connector's MCP "
            "server URL, and choose \"Token\" auth with the pairing token as the bearer "
            "token. Understand that doing this exposes the endpoint beyond this machine "
            "- only do this if you trust the tunnel."),
        config_text="",
        requires_tunnel=True)


def generic_client_info(url: str, token: str, capabilities: list[str]) -> GeneratedConfig:
    """For any other MCP-capable client: the raw connection facts, not a
    guess at that client's config file format."""
    labels = [Capability.labels().get(Capability(c), c) for c in capabilities]
    payload = {
        "endpoint": url,
        "transport": "Streamable HTTP (JSON-RPC 2.0)",
        "authorization": {"scheme": "Bearer", "header": "Authorization",
                          "value": f"Bearer {token}"},
        "scopes": labels,
    }
    return GeneratedConfig(
        title="Custom MCP Client",
        instructions=(
            "Any MCP client that speaks Streamable HTTP with a Bearer Authorization "
            "header can connect using these details."),
        config_text=json.dumps(payload, indent=2))
