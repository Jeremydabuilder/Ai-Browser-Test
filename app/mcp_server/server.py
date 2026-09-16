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

import contextlib
import gc
import json
import os
import sys
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

# Same temporary diagnostic switch as app/ui/mcp_server_settings.py - see
# there for what it's chasing. Off by default.
_VERIFY_DIAG = os.environ.get("PYBROWSER_VERIFY_DIAG") == "1"


def _diag(msg: str) -> None:
    if _VERIFY_DIAG:
        t = threading.current_thread()
        print(f"VERIFYDIAG [{t.name}/{t.ident}] {msg}", file=sys.stderr, flush=True)


@contextlib.contextmanager
def _gc_paused():
    """Suspend the cyclic GC for a cross-thread queued-signal round trip.

    Defense in depth, not by itself sufficient: a reproduced crash (a
    same-process test that opens a real HTTP connection to this server
    while pumping the GUI event loop) needed gc disabled for the whole
    verify round trip, not just one call_sync - see the caller in
    app/ui/mcp_server_settings.py for that fix and its own account of
    what was actually proven. This narrower version still removes one
    real hazard on every call_sync/call_future/confirm round trip: Qt's
    queued delivery of a Signal(object) carrying a Python callable across
    threads has a window - between emit() on the calling thread and the
    slot actually running on the GUI thread - where the callable is live
    only via Qt's own internal (non-refcounted-by-Python) bookkeeping,
    and a cyclic collection landing in that window is a plausible crash
    even where it wasn't the one this specific bug needed. Refcounting
    alone (never suspended) still reclaims everything the moment this
    call returns; only the generational cycle collector is paused, and
    only for the few milliseconds one round trip takes.
    """
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


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
        _diag(f"GuiBridge.__init__ id={id(self)} thread={self.thread()}")

    def _run(self, callback: Callable[[], None]) -> None:
        _diag(f"GuiBridge._run entered id={id(self)} callback={callback!r}")
        callback()
        _diag(f"GuiBridge._run callback returned id={id(self)}")

    def _post(self, callback: Callable[[], None]) -> None:
        _diag(f"GuiBridge._post emitting id={id(self)} callback={callback!r}")
        self._invoke.emit(callback)
        _diag(f"GuiBridge._post emit() returned id={id(self)}")

    def call_sync(self, fn: Callable[[], Any], timeout: float = _CALL_TIMEOUT_S) -> Any:
        """Run ``fn`` on the GUI thread and return its value. ``fn`` must
        return a plain value immediately - use ``call_future`` for anything
        that returns a BrowserFuture."""
        box: dict[str, Any] = {}
        done = threading.Event()

        def on_gui_thread() -> None:
            _diag(f"GuiBridge.call_sync on_gui_thread running id={id(self)}")
            box["value"] = fn()
            done.set()
            _diag(f"GuiBridge.call_sync on_gui_thread done.set() id={id(self)}")

        with _gc_paused():
            _diag(f"GuiBridge.call_sync posting id={id(self)} fn={fn!r} timeout={timeout}")
            self._post(on_gui_thread)
            waited = done.wait(timeout)
            _diag(f"GuiBridge.call_sync done.wait() returned {waited} id={id(self)}")
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

        with _gc_paused():
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

        with _gc_paused():
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
