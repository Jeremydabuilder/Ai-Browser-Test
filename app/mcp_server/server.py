"""The PyBrowser MCP server: a Streamable HTTP listener bound to
127.0.0.1 only, exposing the tools in app/mcp_server/tools.py to paired,
authenticated external clients.

Runs on a plain Python ``threading.Thread`` (not a QThread - it needs no
Qt event loop of its own), so every incoming call happens on a background
HTTP-handler thread. BrowserController must only ever be touched from the
GUI thread, so ``GuiBridge`` below replicates the exact
``_bg_result``/``_post_to_gui_thread`` cross-thread pattern already used by
app/mcp/connection_manager.py, extended with a ``threading.Event`` so a
handler thread can synchronously await a GUI-thread result (including one
that itself resolves via a BrowserFuture) before writing its HTTP response.

Only Streamable HTTP is implemented (stdio server mode is out of scope -
see the Phase 11 report's known limitations: PyBrowser is a persistent GUI
app, not a process meant to be spawned per client on its own stdio).
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox

from app.mcp_server import auth, transport
from app.mcp_server.audit import record_call
from app.mcp_server.tools import TOOL_SCHEMAS, McpToolContext, McpToolError, dispatch

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_CONFIRM_TIMEOUT_S = 120
_CALL_TIMEOUT_S = 30


class GuiBridge(QObject):
    """Runs a zero-arg callable on the GUI thread; the caller (any other
    thread) blocks on a ``threading.Event`` until it is done.

    Must be constructed on the GUI thread - its Qt signal/slot connection
    is what gives ``_post`` its automatic queued cross-thread delivery.
    """

    _invoke = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._invoke.connect(self._run)

    def _run(self, callback: Callable[[], None]) -> None:
        callback()

    def _post(self, callback: Callable[[], None]) -> None:
        self._invoke.emit(callback)

    def call_sync(self, fn: Callable[[], Any], timeout: float = _CALL_TIMEOUT_S) -> Any:
        """Run ``fn`` on the GUI thread and return its value. ``fn`` must
        return a plain value immediately - use ``call_future`` for anything
        that returns a BrowserFuture."""
        box: dict[str, Any] = {}
        done = threading.Event()

        def on_gui_thread() -> None:
            box["value"] = fn()
            done.set()

        self._post(on_gui_thread)
        done.wait(timeout)
        return box.get("value")

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

        self._post(on_gui_thread)
        done.wait(timeout)
        return box.get("value")

    def confirm(self, prompt: str, title: str = "External AI client") -> bool:
        """A blocking Yes/No prompt on the GUI thread - the same
        QMessageBox-based approach Phase 9's worker confirmations use,
        rather than forcing an external call through the full
        AgentSession/AgentPanel confirmation machinery, which is built
        around an active conversational turn that does not exist here."""
        box: dict[str, Any] = {}
        done = threading.Event()

        def on_gui_thread() -> None:
            choice = QMessageBox.question(
                None, title, prompt,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            box["value"] = choice == QMessageBox.StandardButton.Yes
            done.set()

        self._post(on_gui_thread)
        done.wait(_CONFIRM_TIMEOUT_S)
        return bool(box.get("value", False))


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
        self._thread.start()
        self.status_changed.emit(True)
        return True

    def stop(self) -> None:
        if not self.running:
            return
        assert self._httpd is not None
        self._httpd.shutdown()
        self._httpd.server_close()
        self._httpd = None
        self._thread = None
        self.status_changed.emit(False)
