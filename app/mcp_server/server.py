"""The PyBrowser MCP server: a Streamable HTTP listener bound to
127.0.0.1 only, exposing the tools in app/mcp_server/tools.py to paired,
authenticated external clients.

Runs on a plain Python ``threading.Thread`` (not a QThread - it needs no
Qt event loop of its own), so every incoming call happens on a background
HTTP-handler thread. BrowserController must only ever be touched from the
GUI thread, so ``GuiBridge`` below hands a plain Python callable to
``GuiDispatcher``, which queues it on a thread-safe ``queue.Queue`` and
runs it on the GUI thread via a GUI-owned ``QTimer`` - no Qt signal or
QObject ever crosses the worker/GUI thread boundary.

This replaces an earlier design where ``GuiBridge`` emitted a Qt
``Signal(object)`` carrying the callable itself across threads. That
design produced confirmed, reproducible native crashes on both platforms,
always on the GUI thread while Qt's own queued-event delivery ran:
Windows - ``Qt6Core!QCoreApplication::notifyInternal2``, access violation
(0xC0000005) reading address ``0xFFFFFFFFFFFFFFFF``; macOS -
``QtCore!QCoreApplication::sendEvent``, ``EXC_BAD_ACCESS`` at address
``0x70``. Both reproduced from an HTTP worker thread's ``call_sync``
racing the GUI thread's own event loop. See the diagnostic workflows
under .github/workflows/*-mcp-sequence-diag.yml for the native
backtraces that pinned this down.

Only Streamable HTTP is implemented (stdio server mode is out of scope -
see the Phase 11 report's known limitations: PyBrowser is a persistent GUI
app, not a process meant to be spawned per client on its own stdio).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import QMessageBox

from app.gui_dispatch import GuiDispatchShutdown, GuiDispatcher
from app.mcp_server import auth, transport
from app.mcp_server.audit import record_call
from app.mcp_server.tools import TOOL_SCHEMAS, McpToolContext, McpToolError, dispatch

#: Kept as an alias for backwards compatibility with any code/tests that
#: imported the old, module-local name.
GuiBridgeShutdown = GuiDispatchShutdown

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_CONFIRM_TIMEOUT_S = 120
_CALL_TIMEOUT_S = 30

# Same temporary diagnostic switch as app/ui/mcp_server_settings.py - see
# there for what it's chasing. Off by default.
_VERIFY_DIAG = os.environ.get("PYBROWSER_VERIFY_DIAG") == "1"


def _diag(msg: str) -> None:
    if _VERIFY_DIAG:
        t = threading.current_thread()
        print(f"VERIFYDIAG [{t.name}/{t.ident}] {msg}", file=sys.stderr, flush=True)


class GuiBridge:
    """Runs a callable on the GUI thread on behalf of any other thread,
    backed by ``GuiDispatcher`` (see its docstring, and the module
    docstring above, for why this replaced an earlier direct-Qt-signal
    design). Must be constructed on the GUI thread, since it creates its
    ``GuiDispatcher`` there.
    """

    def __init__(self) -> None:
        self._dispatcher = GuiDispatcher()
        _diag(f"GuiBridge.__init__ id={id(self)} thread={QThread.currentThread()}")

    def call_sync(self, fn: Callable[[], Any], timeout: float = _CALL_TIMEOUT_S) -> Any:
        """Run ``fn`` on the GUI thread and return its value. ``fn`` must
        return a plain value immediately - use ``call_future`` for anything
        that returns a BrowserFuture."""
        _diag(f"GuiBridge.call_sync posting id={id(self)} fn={fn!r} timeout={timeout}")
        result = self._dispatcher.run_sync(fn, timeout)
        _diag(f"GuiBridge.call_sync returning id={id(self)}")
        return result

    def call_future(self, fn: Callable[[], Any], timeout: float = _CALL_TIMEOUT_S) -> Any:
        """Run ``fn`` on the GUI thread, where it must return a
        BrowserFuture; wait for that future to resolve via its own
        ``then()`` callback - never ``BrowserFuture.wait()``, which spins a
        nested event loop and is documented as unsafe from a GUI slot."""
        box: dict[str, Any] = {}
        done = threading.Event()

        def on_gui_thread() -> None:
            future = fn()

            def on_resolved(result: Any) -> None:
                box["value"] = result
                done.set()

            future.then(on_resolved)

        self._dispatcher.post(on_gui_thread)
        done.wait(timeout)
        return box.get("value")

    def confirm(self, prompt: str, title: str = "External AI client") -> bool:
        """A blocking Yes/No prompt on the GUI thread - the same
        QMessageBox-based approach Phase 9's worker confirmations use,
        rather than forcing an external call through the full
        AgentSession/AgentPanel confirmation machinery, which is built
        around an active conversational turn that does not exist here."""
        def on_gui_thread() -> bool:
            choice = QMessageBox.question(
                None, title, prompt,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            return choice == QMessageBox.StandardButton.Yes

        result = self._dispatcher.run_sync(on_gui_thread, _CONFIRM_TIMEOUT_S)
        return bool(result)

    def shutdown(self) -> None:
        """Permanently tear down the underlying dispatcher. Call once, at
        application close - see ``PyBrowserMcpServer.shutdown()``."""
        self._dispatcher.shutdown()


class _Handler(BaseHTTPRequestHandler):
    server: "_HttpServer"  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # never let stdlib logging leak request bodies/tokens to stderr

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.rstrip("/") != "/mcp":
            self._send_json(404, {"error": "not found"})
            return
        app = self.server.app
        started = time.monotonic()
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""

        try:
            message = transport.parse_request(raw)
        except transport.TransportError as exc:
            self._send_json(400, transport.build_error(None, exc.code, exc.message))
            return

        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}

        if method == "notifications/initialized":
            self._send_status(202)
            return

        if method == "initialize":
            self._send_json(200, transport.build_result(request_id, transport.initialize_result()))
            return

        client = self._authenticate(app)
        if client is None:
            self._send_json(200, transport.build_error(
                request_id, transport.UNAUTHORIZED, "Invalid or revoked token."))
            return
        app.store.touch_last_used(client.id)

        if method == "tools/list":
            self._send_json(200, transport.build_result(request_id, {"tools": TOOL_SCHEMAS}))
            return

        if method == "tools/call":
            self._handle_tool_call(app, client, request_id, params, started)
            return

        self._send_json(200, transport.build_error(
            request_id, transport.METHOD_NOT_FOUND, f"Unknown method '{method}'."))

    def _handle_tool_call(self, app: "PyBrowserMcpServer", client, request_id: Any,
                          params: dict[str, Any], started: float) -> None:
        tool = params.get("name")
        arguments = params.get("arguments") or {}
        duration_ms = 0
        try:
            result = dispatch(app.context, client, tool, arguments)
            duration_ms = int((time.monotonic() - started) * 1000)
            outcome = "ok" if result.ok else "denied"
            record_call(app.store, client_id=client.id, tool=str(tool), outcome=outcome,
                       duration_ms=duration_ms,
                       approval_result=("approved" if result.ok else None),
                       detail=result.error_code)
            payload = result.to_dict()
            self._send_json(200, transport.build_result(request_id, {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": not result.ok,
            }))
        except McpToolError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            outcome = "forbidden" if exc.code == "INSUFFICIENT_PERMISSION" else "error"
            record_call(app.store, client_id=client.id, tool=str(tool), outcome=outcome,
                       duration_ms=duration_ms, detail=exc.code)
            code = (transport.FORBIDDEN if exc.code == "INSUFFICIENT_PERMISSION"
                   else transport.INVALID_PARAMS)
            self._send_json(200, transport.build_error(request_id, code, exc.message))
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to an external caller
            duration_ms = int((time.monotonic() - started) * 1000)
            record_call(app.store, client_id=client.id, tool=str(tool), outcome="error",
                       duration_ms=duration_ms, detail="internal_error")
            self._send_json(200, transport.build_error(
                request_id, transport.INTERNAL_ERROR, "Internal error."))

    def _authenticate(self, app: "PyBrowserMcpServer"):
        header = self.headers.get("Authorization", "")
        token = header[len("Bearer "):] if header.startswith("Bearer ") else ""
        return auth.verify_token(app.store, token)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_status(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _HttpServer(ThreadingHTTPServer):
    # Deliberately daemon: a per-request thread can be blocked inside
    # GuiBridge.call_sync/call_future waiting on the GUI thread (see
    # GuiBridge above). If stop() ever ran ON the GUI thread while such a
    # request were in flight, joining that thread (Python's own
    # ThreadingMixIn.server_close() would do this automatically for a
    # non-daemon thread) would deadlock - the request thread needs the
    # GUI thread's event loop to deliver the queued signal that would let
    # it finish, and the GUI thread would be stuck in the join waiting for
    # exactly that. Daemon threads are never joined, so stop() always
    # returns promptly instead.
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: "PyBrowserMcpServer") -> None:
        super().__init__(address, _Handler)
        self.app = app


class PyBrowserMcpServer(QObject):
    """Owned by MainWindow. Negligible cost while disabled: nothing is
    constructed or listening until ``start()`` is called."""

    status_changed = Signal(bool)  # True = running

    def __init__(
        self, *, store, browser=None, missions=None, graph_store=None,
        host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
    ) -> None:
        super().__init__()
        self.store = store
        self.host = host
        self.port = port
        self._httpd: _HttpServer | None = None
        self._thread: threading.Thread | None = None
        self._bridge = GuiBridge()
        self.context = McpToolContext(
            browser=browser, missions=missions, graph_store=graph_store,
            call_sync=self._bridge.call_sync, call_future=self._bridge.call_future,
            confirm=self._bridge.confirm)

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> bool:
        if self.running:
            return True
        try:
            self._httpd = _HttpServer((self.host, self.port), self)
        except OSError:
            self._httpd = None
            self.status_changed.emit(False)
            return False
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="mcp-server", daemon=True)
        _diag(f"PyBrowserMcpServer.start starting accept thread id={id(self)} "
              f"port={self.port}")
        self._thread.start()
        self.status_changed.emit(True)
        return True

    def stop(self) -> None:
        if not self.running:
            return
        _diag(f"PyBrowserMcpServer.stop entered id={id(self)}")
        assert self._httpd is not None
        self._httpd.shutdown()
        _diag(f"PyBrowserMcpServer.stop httpd.shutdown() returned id={id(self)}")
        self._httpd.server_close()
        _diag(f"PyBrowserMcpServer.stop httpd.server_close() returned id={id(self)}")
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            _diag(f"PyBrowserMcpServer.stop accept thread joined, "
                  f"alive={self._thread.is_alive()} id={id(self)}")
        self._httpd = None
        self._thread = None
        self.status_changed.emit(False)
        _diag(f"PyBrowserMcpServer.stop returning id={id(self)}")

    def shutdown(self) -> None:
        """Permanent, one-way teardown: stop the HTTP listener (like
        ``stop()``) AND permanently close the GUI dispatcher backing
        ``call_sync``/``call_future``/``confirm``, releasing any pending
        request with ``GuiBridgeShutdown``. Call this once, from the GUI
        thread, at application close (see MainWindow.closeEvent) - never
        from the Settings toggle, which uses plain ``stop()``/``start()``
        and needs the bridge to keep working across restarts."""
        self.stop()
        _diag(f"PyBrowserMcpServer.shutdown closing bridge id={id(self)}")
        self._bridge.shutdown()
