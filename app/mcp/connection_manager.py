"""The MCP connection manager - the only Qt-aware module in app/mcp.

Owns one background thread running a persistent asyncio event loop (every
configured server's protocol client lives on that loop, whichever transport
it uses), and marshals every result back onto the GUI thread via
GuiDispatcher (app/gui_dispatch.py) before resolving the BrowserFuture the
rest of the agent system already knows how to consume, applied here
without needing a QThread, since nothing MCP-side needs Qt's own event
loop.

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
from app.gui_dispatch import GuiDispatchShutdown, GuiDispatcher
from app.security import firewall
from app.security.log import EventType as SecurityEventType
from app.security.log import security_log
from app.mcp import adapter
from app.mcp.audit import (
    DECISION_ALLOWED_ONCE,
    DECISION_ALLOWED_REMEMBERED,
    DECISION_AUTO,
    DECISION_DENIED_ONCE,
    DECISION_DENIED_REMEMBERED,
    OUTCOME_ERROR,
    OUTCOME_NOT_EXECUTED,
    OUTCOME_SUCCESS,
    OUTCOME_TIMEOUT,
    McpAuditStore,
)
from app.mcp.config import McpServerStore, get_secret
from app.mcp.permissions import McpPermissionStore
from app.mcp.protocol import (
    HttpMcpClient,
    McpProtocolError,
    McpTimeoutError,
    StdioMcpClient,
)
from app.mcp.safety import (
    SENSITIVE_SHAPED_FIELD_NAMES,
    classify,
    default_permission,
    describe_effect,
    reason_fragment,
)
from app.mcp.types import (
    ConnectionState,
    McpServerConfig,
    McpToolDescriptor,
    Permission,
    Scope,
    Sensitivity,
    Transport,
)

# Temporary diagnostic instrumentation for the McpConnectionManager
# shutdown/asyncio-lifecycle investigation (background loop thread still
# running at interpreter exit, causing hangs and a segfault under full-
# suite load). Off by default (PYBROWSER_MCP_SHUTDOWN_DIAG unset) - a
# normal run never touches this. Never logs secrets: only thread ids,
# loop state booleans, and task/client counts. Remove once the fix is
# validated.
_SHUTDOWN_DIAG = os.environ.get("PYBROWSER_MCP_SHUTDOWN_DIAG") == "1"


def _diag(msg: str) -> None:
    if _SHUTDOWN_DIAG:
        import sys
        print(f"MCPSHUTDOWNDIAG [{threading.current_thread().name}/{threading.get_ident()}] {msg}",
              file=sys.stderr, flush=True)


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
        """Every discovered tool - kept as its own method (rather than
        inlining ``connection.tools`` at call sites) because Phase 1 code
        and the Settings UI both call it, and its meaning has already
        changed once (Phase 1: read-only only; Phase 2: everything,
        approval-gated) without either caller needing an edit."""
        return list(self.tools)


def _redact_arguments(args: dict[str, Any]) -> dict[str, Any]:
    """The call's arguments, as shown on an approval prompt - with any
    field whose *name* looks like a secret replaced, never the field itself
    dropped silently (a missing field reads as "nothing is sent there",
    which is worse than an honest "redacted"). Values are otherwise shown
    in full: unlike a web form's submitted fields, an MCP call's arguments
    are exactly what the model chose to send, already visible in the
    transcript - hiding them here would only make the approval prompt less
    informative than the conversation right above it.
    """
    if not isinstance(args, dict):
        return {}
    return {
        key: ("•••" if str(key).lower() in SENSITIVE_SHAPED_FIELD_NAMES else value)
        for key, value in args.items()
    }


def _redact_outbound_value(value: Any) -> Any:
    if isinstance(value, str):
        redacted, _ = firewall.redact(value, only_high_risk=True)
        return redacted
    if isinstance(value, dict):
        return {key: _redact_outbound_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_outbound_value(item) for item in value]
    return value


def _redact_outbound_args(args: dict[str, Any], *, server_id: str, tool_name: str) -> dict[str, Any]:
    """Content-based redaction (not the field-name-based ``_redact_arguments``
    above, which is display-only) - a value the model copied in from page
    or file content could carry a secret regardless of what the argument
    happens to be called. Only HIGH-risk categories (credentials, payment,
    authentication data - see app.security.detectors.RiskLevel) are ever
    silently redacted; nothing here blocks the call outright.
    """
    if not firewall.is_enabled() or not isinstance(args, dict):
        return args
    total_findings: list[Any] = []
    for value in args.values():
        if isinstance(value, str):
            total_findings.extend(firewall.scan(value))
    high_risk = [f for f in total_findings if f.risk == firewall.RiskLevel.HIGH]
    if not high_risk:
        return args
    security_log.record(
        SecurityEventType.SECRET_REDACTED, firewall.summarize(high_risk),
        source=f"mcp:{server_id}.{tool_name}")
    return {key: _redact_outbound_value(value) for key, value in args.items()}


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

    #: A *live call* (not a fresh connect attempt) discovered the connection
    #: is dead - server_id, server display name, the tool that was running.
    #: Distinct from server_changed: this is the one moment worth
    #: interrupting the user for ("GitHub disconnected while Py was reading
    #: repository data"), not just a status row quietly turning red.
    connection_dropped = Signal(str, str, str)

    #: Internal: background-thread -> GUI-thread handoff, via GuiDispatcher
    #: (app/gui_dispatch.py) - see _post_to_gui_thread below. Exists purely
    #: so a coroutine running on the background loop can resolve a
    #: BrowserFuture (and touch McpConnection state) on the GUI thread
    #: instead of its own.
    #:
    #: This used to be a Qt Signal(object) carrying the callback itself
    #: across threads - the same design app.mcp_server.server.GuiBridge
    #: had, and the same one that produced confirmed native crashes on
    #: both platforms in Qt's own queued-event delivery (see GuiDispatcher's
    #: docstring for the exact backtraces). Replaced with the identical
    #: queue+QTimer dispatcher GuiBridge now uses - nothing Qt-shaped
    #: crosses the thread boundary here either.

    def __init__(self, store: McpServerStore, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._permissions = McpPermissionStore(store.settings)
        self._audit = McpAuditStore(store.settings)
        self._connections: dict[str, McpConnection] = {
            server.id: McpConnection(server) for server in store.list_servers()
        }
        #: Phase 17: None = every connected server visible (the default,
        #: unchanged from before Workspaces existed) - see
        #: set_workspace_visibility.
        self._visible_server_ids: frozenset[str] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._gui_dispatcher = GuiDispatcher()
        #: Set (GUI thread only) the moment shutdown() begins, so every
        #: entry point that would otherwise schedule new work onto the
        #: loop (connect_server/run_tool/_disconnect_internal) can refuse
        #: it instead - see the shutdown contract docstring below.
        self._shutting_down = False
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False
        self._thread = threading.Thread(target=self._run_loop, name="mcp-io", daemon=True)
        self._thread.start()
        self._loop_ready.wait(timeout=5.0)
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
        _diag(f"_run_loop starting id={threading.get_ident()}")
        loop.run_forever()
        _diag(f"_run_loop run_forever() returned id={threading.get_ident()} "
              f"is_running={loop.is_running()} is_closed={loop.is_closed()}")
        # Deliberately NOT closed here: shutdown() closes it after joining
        # this thread (see below) - by then run_forever() has returned
        # (this line), nothing else touches the loop, and the OS thread
        # that ran it is confirmed dead, so there is no thread-affinity
        # question left to get wrong either way; this just keeps every
        # step of the contract in one place instead of split across two
        # threads.

    def shutdown(self) -> None:
        """The one explicit shutdown sequence for this manager, run once,
        synchronously, from the GUI thread (application exit, or a test's
        tearDown): mark shutting down -> reject new work (the
        _shutting_down guards in connect_server/run_tool/
        _disconnect_internal) -> collect and clear every connection's
        client here on the GUI thread (McpConnection state is GUI-thread-
        only by construction, see its docstring) -> hand those clients to
        a cleanup coroutine that runs entirely on the loop's own thread
        (closes each client - which itself terminates/waits/kills its
        subprocess and cancels its reader tasks - then cancels and awaits
        every task still left on this loop, which is dedicated to this
        manager alone so nothing else could ever be on it) -> block until
        that coroutine finishes or times out -> join the background
        thread -> only then close the loop, since closing while the
        thread that ran it might still be mid-callback is exactly the
        native-crash-shaped mistake this whole investigation started
        from -> drop every reference so nothing here can be mistaken for
        still-live state.

        Idempotent: a second call (or one after startup itself failed, or
        after the background thread already exited on its own) is a
        harmless no-op - guarded by _shutdown_lock/_shutdown_done rather
        than by re-checking self._loop, since clearing self._loop is
        itself one of this method's own side effects.

        Never relies on __del__: nothing here is deferred to garbage
        collection, cyclic or otherwise - that is precisely the hazard
        class the sibling Qt/GuiDispatcher investigation (see
        app/gui_dispatch.py) already had to fix twice.
        """
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        self._shutting_down = True
        self._gui_dispatcher.shutdown()
        if self._loop is None or self._thread is None:
            return
        _diag(f"shutdown start thread={self._thread.ident} "
              f"is_running={self._loop.is_running()}")
        # McpConnection.client is documented GUI-thread-only state - read
        # and cleared here, not inside the cleanup coroutine below, which
        # runs on the loop's own background thread.
        clients_to_close = []
        for connection in self._connections.values():
            if connection.client is not None:
                clients_to_close.append(connection.client)
                connection.client = None
        if threading.get_ident() == self._thread.ident:
            # Not a path anything in this codebase takes today (shutdown()
            # is only ever called from the GUI thread) - guarded anyway,
            # since both run_coroutine_threadsafe().result() and
            # self._thread.join() below would deadlock (the latter would
            # raise RuntimeError: cannot join current thread) if it ever
            # were called from the loop's own thread. Schedule what can
            # be done without blocking and return.
            self._loop.call_soon(self._loop.stop)
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._async_shutdown(clients_to_close), self._loop)
            future.result(timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - cleanup must never block stop()/join()
            _diag(f"shutdown _async_shutdown raised/timed out: {exc!r}")
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10.0)
        thread_exited = not self._thread.is_alive()
        _diag(f"shutdown thread join returned exited={thread_exited}")
        if thread_exited:
            # Only safe once the OS thread that ran it is confirmed dead -
            # nothing can still be mid-callback on it by this point.
            self._loop.close()
        _diag(f"shutdown end loop_closed={self._loop.is_closed() if thread_exited else 'unknown'}")
        self._loop = None
        self._thread = None

    async def _async_shutdown(self, clients: list) -> None:
        """Runs entirely on the manager's own dedicated loop/thread - the
        one place every client and task can be touched directly without
        any cross-thread concern. This loop is created fresh in
        _run_loop and never shared with anything else, so every task
        asyncio.all_tasks() finds on it is manager-owned by construction;
        cancelling "everything left" here can never reach into unrelated
        work the way it could on a shared/default loop.
        """
        for client in clients:
            try:
                await asyncio.wait_for(client.close(), timeout=5.0)
            except Exception as exc:  # noqa: BLE001 - one bad close must not skip the rest
                _diag(f"_async_shutdown client.close() failed: {exc!r}")
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks(loop=self._loop) if t is not current]
        _diag(f"_async_shutdown cancelling {len(pending)} pending task(s)")
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # NOT self._loop.stop() directly: run_coroutine_threadsafe's
        # result only reaches shutdown()'s future.result() below via a
        # done-callback the Task machinery schedules with call_soon() as
        # THIS coroutine returns - i.e. for the loop's *next* iteration.
        # Calling stop() synchronously, right here, sets the loop's
        # stopping flag before that next iteration ever runs, so the
        # callback that would chain our result back never fires and
        # shutdown() always sees its 10s timeout instead of a real
        # result (confirmed directly: PYBROWSER_MCP_SHUTDOWN_DIAG showed
        # this coroutine completing - loop.run_forever() returning -
        # while future.result() still reported TimeoutError). Scheduling
        # stop() with call_soon() queues it for that same next iteration,
        # after the chaining callback, instead of pre-empting it.
        self._loop.call_soon(self._loop.stop)

    def _post_to_gui_thread(self, callback: Callable[[], None]) -> None:
        """Called from the background thread - queues ``callback`` to run
        on the GUI thread via GuiDispatcher (fire-and-forget; no caller
        here waits for a result)."""
        try:
            self._gui_dispatcher.post(callback)
        except GuiDispatchShutdown:
            pass  # shutting down - nothing left to deliver this to

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
        self._permissions.forget_server(server_id)
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
        if self._shutting_down:
            return
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
        if self._shutting_down:
            return
        connection = self._connections.get(server_id)
        if connection is None or connection.client is None or self._loop is None:
            return
        client = connection.client
        connection.client = None
        asyncio.run_coroutine_threadsafe(client.close(), self._loop)

    async def _connect_and_discover(self, server_id: str) -> None:
        connection = self._connections[server_id]
        config = connection.config
        client = None
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
        except asyncio.CancelledError:
            # Shutdown cancelled this task mid-handshake - if a client was
            # already constructed (and may already have a live subprocess
            # or HTTP connection open), it was never published to
            # connection.client, so nothing else will ever close it.
            # Closed here, on this same loop/thread, before the
            # cancellation propagates - never left for a subprocess
            # finalizer to run after the loop is gone.
            #
            # Known residual gap: if cancellation lands while still
            # suspended *inside* asyncio.create_subprocess_exec() itself
            # (client._process not yet assigned - a race narrow enough
            # that it needs an artificial back-to-back connect_server()+
            # shutdown() with no event-loop turn between them to hit),
            # this task can be left "cancelling" forever, since Python's
            # own subprocess-transport creation doesn't unwind cleanly
            # under cancellation there - confirmed directly against
            # CPython's asyncio, not this codebase's own code. shutdown()
            # never hangs on it either way: its own bounded fallback
            # (see shutdown()'s docstring) forces the loop to stop and
            # the thread to exit regardless, at the cost of leaking that
            # one OS subprocess in this specific narrow race rather than
            # the whole shutdown.
            if client is not None:
                try:
                    await client.close()
                except Exception:  # noqa: BLE001 - shutdown proceeds regardless
                    pass
            raise
        except McpTimeoutError as exc:
            message = str(exc)
            await self._close_failed_client(client)
            self._post_to_gui_thread(lambda: self._on_connect_error(server_id, message))
            return
        except McpProtocolError as exc:
            message = str(exc)
            await self._close_failed_client(client)
            self._post_to_gui_thread(lambda: self._on_connect_error(server_id, message))
            return
        except FileNotFoundError as exc:
            # `as exc` is implicitly deleted at the end of this except block
            # (a Python gotcha), so the exception object itself must never be
            # captured by a closure that runs later - the message is copied
            # into a plain local first, here and in every branch below.
            message = f"command not found: {exc}"
            await self._close_failed_client(client)
            self._post_to_gui_thread(lambda: self._on_connect_error(server_id, message))
            return
        except Exception as exc:  # noqa: BLE001 - a connect attempt must never crash the loop
            message = f"{type(exc).__name__}: {exc}"
            await self._close_failed_client(client)
            self._post_to_gui_thread(lambda: self._on_connect_error(server_id, message))
            return
        self._post_to_gui_thread(lambda: self._on_connected(server_id, client, tools))

    @staticmethod
    async def _close_failed_client(client) -> None:
        """client.connect() itself already closes on its own timeout (see
        protocol.py), but every later step here - list_tools(), a
        malformed response - can fail too, after a real subprocess/HTTP
        connection is already open. None of those paths ever reach
        _on_connected, so connection.client is never set and nothing
        else will ever hold a reference to close it: confirmed directly
        as leaked subprocesses (asyncio's own per-subprocess reaper
        thread still blocked in _do_waitpid at interpreter shutdown,
        contributing to a native crash there) once enough connect
        attempts fail this way across a test run. Called from every
        failure branch below rather than a try/finally, so each branch
        keeps building its own specific error message untouched."""
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001 - the caller already has its own error to report
                pass

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
    def server_id_name_pairs(self) -> list[tuple[str, str]]:
        """Every configured server's (id, display name) - for UI that lets
        someone pick among servers by name without reaching into this
        manager's connection dict directly (see app/ui/workspace_switcher.
        py's per-workspace MCP visibility picker). See configured_servers()
        for the full McpServerConfig list this is a thin projection of."""
        return [(server_id, connection.config.name)
                for server_id, connection in self._connections.items()]

    def all_tools(self, server_id: str) -> list[McpToolDescriptor]:
        connection = self._connections.get(server_id)
        return list(connection.tools) if connection else []

    def find_tool(self, server_id: str, tool_name: str) -> McpToolDescriptor | None:
        for tool in self.all_tools(server_id):
            if tool.name == tool_name:
                return tool
        return None

    def knows(self, namespaced_name: str) -> bool:
        """Does this tool exist at all - not "is it currently permitted".

        Phase 1 conflated the two (a write tool did not exist as far as the
        agent loop was concerned); Phase 2 separates them, the same way a
        native browser tool "existing" and "being allowed to run right now
        without asking" are already two different questions answered by
        knows() and assess() respectively.

        Phase 17: also False for a server hidden from the current
        Workspace (see set_workspace_visibility) - defense in depth
        alongside schemas() already not offering it, so a stale/cached
        tool name from a different workspace's context cannot slip
        through just because knows() was asked directly.
        """
        parts = adapter.split_namespaced(namespaced_name)
        if parts is None:
            return False
        server_id, tool_name = parts
        if self._visible_server_ids is not None and server_id not in self._visible_server_ids:
            return False
        return self.find_tool(server_id, tool_name) is not None

    def set_workspace_visibility(self, visible_server_ids: frozenset[str] | None) -> None:
        """Phase 17: scope which connected servers this session's tool
        list/knows() will offer - None (the default) means every
        connected, enabled server, exactly today's behaviour. Never
        touches a server's connection, credentials, or config - purely a
        filter over what is already connected; see app/workspaces/."""
        self._visible_server_ids = visible_server_ids

    def schemas(self) -> list[dict[str, Any]]:
        """Every tool from every currently CONNECTED, enabled, and
        workspace-visible server (see set_workspace_visibility).

        All of them, regardless of classification: a write/sensitive/
        destructive/unknown tool is now offered to the model exactly the
        way a sensitive browser action already is - assess_call() below is
        what stands between the model proposing it and it actually running.
        """
        out: list[dict[str, Any]] = []
        for server_id, connection in self._connections.items():
            if connection.state != ConnectionState.CONNECTED:
                continue
            if self._visible_server_ids is not None and server_id not in self._visible_server_ids:
                continue
            for tool in connection.tools:
                out.append(adapter.to_tool_schema(tool))
        return out

    def current_fingerprint(self, namespaced_name: str) -> str:
        """The connected tool's CURRENT schema_fingerprint, or "" if it is
        unknown right now - used by the Phase 16 Automation Recorder/runner
        to detect an MCP tool's shape changing between recording and replay
        (see app/automation/runner.py), the same fingerprint permissions.py
        already uses to invalidate a remembered approval."""
        parts = adapter.split_namespaced(namespaced_name)
        if parts is None:
            return ""
        server_id, tool_name = parts
        tool = self.find_tool(server_id, tool_name)
        return tool.schema_fingerprint if tool is not None else ""

    def describe_call(self, namespaced_name: str, args: dict[str, Any]) -> str:
        parts = adapter.split_namespaced(namespaced_name)
        if parts is None:
            return namespaced_name
        server_id, tool_name = parts
        connection = self._connections.get(server_id)
        display = connection.config.name if connection else server_id
        return f"Using {display}: {tool_name}"

    # -- permissions -------------------------------------------------------
    def permission_for(self, server_id: str, tool_name: str, *,
                       mission_id: int | None = None) -> str:
        """Allow/Ask/Deny for this tool right now: a remembered decision
        that still matches the tool's current schema, or - failing that -
        safety.default_permission's classification-based fallback (which is
        never ALLOW for anything but READ_ONLY)."""
        tool = self.find_tool(server_id, tool_name)
        sensitivity = tool.sensitivity if tool is not None else Sensitivity.UNKNOWN
        fingerprint = tool.schema_fingerprint if tool is not None else ""
        remembered = self._permissions.decision_for(
            server_id, tool_name, fingerprint, mission_id=mission_id)
        return remembered if remembered is not None else default_permission(sensitivity)

    def remember_permission_for(self, namespaced_name: str, permission: str, scope: str,
                                *, mission_id: int | None = None) -> None:
        parts = adapter.split_namespaced(namespaced_name)
        if parts is None:
            return
        server_id, tool_name = parts
        tool = self.find_tool(server_id, tool_name)
        if (permission == Permission.ALLOW and scope == Scope.ALWAYS and tool is not None
                and tool.sensitivity == Sensitivity.DESTRUCTIVE):
            # Same rule as set_tool_permission, enforced here too: an
            # "Always allow" answered from a live approval prompt reaches
            # this method exactly the way Settings' dropdown does, so a
            # destructive tool refuses it from both entry points, not just
            # the one that happens to hide the option in its UI.
            return
        fingerprint = tool.schema_fingerprint if tool is not None else ""
        self._permissions.remember(server_id, tool_name, fingerprint, permission, scope,
                                   mission_id=mission_id)

    def set_tool_permission(self, server_id: str, tool_name: str, permission: str) -> bool:
        """The Settings UI's per-tool control - always Scope.ALWAYS, since
        there is no Mission context in Settings. ``permission`` may also be
        Permission.ASK, which *clears* any remembered Allow/Deny instead of
        storing "ask" as a value (ASK is never persisted - see
        McpPermissionStore).

        Returns False, refusing the change, for one case only: Allow on a
        DESTRUCTIVE tool. That is enforced here rather than only hidden
        from the dropdown, so nothing - a stale UI, a script, a future
        caller - can silently make a destructive tool auto-run by going
        around the widget. Every other combination, WRITE and SENSITIVE
        included, may still be set to Allow if the user chooses to.
        """
        tool = self.find_tool(server_id, tool_name)
        if (permission == Permission.ALLOW and tool is not None
                and tool.sensitivity == Sensitivity.DESTRUCTIVE):
            return False
        if permission == Permission.ASK:
            self._permissions.clear(server_id, tool_name)
        else:
            fingerprint = tool.schema_fingerprint if tool is not None else ""
            self._permissions.remember(server_id, tool_name, fingerprint, permission,
                                       Scope.ALWAYS)
        return True

    def clear_permission(self, server_id: str, tool_name: str) -> None:
        self._permissions.clear(server_id, tool_name)

    def permission_records(self, server_id: str):
        return self._permissions.all_for_server(server_id)

    def permission_scope_for(self, server_id: str, tool_name: str) -> str:
        """"always" / "mission" / "" (no remembered decision - the default
        applies fresh each time) for display in the global permissions view.
        Ignores *which* Mission a Scope.MISSION record belongs to - the
        summary view only needs to say a decision is Mission-scoped, not
        which one; McpConnectionManager.permission_for is what actually
        checks a specific Mission id when a call is made.
        """
        tool = self.find_tool(server_id, tool_name)
        if tool is not None and tool.never_confirmed:
            return ""
        fingerprint = tool.schema_fingerprint if tool is not None else ""
        for record in self._permissions.all_for_server(server_id):
            if record.tool_name == tool_name and record.schema_fingerprint == fingerprint:
                return record.scope
        return ""

    def reset_server_permissions(self, server_id: str) -> None:
        self._permissions.forget_server(server_id)

    def reset_all_permissions(self) -> None:
        self._permissions.clear_all()

    def editable_field(self, namespaced_name: str, args: dict[str, Any]) -> tuple[str, str]:
        """Which argument, if any, is worth letting the user hand-edit
        before approving - the same rule AgentSession._editable_field
        applies to a browser_type call's text: offered only when there is
        exactly one plain-string top-level argument, and it is not shaped
        like a secret (see safety.SENSITIVE_SHAPED_FIELD_NAMES). A tool
        whose schema doesn't reduce to that - zero, or more than one,
        candidate field - has no single obviously-correct field to expose,
        so nothing is made editable rather than guessing.
        """
        parts = adapter.split_namespaced(namespaced_name)
        if parts is None:
            return "", ""
        server_id, tool_name = parts
        tool = self.find_tool(server_id, tool_name)
        if tool is None:
            return "", ""
        schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
        props = schema.get("properties")
        if not isinstance(props, dict):
            return "", ""
        string_fields = [
            key for key, spec in props.items()
            if isinstance(spec, dict) and spec.get("type") == "string"
            and str(key).lower() not in SENSITIVE_SHAPED_FIELD_NAMES
        ]
        if len(string_fields) != 1:
            return "", ""
        field = string_fields[0]
        value = args.get(field)
        if not isinstance(value, str):
            return "", ""
        return field, value

    def assess_call(self, namespaced_name: str, args: dict[str, Any], *,
                    mission_id: int | None = None, mission_title: str = "") -> dict[str, Any]:
        """The MCP equivalent of BrowserController.describe_action: what
        would this call do, and does it need the user's blessing - decided
        here, from PyBrowser's own classification and stored permission,
        never from the model and never from the server's own description.

        Returns the exact same shape ToolRegistry.assess() already produces
        for a browser tool (level/reasons/requires_confirmation, optionally
        refused+refusal_code+refusal_message), plus one additive "mcp" key
        carrying what the approval prompt needs to show (server/tool/data/
        effect) - AgentSession reads that key only when present, so a
        browser confirmation is completely unaffected by its existence.
        """
        parts = adapter.split_namespaced(namespaced_name)
        if parts is None:
            return {"level": "elevated", "reasons": ["unrecognised MCP tool"],
                    "requires_confirmation": False}
        server_id, tool_name = parts
        tool = self.find_tool(server_id, tool_name)
        if tool is None:
            return {"level": "elevated", "reasons": ["unrecognised MCP tool"],
                    "requires_confirmation": False}
        if tool.never_confirmed:
            return {"level": "normal", "reasons": [], "requires_confirmation": False}

        connection = self._connections.get(server_id)
        display = connection.config.name if connection is not None else server_id
        sensitivity = tool.sensitivity
        mcp_info = {
            "server": display,
            "server_id": server_id,
            "tool": tool_name,
            "data": _redact_arguments(args),
            "effect": describe_effect(sensitivity, display),
            "sensitivity": sensitivity,
        }
        reasons = [reason_fragment(sensitivity)]
        permission = self.permission_for(server_id, tool_name, mission_id=mission_id)

        if permission == Permission.DENY:
            self._record_audit(server_id, tool_name, sensitivity=sensitivity,
                              mission_id=mission_id, mission_title=mission_title,
                              decision=DECISION_DENIED_REMEMBERED,
                              outcome=OUTCOME_NOT_EXECUTED)
            return {
                "level": sensitivity, "reasons": reasons, "requires_confirmation": False,
                "refused": True,
                "refusal_code": "MCP_PERMISSION_DENIED",
                "refusal_message": (
                    f"'{tool_name}' on {display} is set to never be allowed. "
                    "Ask the user to change this in Settings if this task needs it."),
                "refusal_reason": "mcp_permission_denied",
                "mcp": mcp_info,
            }
        if permission == Permission.ALLOW:
            return {"level": sensitivity, "reasons": reasons,
                    "requires_confirmation": False, "mcp": mcp_info}
        # Permission.ASK - either explicitly set, or (far more often) simply
        # never decided - either way, ask.
        return {"level": sensitivity, "reasons": reasons,
                "requires_confirmation": True, "mcp": mcp_info}

    # -- running -----------------------------------------------------
    def run_tool(self, namespaced_name: str, args: dict[str, Any], *,
                mission_id: int | None = None, mission_title: str = "") -> BrowserFuture:
        parts = adapter.split_namespaced(namespaced_name)
        if parts is None or self._loop is None or self._shutting_down:
            return resolved("mcp_call", adapter.render_tool_result(
                ok=False, error_code="UNKNOWN_TOOL",
                error_message=f"'{namespaced_name}' is not a known MCP tool."))
        server_id, tool_name = parts
        connection = self._connections.get(server_id)
        if connection is None or connection.state != ConnectionState.CONNECTED:
            self._record_audit(server_id, tool_name, sensitivity=Sensitivity.UNKNOWN,
                              mission_id=mission_id, mission_title=mission_title,
                              decision=self._current_decision(server_id, tool_name, mission_id),
                              outcome=OUTCOME_ERROR, error_code="SERVER_NOT_CONNECTED")
            return resolved("mcp_call", adapter.render_tool_result(
                ok=False, server_id=server_id, tool_name=tool_name,
                error_code="SERVER_NOT_CONNECTED",
                error_message=f"'{server_id}' is not connected."))
        tool = self.find_tool(server_id, tool_name)
        if tool is None:
            return resolved("mcp_call", adapter.render_tool_result(
                ok=False, server_id=server_id, tool_name=tool_name,
                error_code="UNKNOWN_TOOL", error_message="That tool was not discovered."))
        # No sensitivity check here: run_tool() executes once dispatched,
        # exactly like a native browser tool's own handler does - the
        # decision to run at all (assess_call(), above) already happened
        # one layer up, in ToolRegistry.assess()/AgentSession, before this
        # was ever called. A tool renamed between discovery and this call
        # (see the "server renames tool after reconnect" test) is still
        # safe: its *new* name would need to pass assess_call() again on
        # its own, fresh classification and fingerprint, to ever get here.
        decision = self._current_decision(server_id, tool_name, mission_id)
        sensitivity = tool.sensitivity
        start_time = time.monotonic()

        # Phase 15 egress firewall: the same protection wrap_untrusted gives
        # INBOUND MCP content applies OUTBOUND too - an argument the model
        # filled in from page/file content it read could itself carry an
        # API key or password that has no business leaving the browser for
        # a third-party server. High-risk secrets are redacted before the
        # call is ever sent, never merely logged after the fact.
        args = _redact_outbound_args(args, server_id=server_id, tool_name=tool_name)

        # Parented to this manager (a stable, always-GUI-thread QObject) -
        # not left unparented as BrowserFuture's own default. on_bg_done
        # below runs on this manager's background "mcp-io" thread, and
        # posts a lambda closing over `future` to the GUI thread via
        # _post_to_gui_thread; if the dispatcher is already shut down
        # (a real race: shutdown() during a pending tool call), that post
        # raises GuiDispatchShutdown, which _post_to_gui_thread catches
        # and discards - dropping the lambda's, and therefore `future`'s,
        # last Python reference right there on mcp-io via ordinary
        # refcounting. `future` owns a QTimer child (via set_timeout()
        # below), so an unparented `future` being reclaimed there means
        # that QTimer's C++ destructor - and its own killTimer() call -
        # also runs on mcp-io instead of the GUI thread. Confirmed via
        # gdb (breakpoint on QObject::killTimer, correlating the
        # destroying parent's C++ pointer against a shiboken6
        # .getCppPointer() registry) to be exactly this class, at exactly
        # this construction site. Parenting to the manager means the
        # underlying C++ object stays alive regardless of where its
        # Python wrapper's reference is dropped - Qt reclaims it
        # deterministically, on the GUI thread, when the manager itself
        # is.
        future = BrowserFuture(f"mcp:{namespaced_name}", parent=self)
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
                self._post_to_gui_thread(
                    lambda: self._mark_connection_error(server_id, message, tool_name=tool_name))
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
            duration_ms = (time.monotonic() - start_time) * 1000
            outcome = (OUTCOME_SUCCESS if payload.get("ok") else
                      OUTCOME_TIMEOUT if "TIMEOUT" in payload.get("text", "") else OUTCOME_ERROR)
            self._record_audit(server_id, tool_name, sensitivity=sensitivity,
                              mission_id=mission_id, mission_title=mission_title,
                              decision=decision, outcome=outcome, duration_ms=duration_ms)
            self._post_to_gui_thread(lambda: future.set_result(payload))

        cf.add_done_callback(on_bg_done)
        return future

    def _mark_connection_error(self, server_id: str, message: str, *,
                               tool_name: str = "") -> None:
        """A tool call revealed the connection is actually dead - reflect
        that in state immediately rather than waiting for the user to
        notice on a stale "Connected" row. Does not auto-reconnect: see the
        architecture doc's note that Phase 1 reconnection is user-triggered,
        not an unattended retry loop.

        ``tool_name`` is set only when this was discovered mid-call (as
        opposed to a fresh connect attempt failing, which has its own
        Settings-row feedback and never emits connection_dropped) - it is
        what tells the user "GitHub disconnected while Py was reading
        repository data" rather than just a status row quietly going red.
        """
        connection = self._connections.get(server_id)
        if connection is None:
            return
        connection.state = ConnectionState.ERROR
        connection.last_error = message
        connection.client = None
        self.server_changed.emit(server_id)
        if tool_name:
            server_name = connection.config.name
            self.connection_dropped.emit(server_id, server_name, tool_name)

    # -- audit ------------------------------------------------------------
    def _current_decision(self, server_id: str, tool_name: str,
                          mission_id: int | None) -> str:
        """Why this call is allowed to run, purely from current state - no
        extra plumbing needed from the caller. Correct because a remembered
        record (Scope.ALWAYS/MISSION) is always written *before* the call
        it applies to next runs: resolve_confirmation() persists the
        decision, then executes: see AgentSession.resolve_confirmation."""
        tool = self.find_tool(server_id, tool_name)
        if tool is not None and tool.never_confirmed:
            return DECISION_AUTO
        fingerprint = tool.schema_fingerprint if tool is not None else ""
        remembered = self._permissions.decision_for(server_id, tool_name, fingerprint,
                                                     mission_id=mission_id)
        return DECISION_ALLOWED_REMEMBERED if remembered == Permission.ALLOW else DECISION_ALLOWED_ONCE

    def _record_audit(self, server_id: str, tool_name: str, *, sensitivity: str,
                      mission_id: int | None, mission_title: str, decision: str,
                      outcome: str, duration_ms: float | None = None,
                      error_code: str = "") -> None:
        connection = self._connections.get(server_id)
        server_name = connection.config.name if connection is not None else server_id
        self._audit.record(
            server_id=server_id, server_name=server_name, tool_name=tool_name,
            sensitivity=sensitivity, mission_id=mission_id, mission_title=mission_title,
            decision=decision, outcome=outcome, duration_ms=duration_ms, error_code=error_code)

    def record_declined_call(self, namespaced_name: str, *,
                             mission_id: int | None = None, mission_title: str = "") -> None:
        """Called from AgentSession.resolve_confirmation when the user
        declines a live approval prompt - the one path a denial can happen
        through that assess_call() itself never sees, since a fresh "Ask"
        answered "no" is not a remembered decision."""
        parts = adapter.split_namespaced(namespaced_name)
        if parts is None:
            return
        server_id, tool_name = parts
        tool = self.find_tool(server_id, tool_name)
        sensitivity = tool.sensitivity if tool is not None else Sensitivity.UNKNOWN
        self._record_audit(server_id, tool_name, sensitivity=sensitivity,
                          mission_id=mission_id, mission_title=mission_title,
                          decision=DECISION_DENIED_ONCE, outcome=OUTCOME_NOT_EXECUTED)

    def audit_entries(self, **kwargs):
        return self._audit.entries(**kwargs)

    def clear_audit(self) -> None:
        self._audit.clear()

    def clear_audit_for_server(self, server_id: str) -> None:
        self._audit.clear_server(server_id)
