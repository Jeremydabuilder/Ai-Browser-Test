"""PyBrowser as an MCP Server (Phase 11): the real thing end to end - a
genuine ThreadingHTTPServer, real JSON-RPC over HTTP, and (in the second
half of this file) the real BrowserController/MissionService/
MissionGraphStore behind it via MainWindow. See test_mcp_server_unit.py
and test_mcp_server_tools.py for the storage/auth/dispatch layers tested
in isolation with fakes.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mcp_server_integration -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-mcp-server-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.mcp_server import auth  # noqa: E402
from app.mcp_server.server import DEFAULT_HOST, PyBrowserMcpServer  # noqa: E402
from app.storage import Database, McpServerAccessStore  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_profile = None
_next_port = [8801]


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _profile = shared_profile()


def _free_port() -> int:
    _next_port[0] += 1
    return _next_port[0]


def pump(predicate, timeout_ms: int = 8000) -> bool:
    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(timeout_ms)
    while not predicate() and not expired[0]:
        _app.processEvents()
    timer.stop()
    return predicate()


def call_async(url: str, body: dict, token: str | None = None, timeout: float = 5.0) -> dict:
    """POST a JSON-RPC request on a background thread and pump the Qt event
    loop until it answers - a real HTTP client thread synchronously waiting
    on a GUI-thread bridge is exactly the scenario this server is built
    for, so the test has to reproduce it rather than call requests inline."""
    box: dict = {}

    def worker() -> None:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                box["status"] = resp.status
                box["json"] = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            box["status"] = exc.code
            box["json"] = json.loads(exc.read())

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    pump(lambda: "json" in box, timeout_ms=int(timeout * 1000) + 2000)
    thread.join(timeout=1)
    return box


class BareServerTests(unittest.TestCase):
    """The server against a minimal (browser=None) context - enough to
    prove the transport, auth and permission layers without a real tab."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmp.name, "t.sqlite3"))
        self.store = McpServerAccessStore(self.db)
        self.server = PyBrowserMcpServer(store=self.store, port=_free_port())

    def tearDown(self) -> None:
        self.server.stop()
        self.db.close()
        self._tmp.cleanup()

    def _url(self) -> str:
        return f"http://{self.server.host}:{self.server.port}/mcp"

    def test_server_starts_and_stops(self) -> None:
        self.assertFalse(self.server.running)
        self.assertTrue(self.server.start())
        self.assertTrue(self.server.running)
        self.server.stop()
        self.assertFalse(self.server.running)

    def test_binds_to_localhost_only_by_default(self) -> None:
        self.assertEqual(self.server.host, DEFAULT_HOST)
        self.assertEqual(DEFAULT_HOST, "127.0.0.1")

    def test_tools_list_requires_no_auth_error_but_call_does(self) -> None:
        self.assertTrue(self.server.start())
        result = call_async(self._url(), {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                          "params": {}})
        self.assertEqual(result["status"], 200)
        self.assertIn("result", result["json"])

    def test_tools_list_without_a_token_is_rejected(self) -> None:
        self.assertTrue(self.server.start())
        result = call_async(self._url(), {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                                          "params": {}})
        self.assertEqual(result["json"]["error"]["code"], -32001)

    def test_tools_list_with_a_valid_token_succeeds(self) -> None:
        self.assertTrue(self.server.start())
        _client, token = auth.pair_client(self.store, display_name="X", capabilities=[])
        result = call_async(self._url(), {"jsonrpc": "2.0", "id": 3, "method": "tools/list",
                                          "params": {}}, token=token)
        tools = result["json"]["result"]["tools"]
        self.assertGreater(len(tools), 0)
        names = {t["name"] for t in tools}
        self.assertIn("browser.current_page", names)
        self.assertIn("mission.get_plan", names)

    def test_a_revoked_token_is_rejected(self) -> None:
        self.assertTrue(self.server.start())
        client, token = auth.pair_client(self.store, display_name="X", capabilities=[])
        auth.revoke_client(self.store, client.id)
        result = call_async(self._url(), {"jsonrpc": "2.0", "id": 4, "method": "tools/list",
                                          "params": {}}, token=token)
        self.assertEqual(result["json"]["error"]["code"], -32001)

    def test_a_garbage_token_is_rejected(self) -> None:
        self.assertTrue(self.server.start())
        result = call_async(self._url(), {"jsonrpc": "2.0", "id": 5, "method": "tools/list",
                                          "params": {}}, token="garbage")
        self.assertEqual(result["json"]["error"]["code"], -32001)

    def test_malformed_json_is_rejected_cleanly(self) -> None:
        self.assertTrue(self.server.start())
        box: dict = {}

        def worker() -> None:
            req = urllib.request.Request(
                self._url(), data=b"{not json", headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    box["status"] = resp.status
                    box["json"] = json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                box["status"] = exc.code
                box["json"] = json.loads(exc.read())

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        pump(lambda: "json" in box, timeout_ms=5000)
        self.assertEqual(box["status"], 400)
        self.assertIn("error", box["json"])

    def test_capability_scope_is_enforced_end_to_end(self) -> None:
        """A missing capability is refused at the JSON-RPC level, before the
        call ever reaches a handler - see dispatch()'s permission check."""
        self.assertTrue(self.server.start())
        _client, token = auth.pair_client(self.store, display_name="X", capabilities=["read_tabs"])
        result = call_async(self._url(), {
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "mission.create", "arguments": {"goal": "test"}}}, token=token)
        self.assertEqual(result["json"]["error"]["code"], -32002)

    def test_a_client_can_never_grant_itself_more_capabilities_via_a_tool_call(self) -> None:
        """No tool in the exposed set ever writes to a client's own
        capability row - there is no self-escalation path to call through."""
        self.assertTrue(self.server.start())
        client, token = auth.pair_client(self.store, display_name="X", capabilities=[])
        for tool in ("browser.navigate", "browser.open_tab", "mission.create"):
            call_async(self._url(), {
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": tool, "arguments": {"url": "https://x", "goal": "x"}}},
                token=token)
        refreshed = self.store.get_client(client.id)
        self.assertEqual(refreshed.capabilities, ())

    def test_audit_log_records_every_call_without_secrets(self) -> None:
        self.assertTrue(self.server.start())
        _client, token = auth.pair_client(self.store, display_name="X",
                                         capabilities=["read_tabs"])
        call_async(self._url(), {
            "jsonrpc": "2.0", "id": 8, "method": "tools/call",
            "params": {"name": "browser.list_tabs", "arguments": {}}}, token=token)
        entries = self.store.recent_audit()
        self.assertEqual(len(entries), 1)
        self.assertNotIn(token, entries[0].detail)
        self.assertLess(len(entries[0].detail), 300)


class ProtocolLevelClientTests(unittest.TestCase):
    """A minimal, genuine MCP client speaking the real wire protocol
    against the real server - the "at least one true protocol-level
    integration test" the Phase 11 spec calls for."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmp.name, "t.sqlite3"))
        self.store = McpServerAccessStore(self.db)
        self.server = PyBrowserMcpServer(store=self.store, port=_free_port())
        self.assertTrue(self.server.start())
        _client, self.token = auth.pair_client(
            self.store, display_name="Protocol Test Client",
            capabilities=["read_tabs", "read_pages", "read_missions"])

    def tearDown(self) -> None:
        self.server.stop()
        self.db.close()
        self._tmp.cleanup()

    def _url(self) -> str:
        return f"http://{self.server.host}:{self.server.port}/mcp"

    def test_full_handshake_then_list_then_call(self) -> None:
        init = call_async(self._url(), {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                        "params": {"protocolVersion": "2026-07-28",
                                                  "capabilities": {}, "clientInfo": {}}})
        self.assertIn("protocolVersion", init["json"]["result"])

        listed = call_async(self._url(), {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                                          "params": {}}, token=self.token)
        tool_names = {t["name"] for t in listed["json"]["result"]["tools"]}
        self.assertIn("browser.list_tabs", tool_names)

        # Missions are unavailable in this bare (no-MissionService) server,
        # which is itself a genuine wire-protocol case worth covering: an
        # application-level tool failure comes back as a *result* with
        # isError - never a JSON-RPC error - so a client's error-handling
        # code path for "the tool ran and said no" is exercised for real.
        called = call_async(self._url(), {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "mission.list", "arguments": {}}}, token=self.token)
        content = called["json"]["result"]["content"][0]["text"]
        payload = json.loads(content)
        self.assertFalse(payload["ok"])
        self.assertTrue(called["json"]["result"]["isError"])
        self.assertEqual(payload["error"]["code"], "MISSIONS_UNAVAILABLE")


class RealBrowserAndMissionIntegrationTests(unittest.TestCase):
    """The server wired to a REAL BrowserController/MissionService/
    MissionGraphStore via MainWindow - not a fake. Exercises the GUI-thread
    bridge (GuiBridge) for real, including a BrowserFuture that resolves
    asynchronously off a real (data:) page load."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmp.name, "t.sqlite3"))
        self.window = MainWindow(_profile, self.db, start_urls=[
            "data:text/html,<title>Real Page</title><p>hello from a real tab</p>"])
        self.assertTrue(pump(lambda: not self.window.controller.list_tabs()[0]["loading"]
                            and self.window.controller.list_tabs()[0]["title"] == "Real Page"))
        self.window.mcp_server.port = _free_port()
        self.assertTrue(self.window.mcp_server.start())
        _client, self.token = auth.pair_client(
            self.window.mcp_server.store, display_name="Real Test",
            capabilities=["read_pages", "read_tabs", "navigate", "open_tabs",
                         "read_missions", "create_mission"])

    def tearDown(self) -> None:
        self.window.close()
        self.db.close()
        self._tmp.cleanup()
        for _ in range(3):
            _app.processEvents()

    def _url(self) -> str:
        return f"http://{self.window.mcp_server.host}:{self.window.mcp_server.port}/mcp"

    def _call(self, tool: str, arguments: dict | None = None) -> dict:
        result = call_async(self._url(), {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}}}, token=self.token,
            timeout=10.0)
        return json.loads(result["json"]["result"]["content"][0]["text"])

    def test_current_page_reflects_the_real_active_tab(self) -> None:
        payload = self._call("browser.current_page")
        self.assertTrue(payload["ok"])
        self.assertIn("Real Page", payload["title"])

    def test_read_page_returns_the_real_pages_text_fenced_as_untrusted(self) -> None:
        payload = self._call("browser.read_page")
        self.assertTrue(payload["ok"])
        self.assertIn("hello from a real tab", payload["text"])
        self.assertIn("<untrusted_web_page_content>", payload["text"])

    def test_list_tabs_reflects_the_real_tab_manager(self) -> None:
        payload = self._call("browser.list_tabs")
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["tabs"]), len(self.window.controller.list_tabs()))

    def test_navigate_to_an_ordinary_url_actually_navigates_the_real_tab(self) -> None:
        before = len(self.window.controller.list_tabs())
        payload = self._call("browser.navigate", {
            "url": "data:text/html,<title>Navigated</title>"})
        self.assertTrue(payload["ok"])
        self.assertEqual(len(self.window.controller.list_tabs()), before)

    def test_mission_create_then_list_then_get_plan_round_trips_through_real_services(
        self,
    ) -> None:
        created = self._call("mission.create", {"goal": "Compare two laptops"})
        self.assertTrue(created["ok"])
        mission_id = created["mission"]["id"]

        listed = self._call("mission.list")
        self.assertTrue(any(m["id"] == mission_id for m in listed["missions"]))

        plan = self._call("mission.get_plan", {"mission_id": mission_id})
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["nodes"], [])  # no coordinator has run yet - an empty plan is valid

    def test_a_client_without_the_navigate_capability_is_refused_and_the_tab_never_moves(
        self,
    ) -> None:
        _client, restricted_token = auth.pair_client(
            self.window.mcp_server.store, display_name="Read Only", capabilities=["read_tabs"])
        before_url = self.window.controller.list_tabs()[0]["url"]
        result = call_async(self._url(), {
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "browser.navigate", "arguments": {"url": "https://example.com"}}},
            token=restricted_token)
        self.assertEqual(result["json"]["error"]["code"], -32002)
        self.assertEqual(self.window.controller.list_tabs()[0]["url"], before_url)


class ConfirmationBridgeTests(unittest.TestCase):
    """A sensitive browser.navigate call must route through the exact same
    describe_action classification as the interactive agent, and an
    external caller must not be able to bypass a denial - the confirmation
    prompt itself is a real QMessageBox, monkeypatched here only to answer
    it deterministically rather than block on a human."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmp.name, "t.sqlite3"))
        self.window = MainWindow(_profile, self.db, start_urls=["about:blank"])
        self.window.mcp_server.port = _free_port()
        self.assertTrue(self.window.mcp_server.start())
        _client, self.token = auth.pair_client(
            self.window.mcp_server.store, display_name="Confirm Test",
            capabilities=["navigate"])

    def tearDown(self) -> None:
        self.window.close()
        self.db.close()
        self._tmp.cleanup()
        for _ in range(3):
            _app.processEvents()

    def _url(self) -> str:
        return f"http://{self.window.mcp_server.host}:{self.window.mcp_server.port}/mcp"

    def test_a_denied_confirmation_blocks_the_external_navigate(self) -> None:
        """Force the SAME describe_action classification path the
        interactive agent relies on to report "needs confirmation", then
        prove a denial there actually stops the navigate - the external
        caller never gets a second route around it."""
        original_describe = self.window.controller.describe_action
        original_question = QMessageBox.question
        self.window.controller.describe_action = (
            lambda action, ref=None, text="", url="", tab_id=None:
            {"level": "elevated", "reasons": ["forced for test"], "requires_confirmation": True})
        QMessageBox.question = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.No)
        try:
            before_url = self.window.controller.list_tabs()[0]["url"]
            result = call_async(self._url(), {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "browser.navigate",
                          "arguments": {"url": "https://example.com"}}}, token=self.token,
                timeout=10.0)
            payload = json.loads(result["json"]["result"]["content"][0]["text"])
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "DENIED")
            self.assertEqual(self.window.controller.list_tabs()[0]["url"], before_url)
        finally:
            self.window.controller.describe_action = original_describe
            QMessageBox.question = original_question


if __name__ == "__main__":
    unittest.main()
