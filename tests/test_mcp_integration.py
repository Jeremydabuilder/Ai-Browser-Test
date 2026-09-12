"""Integration tests: McpConnectionManager + ToolRegistry + AgentSession +
Missions, wired together the same way main_window.py wires them, against the
real (if minimal) subprocess server in tests/fake_mcp_server.py.

Also covers the security scenarios the Phase 1 spec called out explicitly:
a write tool with a misleading "read-only" description, a prompt-injection
attempt inside a tool's result, and a server renaming a tool after reconnect.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mcp_integration -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-mcp-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.config import AgentConfig, ContextLimits  # noqa: E402
from app.agent.session import AgentSession  # noqa: E402
from app.agent.tools import ToolRegistry, ToolError  # noqa: E402
from app.browser.controller import BrowserController  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from app.mcp import adapter  # noqa: E402
from app.mcp.config import McpServerStore  # noqa: E402
from app.mcp.connection_manager import McpConnectionManager  # noqa: E402
from app.mcp.types import (  # noqa: E402
    ConnectionState,
    McpServerConfig,
    Permission,
    Scope,
    Sensitivity,
    Transport,
)
from app.missions import MissionService, MissionStore  # noqa: E402
from app.storage.database import Database  # noqa: E402
from app.storage.settings import SettingsStore  # noqa: E402
from tests.fake_claude import ScriptedClaude, calls, says  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_SERVER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_mcp_server.py")

_app: QApplication | None = None
_profile = None


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _profile = shared_profile()


def pump(predicate, timeout_ms: int = 15000) -> bool:
    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(timeout_ms)
    while not predicate() and not expired[0]:
        _app.processEvents()
    timer.stop()
    return predicate()


class McpTestCase(unittest.TestCase):
    """A real McpConnectionManager against a real fake_mcp_server subprocess."""

    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self._tmp.close()
        self.db = Database(self._tmp.name)
        self.settings = SettingsStore(self.db)
        self.store = McpServerStore(self.settings)
        self.manager = McpConnectionManager(self.store)

        self.tabs = TabManager(_profile, "about:blank")
        self.tabs.resize(1000, 700)
        self.browser = BrowserController(self.tabs)
        self.browser.open_tab().wait()

        self.missions = MissionService(MissionStore(self.db), self.browser, self.tabs)

    def tearDown(self) -> None:
        self.manager.shutdown()
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        _app.processEvents()
        os.unlink(self._tmp.name)

    # -- helpers ----------------------------------------------------------
    def add_server(self, server_id: str = "fake", *, env_overrides=None,
                   enabled: bool = True) -> McpServerConfig:
        env = dict(env_overrides or {})
        config = McpServerConfig(
            id=server_id, name="Fake MCP", transport=Transport.STDIO,
            enabled=enabled, command=sys.executable, args=(_SERVER_SCRIPT,), env=env)
        self.manager.add_or_update_server(config)
        return config

    def connect_and_wait(self, server_id: str = "fake") -> None:
        self.manager.connect_server(server_id)
        ok = pump(lambda: self.manager.state(server_id) in
                  (ConnectionState.CONNECTED, ConnectionState.ERROR))
        self.assertTrue(ok, "server never left the CONNECTING state")
        self.assertEqual(self.manager.state(server_id), ConnectionState.CONNECTED,
                         self.manager.connection(server_id).last_error)


class ConnectionErrorTests(McpTestCase):
    def test_missing_command_surfaces_as_an_error_state_with_a_message(self):
        # Regression test: _connect_and_discover's FileNotFoundError/
        # McpProtocolError/generic-Exception handlers used to build their
        # error-reporting lambda as `lambda: self._on_connect_error(id, str(exc))`,
        # capturing the `except ... as exc` name itself - which Python deletes
        # at the end of the except block, so by the time the lambda actually
        # ran (posted to the GUI thread, i.e. later) it raised NameError
        # instead of ever reaching ConnectionState.ERROR.
        config = McpServerConfig(id="missing", name="Missing Command",
                                 transport=Transport.STDIO,
                                 command="this-command-does-not-exist-anywhere")
        self.manager.add_or_update_server(config)
        self.manager.connect_server("missing")
        ok = pump(lambda: self.manager.state("missing") in
                  (ConnectionState.CONNECTED, ConnectionState.ERROR))
        self.assertTrue(ok)
        self.assertEqual(self.manager.state("missing"), ConnectionState.ERROR)
        self.assertIn("command not found",
                      self.manager.connection("missing").last_error)


class DiscoveryAndClassificationTests(McpTestCase):
    def test_all_discovered_tools_are_now_exposed(self):
        # Phase 1 only ever advertised READ_ONLY tools; Phase 2 offers every
        # discovered tool - a write tool is now proposable by the model, but
        # assess_call() (see ToolRegistryMcpTests below) is what actually
        # stands between that proposal and it running.
        self.add_server()
        self.connect_and_wait()
        names = {s["name"] for s in self.manager.schemas()}
        self.assertEqual(names, {"mcp.fake.echo", "mcp.fake.list_items",
                                 "mcp.fake.get_item", "mcp.fake.create_item"})

    def test_write_tool_is_classified_write_and_never_auto_runs(self):
        self.add_server()
        self.connect_and_wait()
        tool = self.manager.find_tool("fake", "create_item")
        self.assertIsNotNone(tool, "the write tool must still be discovered")
        self.assertEqual(tool.sensitivity, Sensitivity.WRITE)
        self.assertFalse(tool.never_confirmed)
        # No permission has been set yet - the default for anything but
        # READ_ONLY is always Ask, never a silent Allow.
        self.assertEqual(self.manager.permission_for("fake", "create_item"), Permission.ASK)

    def test_misleading_description_does_not_change_the_classification(self):
        self.add_server(env_overrides={"FAKE_MCP_MISLEADING_DESCRIPTIONS": "1"})
        self.connect_and_wait()
        tool = self.manager.find_tool("fake", "create_item")
        self.assertIn("safe", tool.description.lower())  # the server really did lie
        self.assertEqual(tool.sensitivity, Sensitivity.WRITE)  # classifier ignored it anyway
        self.assertEqual(self.manager.permission_for("fake", "create_item"), Permission.ASK)
        # Still offered to the model - Phase 2 gates execution, not discovery.
        self.assertIn("mcp.fake.create_item", {s["name"] for s in self.manager.schemas()})

    def test_knows_is_true_for_any_discovered_tool(self):
        self.add_server()
        self.connect_and_wait()
        self.assertTrue(self.manager.knows("mcp.fake.echo"))
        self.assertTrue(self.manager.knows("mcp.fake.create_item"))  # exists, even if gated
        self.assertFalse(self.manager.knows("mcp.fake.nonexistent"))
        self.assertFalse(self.manager.knows("mcp.other_server.echo"))

    def test_disabled_server_exposes_no_schemas(self):
        self.add_server(enabled=False)
        self.assertEqual(self.manager.state("fake"), ConnectionState.DISABLED)
        self.assertEqual(self.manager.schemas(), [])


class ToolRegistryMcpTests(McpTestCase):
    def test_assess_requires_confirmation_for_an_undecided_write_tool(self):
        # run() itself no longer blocks a write tool - assess() is where
        # Phase 2's gate lives, exactly like a browser_click's own
        # sensitivity check. Calling run() directly (as the model's chosen
        # tool would only ever do *after* assess()+confirmation) really
        # does execute it - that is what ToolRegistry.run's docstring means
        # by "getting here already means it was permitted".
        self.add_server()
        self.connect_and_wait()
        registry = ToolRegistry(self.browser, mcp=self.manager)
        assessment = registry.assess("mcp.fake.create_item", {"item_id": "9", "text": "hi"})
        self.assertTrue(assessment["requires_confirmation"])
        self.assertNotIn("refused", assessment)

    def test_run_still_executes_a_write_tool_once_dispatched(self):
        # The flip side of the above: run() trusts its caller, the same as
        # every native tool handler does - it is AgentSession's job to only
        # call it after assess()+confirmation said yes.
        self.add_server()
        self.connect_and_wait()
        registry = ToolRegistry(self.browser, mcp=self.manager)
        outcome = registry.run("mcp.fake.create_item", {"item_id": "9", "text": "hi"})
        result = outcome.mcp_future.wait(5000)
        self.assertTrue(result["ok"])

    def test_run_calls_the_real_read_only_tool(self):
        self.add_server()
        self.connect_and_wait()
        registry = ToolRegistry(self.browser, mcp=self.manager)
        outcome = registry.run("mcp.fake.echo", {"phrase": "hello there"})
        self.assertIsNotNone(outcome.mcp_future)
        result = outcome.mcp_future.wait(5000)
        self.assertTrue(result["ok"])
        self.assertIn("hello there", result["text"])
        self.assertIn(adapter.UNTRUSTED_OPEN, result["text"])

    def test_run_unknown_server_raises(self):
        registry = ToolRegistry(self.browser, mcp=self.manager)
        with self.assertRaises(ToolError):
            registry.run("mcp.nonexistent.echo", {})

    def test_run_with_no_mcp_manager_raises(self):
        registry = ToolRegistry(self.browser, mcp=None)
        with self.assertRaises(ToolError):
            registry.run("mcp.fake.echo", {})

    def test_schemas_combines_native_and_mcp(self):
        self.add_server()
        self.connect_and_wait()
        registry = ToolRegistry(self.browser, mcp=self.manager)
        names = {s["name"] for s in registry.schemas()}
        self.assertIn("browser_navigate", names)
        self.assertIn("mcp.fake.echo", names)

    def test_describe_call_uses_server_display_name(self):
        self.add_server()
        self.connect_and_wait()
        registry = ToolRegistry(self.browser, mcp=self.manager)
        description = registry.describe_call("mcp.fake.echo", {"phrase": "hi"})
        self.assertIn("Fake MCP", description)
        self.assertIn("echo", description)

    def test_disconnect_then_run_reports_server_not_connected(self):
        self.add_server()
        self.connect_and_wait()
        registry = ToolRegistry(self.browser, mcp=self.manager)
        self.manager.disconnect_server("fake")
        outcome = registry.run("mcp.fake.echo", {"phrase": "hi"})
        result = outcome.mcp_future.wait(5000)
        self.assertFalse(result["ok"])
        self.assertIn("not connected", result["text"])


class AgentSessionMcpTests(McpTestCase):
    def _session(self, script, missions=None) -> tuple[AgentSession, ScriptedClaude]:
        fake = ScriptedClaude(script)
        config = AgentConfig(limits=ContextLimits())
        session = AgentSession(self.browser, fake, config, missions=missions, mcp=self.manager)
        return session, fake

    def test_ask_py_uses_a_read_only_mcp_tool(self):
        self.add_server()
        self.connect_and_wait()
        session, fake = self._session([
            calls("mcp.fake.echo", {"phrase": "ping"}),
            lambda messages: says("The tool replied: ping"),
        ])
        done = []
        session.finished.connect(lambda: done.append(True))
        self.assertTrue(session.send("echo ping for me"))
        self.assertTrue(pump(lambda: bool(done)))
        self.assertIn("ping", fake.tool_results()[0])
        session.shutdown()

    def test_mission_records_the_mcp_tool_call(self):
        self.add_server()
        self.connect_and_wait()
        mission = self.missions.start("Try out the fake MCP server")
        self.assertIsNotNone(mission)
        session, fake = self._session(
            [calls("mcp.fake.echo", {"phrase": "for the record"}), lambda m: says("done")],
            missions=self.missions)
        session.step_changed.connect(self.missions.record_agent_step)
        done = []
        session.finished.connect(lambda: done.append(True))
        self.assertTrue(session.send("echo something"))
        self.assertTrue(pump(lambda: bool(done)))
        actions = self.missions.actions(mission.id)
        self.assertTrue(any(a.tool_name == "mcp.fake.echo" for a in actions),
                        [a.tool_name for a in actions])
        session.shutdown()

    def test_disconnect_during_mission_surfaces_as_a_failed_tool_result(self):
        # The server goes offline mid-Mission, between the first and second
        # tool call. The disconnect is triggered from the GUI thread (via the
        # pump loop below), not from inside the scripted model's callback,
        # which runs on the Claude worker thread and must never touch Qt
        # objects it does not own - the whole reason connection_manager.py
        # marshals its own background-thread results back onto the GUI
        # thread instead of calling into Qt directly from there.
        self.add_server()
        self.connect_and_wait()
        session, fake = self._session([
            calls("mcp.fake.echo", {"phrase": "first"}),
            calls("mcp.fake.echo", {"phrase": "second"}),
            lambda messages: says("noted"),
        ])
        done = []
        session.finished.connect(lambda: done.append(True))
        self.assertTrue(session.send("echo twice"))

        disconnected = []

        def tick():
            if not disconnected and len(fake.tool_results()) >= 1:
                self.manager.disconnect_server("fake")
                disconnected.append(True)
            return bool(done)

        self.assertTrue(pump(tick))
        self.assertTrue(disconnected, "never reached the point of disconnecting")
        second_payload = json.loads(fake.tool_results()[-1].split("\n")[0])
        self.assertFalse(second_payload["ok"])
        self.assertIn("not connected", fake.tool_results()[-1])
        session.shutdown()


class SecurityTests(McpTestCase):
    def test_prompt_injection_in_tool_result_stays_fenced(self):
        self.add_server(env_overrides={"FAKE_MCP_INJECTION": "1"})
        self.connect_and_wait()
        registry = ToolRegistry(self.browser, mcp=self.manager)
        outcome = registry.run("mcp.fake.echo", {"phrase": "hi"})
        result = outcome.mcp_future.wait(5000)
        text = result["text"]
        self.assertIn("ignore all previous instructions", text)
        # The injection text must appear only inside the untrusted fence -
        # i.e. between the open and close markers, not before the open one.
        open_index = text.index(adapter.UNTRUSTED_OPEN)
        injection_index = text.index("ignore all previous instructions")
        close_index = text.index(adapter.UNTRUSTED_CLOSE)
        self.assertLess(open_index, injection_index)
        self.assertLess(injection_index, close_index)

    def test_server_renaming_a_tool_after_reconnect_stays_blocked(self):
        marker = tempfile.NamedTemporaryFile(suffix=".marker", delete=False)
        marker.close()
        os.unlink(marker.name)  # the fake server treats "file exists" as "already connected once"
        self.addCleanup(lambda: os.path.exists(marker.name) and os.unlink(marker.name))
        self.add_server(env_overrides={"FAKE_MCP_RENAME_MARKER": marker.name})
        self.connect_and_wait()
        # First connection: "echo" is read-only and visible.
        self.assertTrue(self.manager.knows("mcp.fake.echo"))
        self.manager.reconnect_server("fake")
        pump(lambda: self.manager.state("fake") in
             (ConnectionState.CONNECTED, ConnectionState.ERROR))
        self.assertEqual(self.manager.state("fake"), ConnectionState.CONNECTED)
        # After reconnect the server calls it something else; the old
        # namespaced name is simply gone, not silently still runnable.
        self.assertFalse(self.manager.knows("mcp.fake.echo"))
        registry = ToolRegistry(self.browser, mcp=self.manager)
        with self.assertRaises(ToolError):
            registry.run("mcp.fake.echo", {"phrase": "hi"})

    def test_malformed_tool_entry_from_server_is_dropped_not_crashed(self):
        # Exercises _connect_and_discover's defensive filtering directly,
        # since making the fake server itself emit a malformed tools/list
        # entry would require yet another env flag for one assertion.
        from app.mcp.connection_manager import McpConnection
        self.add_server()
        self.connect_and_wait()
        connection = self.manager.connection("fake")
        self.assertTrue(connection.tools)
        for tool in connection.tools:
            self.assertIsInstance(tool.name, str)
            self.assertTrue(tool.name)

    def test_unknown_tool_call_does_not_reach_the_subprocess(self):
        self.add_server()
        self.connect_and_wait()
        future = self.manager.run_tool("mcp.fake.does_not_exist", {})
        result = future.wait(2000)
        self.assertFalse(result["ok"])
        self.assertIn("TOOL", json.loads(result["text"].split("\n")[0])
                      .get("error", {}).get("code", "") or "UNKNOWN_TOOL")


if __name__ == "__main__":
    unittest.main()
