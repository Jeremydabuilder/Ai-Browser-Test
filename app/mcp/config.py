"""Persistence for configured MCP servers.

Non-secret configuration (name, transport, command/URL, enabled state) is a
JSON blob in the ordinary settings table - it is preference data, not a
secret, the same distinction app/agent/config.py already draws for the
provider/model/effort choices. Any actual secret (a bearer token, a stdio
server's API key env var) is never part of that JSON: it is stored in the
OS keyring, one entry per server, reusing the exact keyring plumbing
app/agent/keys.py already has rather than inventing a second one.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any

from app.agent.keys import KeyringUnavailable, _guard, _keyring  # noqa: PLC2701 - reused, not duplicated
from app.mcp.types import McpServerConfig, Transport
from app.storage.settings import SettingsStore

KEY_MCP_SERVERS = "mcp_servers"

_KEYRING_SERVICE_PREFIX = "PyBrowser-MCP"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """A stable id from a display name - "GitHub MCP" -> "github-mcp"."""
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "server"


def _server_to_json(server: McpServerConfig) -> dict[str, Any]:
    data = asdict(server)
    data["args"] = list(data["args"])
    data["secret_env_vars"] = list(data["secret_env_vars"])
    return data


def _server_from_json(data: dict[str, Any]) -> McpServerConfig | None:
    try:
        return McpServerConfig(
            id=str(data["id"]),
            name=str(data.get("name", data["id"])),
            transport=str(data.get("transport", Transport.STDIO)),
            enabled=bool(data.get("enabled", True)),
            command=str(data.get("command", "")),
            args=tuple(str(a) for a in data.get("args", [])),
            env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
            secret_env_vars=tuple(str(v) for v in data.get("secret_env_vars", [])),
            url=str(data.get("url", "")),
            auth_header=str(data.get("auth_header", "")),
            call_timeout_s=float(data.get("call_timeout_s", 30.0)),
            connect_timeout_s=float(data.get("connect_timeout_s", 10.0)),
        )
    except (KeyError, TypeError, ValueError):
        # A corrupted or hand-edited settings row must not crash startup -
        # the same "a bad preference is never load-bearing" rule
        # AgentConfig.from_environment already follows.
        return None


class McpServerStore:
    """CRUD for configured servers, plus their secrets.

    ``settings`` may be None (no profile database available yet, e.g. very
    early in some test harnesses) - every method degrades to an empty list
    / no-op rather than raising, the same defensive posture every other
    settings-backed reader in this codebase already takes.
    """

    def __init__(self, settings: SettingsStore | None) -> None:
        self._settings = settings

    @property
    def settings(self) -> SettingsStore | None:
        """The underlying store, so a sibling module (permissions.py, via
        McpConnectionManager) can keep its own JSON blob in the same
        database without main_window.py having to construct and pass a
        second object around."""
        return self._settings

    def list_servers(self) -> list[McpServerConfig]:
        if self._settings is None:
            return []
        raw = self._settings.get(KEY_MCP_SERVERS, "[]")
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(items, list):
            return []
        servers = [_server_from_json(item) for item in items if isinstance(item, dict)]
        return [s for s in servers if s is not None]

    def get_server(self, server_id: str) -> McpServerConfig | None:
        for server in self.list_servers():
            if server.id == server_id:
                return server
        return None

    def save_server(self, server: McpServerConfig) -> None:
        """Insert or replace, by id."""
        if self._settings is None:
            return
        servers = [s for s in self.list_servers() if s.id != server.id]
        servers.append(server)
        self._write(servers)

    def remove_server(self, server_id: str) -> None:
        if self._settings is None:
            return
        servers = [s for s in self.list_servers() if s.id != server_id]
        self._write(servers)
        clear_secret(server_id)

    def set_enabled(self, server_id: str, enabled: bool) -> None:
        server = self.get_server(server_id)
        if server is None:
            return
        self.save_server(_replace_enabled(server, enabled))

    def _write(self, servers: list[McpServerConfig]) -> None:
        if self._settings is None:
            return
        self._settings.set(KEY_MCP_SERVERS, json.dumps(
            [_server_to_json(s) for s in servers], ensure_ascii=False))


def _replace_enabled(server: McpServerConfig, enabled: bool) -> McpServerConfig:
    from dataclasses import replace
    return replace(server, enabled=enabled)


# ---------------------------------------------------------------------------
# Secrets - one keyring entry per server, never touching the settings table.
# ---------------------------------------------------------------------------


def _service_name(server_id: str) -> str:
    return f"{_KEYRING_SERVICE_PREFIX}-{server_id}"


def get_secret(server_id: str) -> str | None:
    """The stored secret for a server (an HTTP bearer token, a stdio API
    key), or None if none is stored or the keyring is unavailable. Never
    raises - the same defensive read every keyring access in this codebase
    uses."""
    try:
        value = _keyring().get_password(_service_name(server_id), "secret")
        return value.strip() if value else None
    except KeyringUnavailable:
        return None
    except BaseException as exc:  # noqa: BLE001
        _guard(exc)
        return None


def set_secret(server_id: str, value: str) -> None:
    """Raises KeyringUnavailable if the OS keyring cannot be used - the
    caller (the Add Server UI) is expected to surface that rather than
    silently pretend the secret was stored."""
    value = (value or "").strip()
    if not value:
        raise ValueError("The secret is empty.")
    try:
        _keyring().set_password(_service_name(server_id), "secret", value)
    except KeyringUnavailable:
        raise
    except BaseException as exc:  # noqa: BLE001
        raise _guard(exc) from exc


def clear_secret(server_id: str) -> None:
    try:
        _keyring().delete_password(_service_name(server_id), "secret")
    except BaseException as exc:  # noqa: BLE001 - deleting a missing secret is fine
        _guard(exc)


def has_secret(server_id: str) -> bool:
    return get_secret(server_id) is not None
