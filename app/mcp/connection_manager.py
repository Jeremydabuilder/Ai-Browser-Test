"""The MCP connection manager - the only Qt-aware module in app/mcp.

Owns one background thread running a persistent asyncio event loop (every
configured server's protocol client lives on that loop, whichever transport
it uses), and marshals every result back onto the GUI thread via a Qt
queued signal before resolving the BrowserFuture the rest of the agent
system already knows how to consume - the same cross-thread pattern
app.agent.session's `_ClaudeWorker` uses for the Claude worker thread,
applied here without needing a QThread, since nothing MCP-side needs Qt's
own event loop.

Nothing in app/agent/tools.py or app/agent/session.py needs to know any of
this happened: they hold one `McpConnectionManager` reference and call
`schemas()` / `knows()` / `run_tool()` / `describe_call()` on it, the exact
shape ToolRegistry already expects of itself for native tools.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from app.browser.futures import BrowserFuture, resolved
from app.mcp import adapter
from app.mcp.config import McpServerStore, get_secret
from app.mcp.protocol import (
    HttpMcpClient,
    McpProtocolError,
    McpTimeoutError,
    StdioMcpClient,
)
from app.mcp.safety import classify
from app.mcp.types import ConnectionState, McpServerConfig, McpToolDescriptor, Sensitivity, Transport


class McpConnection:
    """Live state for one configured server. Read from the GUI thread only
    (all mutation happens inside the manager's own GUI-thread slots) -
    the background thread never touches this object directly, only the
    protocol client it holds."""

    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self.state = ConnectionState.DISABLED if not config.enabled else ConnectionState.DISCONNECTED
        self.client: StdioMcpClient | HttpMcpClient | None = None
        self.tools: list[McpToolDescriptor] = []
        self.last_error: str = ""
        self.last_connected_at: float | None = None

    @property
    def tool_count(self) -> int:
        return len(self.tools)

    def agent_visible_tools(self) -> list[McpToolDescriptor]:
        return [t for t in self.tools if t.agent_visible]


def _build_client(config: McpServerConfig) -> StdioMcpClient | HttpMcpClient:
    if config.transport == Transport.STDIO:
        env = dict(os.environ)
        env.update(config.env)
        for var_name in config.secret_env_vars:
            secret = get_secret(config.id)
            if secret:
                env[var_name] = secret
        return StdioMcpClient(config.command, config.args, env)
    if config.transport == Transport.STREAMABLE_HTTP:
        headers = {}
        if config.auth_header:
            secret = get_secret(config.id)
            if secret:
                headers[config.auth_header] = secret
        return HttpMcpClient(config.url, headers)
    raise ValueError(f"unsupported transport: {config.transport!r}")


class McpConnectionManager(QObject):
    """Configured servers, their live connections, and the bridge that lets
    ToolRegistry treat an MCP tool call like any other asynchronous tool."""

    #: A server's connection state or tool list changed - the Settings UI
    #: reconnects to this to refresh its rows without polling.
    server_changed = Signal(str)  # server_id

    #: Internal: background-thread -> GUI-thread handoff. Never connect to
    #: this from outside the class; it exists purely so a coroutine running
    #: on the background loop can resolve a BrowserFuture (and touch
    #: McpConnection state) on the GUI thread instead of its own.
    _bg_result = Signal(object)  # a zero-arg callable to run on the GUI thread

    def __init__(self, store: McpServerStore, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._connections: dict[str, McpConnection] = {
            server.id: McpConnection(server) for server in store.list_servers()
        }
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, name="mcp-io", daemon=True)
        self._thread.start()
        self._loop_ready.wait(timeout=5.0)
        self._bg_result.connect(self._run_on_gui_thread)
        # Reconnect whatever was enabled last time, so the user does not have
        # to re-press Connect on every launch. Each attempt is independent
        # and posts its own success/error back through the usual path, so
        # one server being offline never blocks the others or startup itself.
        for server_id, connection in self._connections.items():
            if connection.config.enabled:
                self.connect_server(server_id)

    # -- background loop lifecycle -----------------------------------------
    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._loop_ready.set()
        loop.run_forever()

    def shutdown(self) -> None:
        """Close every connection and stop the background loop. Call once,
        at application exit."""
        if self._loop is None:
            return
        for connection in self._connections.values():
            client = connection.client
            if client is not None:
                asyncio.run_coroutine_threadsafe(client.close(), self._loop)
        self._loop.call_soon_threadsafe(self._loop.stop)

    def _run_on_gui_thread(self, callback: Callable[[], None]) -> None:
        callback()

    def _post_to_gui_thread(self, callback: Callable[[], None]) -> None:
        """Called from the background thread - queues ``callback`` to run on
        the GUI thread via the Qt signal above."""
        self._bg_result.emit(callback)

    # -- server configuration -----------------------------------------------
    def configured_servers(self) -> list[McpServerConfig]:
        return [c.config for c in self._connections.values()]

    def connection(self, server_id: str) -> McpConnection | None:
        return self._connections.get(server_id)

    def state(self, server_id: str) -> str:
        connection = self._connections.get(server_id)
        return connection.state if connection else ConnectionState.DISCONNECTED

    def add_or_update_server(self, config: McpServerConfig) -> None:
        self._store.save_server(config)
        existing = self._connections.get(config.id)
        if existing is not None and existing.client is not None:
            self._disconnect_internal(config.id)
        self._connections[config.id] = McpConnection(config)
        self.server_changed.emit(config.id)

    def remove_server(self, server_id: str) -> None:
        self._disconnect_internal(server_id)
        self._store.remove_server(server_id)
        self._connections.pop(server_id, None)
        self.server_changed.emit(server_id)

    def set_enabled(self, server_id: str, enabled: bool) -> None:
        connection = self._connections.get(server_id)
        if connection is None:
            return
        self._store.set_enabled(server_id, enabled)
        connection.config = self._store.get_server(server_id) or connection.config
        if not enabled:
            self._disconnect_internal(server_id)
            connection.state = ConnectionState.DISABLED
        else:
            connection.state = ConnectionState.DISCONNECTED
        self.server_changed.emit(server_id)

    # -- connecting -----------------------------------------------------
    def connect_server(self, server_id: str) -> None:
        connection = self._connections.get(server_id)
        if connection is None or self._loop is None:
            return
        if not connection.config.enabled:
            return
        if connection.state in (ConnectionState.CONNECTING, ConnectionState.CONNECTED):
            return
        connection.state = ConnectionState.CONNECTING
        connection.last_error = ""
        self.server_changed.emit(server_id)
        asyncio.run_coroutine_threadsafe(self._connect_and_discover(server_id), self._loop)

    def reconnect_server(self, server_id: str) -> None:
        self._disconnect_internal(server_id)
        connection = self._connections.get(server_id)
        if connection is not None:
            # _disconnect_internal only drops the client - without this,
            # connect_server's own "already CONNECTED" guard would see the
            # stale state left over from before and silently do nothing,
            # leaving Settings' Reconnect button a no-op after the first use.
            connection.state = ConnectionState.DISCONNECTED
        self.connect_server(server_id)

    def disconnect_server(self, server_id: str) -> None:
        self._disconnect_internal(server_id)
        connection = self._connections.get(server_id)
        if connection is not None:
            connection.state = ConnectionState.DISCONNECTED
            self.server_changed.emit(server_id)

    def _disconnect_internal(self, server_id: str) -> None:
        connection = self._connections.get(server_id)
        if connection is None or connection.client is None or self._loop is None:
            return
        client = connection.client
        connection.client = None
        asyncio.run_coroutine_threadsafe(client.close(), self._loop)

    async def _connect_and_discover(self, server_id: str) -> None:
        connection = self._connections[server_id]
        config = connection.config
        try:
            client = _build_client(config)
            await client.connect(timeout=config.connect_timeout_s)
            raw_tools = await client.list_tools(timeout=config.connect_timeout_s)
            tools = []
            for raw in raw_tools:
                if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
                    continue  # a malformed tool entry is dropped, not raised
                schema = raw.get("inputSchema")
                schema = schema if isinstance(schema, dict) else {}
                tools.append(McpToolDescriptor(
                    server_id=server_id,
                    name=raw["name"],
                    description=str(raw.get("description", "")),
                    input_schema=schema,
                    sensitivity=classify(raw["name"], schema),
                ))
        except McpTimeoutError as exc:
            message = str(exc)
            self._post_to_gui_thread(lambda: self._on_connect_error(server_id, message))
            return
        except McpProtocolError as exc:
            message = str(exc)
            self._post_to_gui_thread(lambda: self._on_connect_error(server_id, message))
            return
        except FileNotFoundError as exc:
            # `as exc` is implicitly deleted at the end of this except block
            # (a Python gotcha), so the exception object itself must never be
            # captured by a closure that runs later - the message is copied
            # into a plain local first, here and in every branch below.
            message = f"command not found: {exc}"
            self._post_to_gui_thread(lambda: self._on_connect_error(server_id, message))
            return
        except Exception as exc:  # noqa: BLE001 - a connect attempt must never crash the loop
            message = f"{type(exc).__name__}: {exc}"
            self._post_to_gui_thread(lambda: self._on_connect_error(server_id, message))
            return
        self._post_to_gui_thread(lambda: self._on_connected(server_id, client, tools))

    def _on_connected(self, server_id: str, client, tools: list[McpToolDescriptor]) -> None:
        connection = self._connections.get(server_id)
        if connection is None:
            return
        connection.client = client
        connection.tools = tools
        connection.state = ConnectionState.CONNECTED
        connection.last_connected_at = time.time()
        connection.last_error = ""
        self.server_changed.emit(server_id)

    def _on_connect_error(self, server_id: str, message: str) -> None:
        connection = self._connections.get(server_id)
        if connection is None:
            return
        connection.state = ConnectionState.ERROR
        connection.last_error = message
        self.server_changed.emit(server_id)

    # -- tool discovery / agent surface -----------------------------------------------
    def all_tools(self, server_id: str) -> list[McpToolDescriptor]:
        connection = self._connections.get(server_id)
        return list(connection.tools) if connection else []

    def find_tool(self, server_id: str, tool_name: str) -> McpToolDescriptor | None:
        for tool in self.all_tools(server_id):
            if tool.name == tool_name:
                return tool
        return None

    def knows(self, namespaced_name: str) -> bool:
        parts = adapter.split_namespaced(namespaced_name)
        if parts is None:
            return False
        server_id, tool_name = parts
        tool = self.find_tool(server_id, tool_name)
        return tool is not None and tool.agent_visible

    def schemas(self) -> list[dict[str, Any]]:
        """READ_ONLY tools from every currently CONNECTED, enabled server -
        the only ones Phase 1 ever exposes to the model."""
        out: list[dict[str, Any]] = []
        for connection in self._connections.values():
            if connection.state != ConnectionState.CONNECTED:
                continue
            for tool in connection.agent_visible_tools():
                out.append(adapter.to_tool_schema(tool))
        return out

    def describe_call(self, namespaced_name: str, args: dict[str, Any]) -> str:
        parts = adapter.split_namespaced(namespaced_name)
        if parts is None:
            return namespaced_name
        server_id, tool_name = parts
        connection = self._connections.get(server_id)
        display = connection.config.name if connection else server_id
        return f"Using {display}: {tool_name}"

    # -- running -----------------------------------------------------
    def run_tool(self, namespaced_name: str, args: dict[str, Any]) -> BrowserFuture:
        parts = adapter.split_namespaced(namespaced_name)
        if parts is None or self._loop is None:
            return resolved("mcp_call", adapter.render_tool_result(
                ok=False, error_code="UNKNOWN_TOOL",
                error_message=f"'{namespaced_name}' is not a known MCP tool."))
        server_id, tool_name = parts
        connection = self._connections.get(server_id)
        if connection is None or connection.state != ConnectionState.CONNECTED:
            return resolved("mcp_call", adapter.render_tool_result(
                ok=False, server_id=server_id, tool_name=tool_name,
                error_code="SERVER_NOT_CONNECTED",
                error_message=f"'{server_id}' is not connected."))
        tool = self.find_tool(server_id, tool_name)
        if tool is None:
            return resolved("mcp_call", adapter.render_tool_result(
                ok=False, server_id=server_id, tool_name=tool_name,
                error_code="UNKNOWN_TOOL", error_message="That tool was not discovered."))
        if not tool.agent_visible:
            # Defence in depth: schemas() never advertises this tool, so the
            # model should not be able to name it - but a tool renamed to
            # look read-only between discovery and this call (see the
            # "server renames tool after reconnect" test) must still be
            # blocked here, not just at discovery time.
            return resolved("mcp_call", adapter.blocked_result(
                server_id=server_id, tool_name=tool_name, sensitivity=tool.sensitivity))

        future = BrowserFuture(f"mcp:{namespaced_name}")
        # A backstop only: the asyncio-level timeout inside call_tool()
        # normally fires first and produces a proper TIMEOUT payload. This
        # one exists purely so a completely wedged background thread (one
        # that never calls back at all) cannot hang the task forever - hence
        # the generous extra margin over the "real" timeout.
        future.set_timeout(int(connection.config.call_timeout_s * 1000) + 5000, lambda: None)
        client = connection.client
        coro = client.call_tool(tool_name, args, timeout=connection.config.call_timeout_s)
        cf = asyncio.run_coroutine_threadsafe(coro, self._loop)

        def on_bg_done(done_future) -> None:
            try:
                raw_result = done_future.result()
            except McpTimeoutError:
                payload = adapter.render_tool_result(
                    ok=False, server_id=server_id, tool_name=tool_name,
                    error_code="TIMEOUT", error_message="The tool did not respond in time.")
            except McpProtocolError as exc:
                # `as exc` is deleted at the end of this except block, so the
                # message must be copied out before the lambda that runs
                # later (on the GUI thread) can safely reference it.
                message = str(exc)
                self._post_to_gui_thread(lambda: self._mark_connection_error(server_id, message))
                payload = adapter.render_tool_result(
                    ok=False, server_id=server_id, tool_name=tool_name,
                    error_code="MCP_ERROR", error_message=message)
            except Exception as exc:  # noqa: BLE001 - a tool call must never crash the loop
                payload = adapter.render_tool_result(
                    ok=False, server_id=server_id, tool_name=tool_name,
                    error_code="MCP_FAILED", error_message=f"{type(exc).__name__}: {exc}")
            else:
                is_error = bool(isinstance(raw_result, dict) and raw_result.get("isError"))
                content = raw_result.get("content") if isinstance(raw_result, dict) else raw_result
                if is_error:
                    payload = adapter.render_tool_result(
                        ok=False, server_id=server_id, tool_name=tool_name,
                        error_code="TOOL_REPORTED_ERROR",
                        error_message=adapter.render_call_content(content))
                else:
                    payload = adapter.render_tool_result(
                        ok=True, server_id=server_id, tool_name=tool_name, content=content)
            self._post_to_gui_thread(lambda: future.set_result(payload))

        cf.add_done_callback(on_bg_done)
        return future

    def _mark_connection_error(self, server_id: str, message: str) -> None:
        """A tool call revealed the connection is actually dead - reflect
        that in state immediately rather than waiting for the user to
        notice on a stale "Connected" row. Does not auto-reconnect: see the
        architecture doc's note that Phase 1 reconnection is user-triggered,
        not an unattended retry loop."""
        connection = self._connections.get(server_id)
        if connection is None:
            return
        connection.state = ConnectionState.ERROR
        connection.last_error = message
        connection.client = None
        self.server_changed.emit(server_id)
