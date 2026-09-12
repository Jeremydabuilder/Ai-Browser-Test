"""MCP Phase 3: global permissions/audit management, destructive-tool
safety, redacted/truncated data preview, and connection-drop recovery.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mcp_phase3 -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-mcp-phase3-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.config import AgentConfig, ContextLimits  # noqa: E402
from app.agent.session import AgentSession  # noqa: E402
from app.agent.tools import ToolRegistry  # noqa: E402
from app.browser.controller import BrowserController  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from app.mcp.config import McpServerStore  # noqa: E402
from app.mcp.connection_manager import McpConnectionManager  # noqa: E402
from app.mcp.types import ConnectionState, McpServerConfig, Permission, Scope, Sensitivity, Transport  # noqa: E402
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


class Phase3TestCase(unittest.TestCase):
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

    def add_server(self, server_id: str = "fake", *, env_overrides=None) -> McpServerConfig:
        config = McpServerConfig(
            id=server_id, name="Fake MCP", transport=Transport.STDIO,
            enabled=True, command=sys.executable, args=(_SERVER_SCRIPT,),
            env=dict(env_overrides or {}))
        self.manager.add_or_update_server(config)
        return config

    def connect_and_wait(self, server_id: str = "fake") -> None:
        self.manager.connect_server(server_id)
        ok = pump(lambda: self.manager.state(server_id) in
                  (ConnectionState.CONNECTED, ConnectionState.ERROR))
        self.assertTrue(ok)
        self.assertEqual(self.manager.state(server_id), ConnectionState.CONNECTED,
                         self.manager.connection(server_id).last_error)

    def inject_destructive_tool(self, server_id: str = "fake",
                                name: str = "delete_everything") -> None:
        """fake_mcp_server.py has no destructive tool of its own (nothing
        it does is actually destructive) - this adds a classified-
        DESTRUCTIVE descriptor directly to the live connection's discovered
        tools, exactly the shape a real destructive tool would arrive as.
        """
        from app.mcp.types import McpToolDescriptor
        connection = self.manager.connection(server_id)
        connection.tools = list(connection.tools) + [McpToolDescriptor(
            server_id=server_id, name=name, description="Deletes everything.",
            input_schema={"properties": {"confirm": {"type": "boolean"}}},
            sensitivity=Sensitivity.DESTRUCTIVE)]


class GlobalPermissionReviewTests(Phase3TestCase):
    def test_permission_scope_for_reports_always_after_a_settings_change(self):
        self.add_server()
        self.connect_and_wait()
        self.manager.set_tool_permission("fake", "create_item", Permission.ALLOW)
        self.assertEqual(self.manager.permission_scope_for("fake", "create_item"), Scope.ALWAYS)

    def test_permission_scope_for_is_empty_with_no_remembered_decision(self):
        self.add_server()
        self.connect_and_wait()
        self.assertEqual(self.manager.permission_scope_for("fake", "create_item"), "")

    def test_permission_scope_for_read_only_tool_is_empty(self):
        self.add_server()
        self.connect_and_wait()
        self.assertEqual(self.manager.permission_scope_for("fake", "echo"), "")

    def test_reviewing_every_server_needs_no_per_server_dialog(self):
        # The point of the global view: everything is available from the
        # manager without ever calling connect()/state() per server first.
        self.add_server("a")
        self.add_server("b")
        self.connect_and_wait("a")
        self.connect_and_wait("b")
        all_tools = [(config.id, tool.name)
                    for config in self.manager.configured_servers()
                    for tool in self.manager.all_tools(config.id)]
        self.assertTrue(any(server == "a" for server, _ in all_tools))
        self.assertTrue(any(server == "b" for server, _ in all_tools))


class ResetPermissionsTests(Phase3TestCase):
    def test_reset_server_clears_only_that_servers_permissions(self):
        self.add_server("a")
        self.add_server("b")
        self.connect_and_wait("a")
        self.connect_and_wait("b")
        self.manager.set_tool_permission("a", "create_item", Permission.ALLOW)
        self.manager.set_tool_permission("b", "create_item", Permission.ALLOW)
        self.manager.reset_server_permissions("a")
        self.assertEqual(self.manager.permission_for("a", "create_item"), Permission.ASK)
        self.assertEqual(self.manager.permission_for("b", "create_item"), Permission.ALLOW)

    def test_reset_all_clears_every_server(self):
        self.add_server("a")
        self.add_server("b")
        self.connect_and_wait("a")
        self.connect_and_wait("b")
        self.manager.set_tool_permission("a", "create_item", Permission.ALLOW)
        self.manager.set_tool_permission("b", "create_item", Permission.DENY)
        self.manager.reset_all_permissions()
        self.assertEqual(self.manager.permission_for("a", "create_item"), Permission.ASK)
        self.assertEqual(self.manager.permission_for("b", "create_item"), Permission.ASK)

    def test_removing_a_server_cleans_up_its_permissions(self):
        self.add_server()
        self.connect_and_wait()
        self.manager.set_tool_permission("fake", "create_item", Permission.ALLOW)
        self.manager.remove_server("fake")
        # Re-adding the same server id starts with a clean slate - the old
        # permission is gone, not silently reattached to the new instance.
        self.add_server()
        self.connect_and_wait()
        self.assertEqual(self.manager.permission_for("fake", "create_item"), Permission.ASK)


class DestructiveToolSafetyTests(Phase3TestCase):
    def test_set_tool_permission_refuses_allow_on_a_destructive_tool(self):
        self.add_server()
        self.connect_and_wait()
        self.inject_destructive_tool()
        applied = self.manager.set_tool_permission("fake", "delete_everything", Permission.ALLOW)
        self.assertFalse(applied)
        self.assertEqual(self.manager.permission_for("fake", "delete_everything"),
                         Permission.ASK)

    def test_set_tool_permission_still_allows_ask_and_deny_on_destructive(self):
        self.add_server()
        self.connect_and_wait()
        self.inject_destructive_tool()
        self.assertTrue(
            self.manager.set_tool_permission("fake", "delete_everything", Permission.DENY))
        self.assertEqual(self.manager.permission_for("fake", "delete_everything"),
                         Permission.DENY)

    def test_remember_permission_for_refuses_always_allow_on_destructive(self):
        self.add_server()
        self.connect_and_wait()
        self.inject_destructive_tool()
        self.manager.remember_permission_for(
            "mcp.fake.delete_everything", Permission.ALLOW, Scope.ALWAYS)
        self.assertEqual(self.manager.permission_for("fake", "delete_everything"),
                         Permission.ASK)

    def test_remember_permission_for_allows_mission_scoped_allow_on_destructive(self):
        # Only "Always" is refused - a Mission-scoped Allow is still the
        # user's call to make, bounded to one Mission's lifetime.
        self.add_server()
        self.connect_and_wait()
        self.inject_destructive_tool()
        mission = self.missions.start("Clean up test data")
        self.manager.remember_permission_for(
            "mcp.fake.delete_everything", Permission.ALLOW, Scope.MISSION,
            mission_id=mission.id)
        self.assertEqual(
            self.manager.permission_for("fake", "delete_everything", mission_id=mission.id),
            Permission.ALLOW)

    def test_write_and_sensitive_tools_may_still_be_always_allowed(self):
        self.add_server()
        self.connect_and_wait()
        self.assertTrue(
            self.manager.set_tool_permission("fake", "create_item", Permission.ALLOW))
        self.assertEqual(self.manager.permission_for("fake", "create_item"), Permission.ALLOW)


class AuditLogTests(Phase3TestCase):
    def test_a_read_only_call_is_logged_as_auto(self):
        self.add_server()
        self.connect_and_wait()
        registry = ToolRegistry(self.browser, mcp=self.manager)
        registry.run("mcp.fake.echo", {"phrase": "hi"}).mcp_future.wait(5000)
        entries = self.manager.audit_entries(server_id="fake")
        self.assertTrue(any(e.tool_name == "echo" and e.decision == "auto" for e in entries))

    def test_a_remembered_denial_is_logged_without_running(self):
        self.add_server()
        self.connect_and_wait()
        self.manager.set_tool_permission("fake", "create_item", Permission.DENY)
        registry = ToolRegistry(self.browser, mcp=self.manager)
        registry.assess("mcp.fake.create_item", {"item_id": "1", "text": "x"})
        entries = self.manager.audit_entries(server_id="fake", allowed=False)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].outcome, "not_executed")

    def test_a_live_decline_is_logged(self):
        self.add_server()
        self.connect_and_wait()
        session = AgentSession(self.browser, ScriptedClaude([
            calls("mcp.fake.create_item", {"item_id": "1", "text": "x"}),
            lambda m: says("ok"),
        ]), AgentConfig(limits=ContextLimits()), mcp=self.manager)
        try:
            done = []
            session.finished.connect(lambda: done.append(True))
            session.confirmation_required.connect(
                lambda r: session.resolve_confirmation(False, None, Scope.ONCE))
            self.assertTrue(session.send("create an item"))
            self.assertTrue(pump(lambda: bool(done)))
            entries = self.manager.audit_entries(server_id="fake", allowed=False)
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].decision, "denied_once")
        finally:
            session.shutdown()

    def test_filtering_by_mission(self):
        self.add_server()
        self.connect_and_wait()
        mission = self.missions.start("Track this")
        registry = ToolRegistry(self.browser, missions=self.missions, mcp=self.manager)
        registry.run("mcp.fake.echo", {"phrase": "hi"}).mcp_future.wait(5000)
        entries = self.manager.audit_entries(mission_id=mission.id)
        self.assertEqual(len(entries), 1)
        # The title snapshot, whatever start() derived it as (a short title
        # from the goal, not necessarily the goal verbatim) - the audit
        # entry just has to match the Mission's actual title, not guess it.
        self.assertEqual(entries[0].mission_title, mission.title)
        self.assertTrue(entries[0].mission_title)

    def test_clear_audit_empties_the_log(self):
        self.add_server()
        self.connect_and_wait()
        registry = ToolRegistry(self.browser, mcp=self.manager)
        registry.run("mcp.fake.echo", {"phrase": "hi"}).mcp_future.wait(5000)
        self.manager.clear_audit()
        self.assertEqual(self.manager.audit_entries(), [])

    def test_no_secrets_or_payloads_are_ever_stored(self):
        self.add_server()
        self.connect_and_wait()
        registry = ToolRegistry(self.browser, mcp=self.manager)
        registry.run("mcp.fake.echo", {"phrase": "a very specific secret phrase"}
                    ).mcp_future.wait(5000)
        entries = self.manager.audit_entries()
        for entry in entries:
            for field in (entry.server_id, entry.server_name, entry.tool_name,
                         entry.mission_title, entry.decision, entry.outcome,
                         entry.error_code):
                self.assertNotIn("a very specific secret phrase", field)


class ConnectionRecoveryTests(Phase3TestCase):
    def test_disconnect_signal_carries_server_and_tool(self):
        self.add_server()
        self.connect_and_wait()
        connection = self.manager.connection("fake")
        connection.client = _BrokenClient()

        seen = []
        self.manager.connection_dropped.connect(
            lambda sid, name, tool: seen.append((sid, name, tool)))
        registry = ToolRegistry(self.browser, mcp=self.manager)
        outcome = registry.run("mcp.fake.echo", {"phrase": "hi"})
        pump(lambda: bool(seen), 5000)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0], ("fake", "Fake MCP", "echo"))
        self.assertEqual(self.manager.state("fake"), ConnectionState.ERROR)

    def test_reconnect_after_drop_does_not_replay_the_failed_call(self):
        self.add_server()
        self.connect_and_wait()
        connection = self.manager.connection("fake")
        connection.client = _BrokenClient()

        registry = ToolRegistry(self.browser, mcp=self.manager)
        outcome = registry.run("mcp.fake.create_item", {"item_id": "x1", "text": "hi"})
        outcome.mcp_future.wait(5000)
        self.assertEqual(self.manager.state("fake"), ConnectionState.ERROR)

        # Reconnect - a fresh subprocess, with no memory of the failed call.
        self.manager.reconnect_server("fake")
        pump(lambda: self.manager.state("fake") in
             (ConnectionState.CONNECTED, ConnectionState.ERROR))
        self.assertEqual(self.manager.state("fake"), ConnectionState.CONNECTED)

        # The item was never created - no automatic replay happened.
        check = registry.run("mcp.fake.get_item", {"item_id": "x1"})
        result = check.mcp_future.wait(5000)
        self.assertIn("no such item", result["text"])

    def test_resuming_a_read_after_reconnect_succeeds_once(self):
        self.add_server()
        self.connect_and_wait()
        connection = self.manager.connection("fake")
        connection.client = _BrokenClient()
        registry = ToolRegistry(self.browser, mcp=self.manager)
        registry.run("mcp.fake.echo", {"phrase": "hi"}).mcp_future.wait(5000)
        self.assertEqual(self.manager.state("fake"), ConnectionState.ERROR)

        self.manager.reconnect_server("fake")
        pump(lambda: self.manager.state("fake") in
             (ConnectionState.CONNECTED, ConnectionState.ERROR))
        self.assertEqual(self.manager.state("fake"), ConnectionState.CONNECTED)
        result = registry.run("mcp.fake.echo", {"phrase": "hi again"}).mcp_future.wait(5000)
        self.assertTrue(result["ok"])
        self.assertIn("hi again", result["text"])


class _BrokenClient:
    """Stands in for a live client whose transport has died - every call
    raises the same protocol-level error run_tool() already knows how to
    turn into a connection_dropped signal."""

    async def call_tool(self, name, arguments, timeout=30.0):
        from app.mcp.protocol import McpProtocolError
        raise McpProtocolError("the connection was reset")

    async def close(self):
        pass


if __name__ == "__main__":
    unittest.main()
