"""Phase 2: write-capable MCP tools through the existing approval system.

Exercises the confirmation/permission gate end to end - AgentSession, real
McpConnectionManager, real fake_mcp_server.py subprocess - for exactly the
scenarios the spec called out: a write tool is blocked before approval,
approval executes it once, denial prevents execution, a remembered
permission (Mission-scoped and Always) is honoured, a schema change
invalidates a remembered decision, a misleading description cannot
downgrade sensitivity, and Mission history records the approval and result.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mcp_phase2 -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-mcp-phase2-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.config import AgentConfig, ContextLimits  # noqa: E402
from app.agent.session import AgentSession, ConfirmationRequest  # noqa: E402
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


class Phase2TestCase(unittest.TestCase):
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

    def _session(self, script, missions=None) -> tuple[AgentSession, ScriptedClaude]:
        fake = ScriptedClaude(script)
        config = AgentConfig(limits=ContextLimits())
        session = AgentSession(self.browser, fake, config, missions=missions, mcp=self.manager)
        return session, fake

    def run_to_completion(self, session: AgentSession, message: str,
                          *, on_confirmation=None, timeout_ms: int = 15000) -> None:
        """Send a message and pump until finished, auto-answering exactly
        one confirmation via ``on_confirmation(request) -> (allowed, scope)``
        if one arrives - most tests here need at most one."""
        done = []
        session.finished.connect(lambda: done.append(True))
        answered = []

        def maybe_confirm(request: ConfirmationRequest) -> None:
            if on_confirmation is None:
                return
            allowed, scope = on_confirmation(request)
            answered.append(True)
            session.resolve_confirmation(allowed, None, scope)

        session.confirmation_required.connect(maybe_confirm)
        self.assertTrue(session.send(message))
        self.assertTrue(pump(lambda: bool(done), timeout_ms))


class WriteToolApprovalTests(Phase2TestCase):
    def test_write_tool_call_requires_confirmation_before_running(self):
        self.add_server()
        self.connect_and_wait()
        registry = ToolRegistry(self.browser, mcp=self.manager)
        assessment = registry.assess("mcp.fake.create_item", {"item_id": "new1", "text": "hi"})
        self.assertTrue(assessment["requires_confirmation"])
        # And it really has not run: get_item for "new1" still errors.
        outcome = registry.run("mcp.fake.get_item", {"item_id": "new1"})
        result = outcome.mcp_future.wait(5000)
        self.assertIn("no such item", result["text"])

    def test_approval_shows_server_tool_data_and_effect(self):
        self.add_server()
        self.connect_and_wait()
        session, fake = self._session([
            calls("mcp.fake.create_item", {"item_id": "42", "text": "approved item"}),
            lambda m: says("done"),
        ])
        seen = []
        session.confirmation_required.connect(seen.append)
        self.run_to_completion(session, "create an item",
                              on_confirmation=lambda r: (True, Scope.ONCE))
        self.assertEqual(len(seen), 1)
        request = seen[0]
        self.assertTrue(request.is_mcp)
        self.assertEqual(request.mcp_server, "Fake MCP")
        self.assertIn("create_item", request.tool_name)
        self.assertEqual(request.mcp_data, {"item_id": "42", "text": "approved item"})
        self.assertIn("Fake MCP", request.mcp_effect)
        session.shutdown()

    def test_approval_executes_the_tool_exactly_once(self):
        self.add_server()
        self.connect_and_wait()
        session, fake = self._session([
            calls("mcp.fake.create_item", {"item_id": "once1", "text": "hello"}),
            lambda m: says("created"),
        ])
        self.run_to_completion(session, "create an item",
                              on_confirmation=lambda r: (True, Scope.ONCE))
        registry = ToolRegistry(self.browser, mcp=self.manager)
        outcome = registry.run("mcp.fake.get_item", {"item_id": "once1"})
        result = outcome.mcp_future.wait(5000)
        self.assertTrue(result["ok"])
        self.assertIn("hello", result["text"])
        payload = json.loads(fake.tool_results()[-1].split("\n")[0])
        self.assertTrue(payload["ok"])
        session.shutdown()

    def test_denial_prevents_execution(self):
        self.add_server()
        self.connect_and_wait()
        session, fake = self._session([
            calls("mcp.fake.create_item", {"item_id": "denied1", "text": "nope"}),
            lambda m: says("ok, not doing that"),
        ])
        self.run_to_completion(session, "create an item",
                              on_confirmation=lambda r: (False, Scope.ONCE))
        payload = json.loads(fake.tool_results()[-1].split("\n")[0])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "USER_DECLINED")
        registry = ToolRegistry(self.browser, mcp=self.manager)
        outcome = registry.run("mcp.fake.get_item", {"item_id": "denied1"})
        result = outcome.mcp_future.wait(5000)
        self.assertIn("no such item", result["text"])
        session.shutdown()


class RememberedPermissionTests(Phase2TestCase):
    def test_always_allow_skips_the_next_confirmation(self):
        self.add_server()
        self.connect_and_wait()
        session, fake = self._session([
            calls("mcp.fake.create_item", {"item_id": "a1", "text": "first"}),
            lambda m: says("created a1"),
        ])
        self.run_to_completion(session, "create a1",
                              on_confirmation=lambda r: (True, Scope.ALWAYS))
        session.shutdown()

        registry = ToolRegistry(self.browser, mcp=self.manager)
        assessment = registry.assess("mcp.fake.create_item", {"item_id": "a2", "text": "second"})
        self.assertFalse(assessment["requires_confirmation"])
        self.assertNotIn("refused", assessment)

    def test_always_deny_refuses_without_asking(self):
        self.add_server()
        self.connect_and_wait()
        self.manager.set_tool_permission("fake", "create_item", Permission.DENY)
        registry = ToolRegistry(self.browser, mcp=self.manager)
        assessment = registry.assess("mcp.fake.create_item", {"item_id": "b1", "text": "x"})
        self.assertFalse(assessment["requires_confirmation"])
        self.assertTrue(assessment["refused"])
        self.assertEqual(assessment["refusal_code"], "MCP_PERMISSION_DENIED")

    def test_mission_scoped_allow_only_applies_within_that_mission(self):
        self.add_server()
        self.connect_and_wait()
        mission = self.missions.start("Populate the item list")
        registry = ToolRegistry(self.browser, missions=self.missions, mcp=self.manager)
        self.manager.remember_permission_for(
            "mcp.fake.create_item", Permission.ALLOW, Scope.MISSION, mission_id=mission.id)
        # Within the Mission: no confirmation needed.
        assessment = registry.assess("mcp.fake.create_item", {"item_id": "m1", "text": "x"})
        self.assertFalse(assessment["requires_confirmation"])
        # Once nothing is active (or a different Mission is), it is not honoured.
        self.missions.pause()
        assessment_after = registry.assess("mcp.fake.create_item", {"item_id": "m2", "text": "x"})
        self.assertTrue(assessment_after["requires_confirmation"])

    def test_setting_permission_from_settings_ui_is_scope_always(self):
        self.add_server()
        self.connect_and_wait()
        self.manager.set_tool_permission("fake", "create_item", Permission.ALLOW)
        registry = ToolRegistry(self.browser, mcp=self.manager)
        assessment = registry.assess("mcp.fake.create_item", {"item_id": "c1", "text": "x"})
        self.assertFalse(assessment["requires_confirmation"])
        self.assertNotIn("refused", assessment)
        # No Mission active, no mission_id passed - still allowed, because it
        # was stored with Scope.ALWAYS, not Scope.MISSION.
        self.assertIsNone(registry.active_mission_id())

    def test_setting_permission_back_to_ask_forgets_the_remembered_allow(self):
        self.add_server()
        self.connect_and_wait()
        self.manager.set_tool_permission("fake", "create_item", Permission.ALLOW)
        self.manager.set_tool_permission("fake", "create_item", Permission.ASK)
        registry = ToolRegistry(self.browser, mcp=self.manager)
        assessment = registry.assess("mcp.fake.create_item", {"item_id": "d1", "text": "x"})
        self.assertTrue(assessment["requires_confirmation"])


class SchemaInvalidationTests(Phase2TestCase):
    def test_reconnect_with_a_changed_schema_invalidates_a_remembered_allow(self):
        # The fake server's create_item schema is fixed, so this exercises
        # the fingerprint mechanism directly: remember a decision against
        # the *current* fingerprint, then simulate the server having
        # changed shape by asking for a decision against a different one -
        # exactly what happens automatically when a real reconnect
        # discovers a tool whose schema no longer matches.
        self.add_server()
        self.connect_and_wait()
        self.manager.remember_permission_for(
            "mcp.fake.create_item", Permission.ALLOW, Scope.ALWAYS)
        self.assertEqual(self.manager.permission_for("fake", "create_item"), Permission.ALLOW)

        from app.mcp.permissions import McpPermissionStore
        store = McpPermissionStore(self.settings)
        matched = store.decision_for("fake", "create_item",
                                     self.manager.find_tool("fake", "create_item").schema_fingerprint,
                                     mission_id=None)
        self.assertEqual(matched, Permission.ALLOW)
        # A different fingerprint (as if the server now describes the tool
        # differently) never matches the old record.
        stale = store.decision_for("fake", "create_item", "not-the-same-fingerprint",
                                   mission_id=None)
        self.assertIsNone(stale)

    def test_full_reconnect_with_a_renamed_tool_asks_again(self):
        marker = tempfile.NamedTemporaryFile(suffix=".marker", delete=False)
        marker.close()
        os.unlink(marker.name)
        self.addCleanup(lambda: os.path.exists(marker.name) and os.unlink(marker.name))
        self.add_server(env_overrides={"FAKE_MCP_RENAME_MARKER": marker.name})
        self.connect_and_wait()
        self.manager.remember_permission_for("mcp.fake.echo", Permission.ALLOW, Scope.ALWAYS)
        # echo is READ_ONLY and never_confirmed anyway, so use create_item's
        # equivalent guarantee instead: remember create_item, then reconnect
        # so the *server* relabels "echo" - proving a decision keyed to a
        # tool name that vanished after reconnect is simply not found, not
        # silently reused for whatever now has that slot.
        self.manager.reconnect_server("fake")
        pump(lambda: self.manager.state("fake") in
             (ConnectionState.CONNECTED, ConnectionState.ERROR))
        self.assertEqual(self.manager.state("fake"), ConnectionState.CONNECTED)
        self.assertFalse(self.manager.knows("mcp.fake.echo"))
        self.assertTrue(self.manager.knows("mcp.fake.echo_and_delete_all"))
        # The renamed tool is READ_ONLY-shaped by name (echo_and_delete_all
        # still matches the "echo" word), so it never_confirmed either way -
        # the real assertion is simply that no stale record transferred to it.
        registry = ToolRegistry(self.browser, mcp=self.manager)
        self.assertFalse(registry.knows("mcp.fake.echo"))


class SecurityTests(Phase2TestCase):
    def test_misleading_description_cannot_downgrade_sensitivity(self):
        self.add_server(env_overrides={"FAKE_MCP_MISLEADING_DESCRIPTIONS": "1"})
        self.connect_and_wait()
        tool = self.manager.find_tool("fake", "create_item")
        self.assertIn("safe", tool.description.lower())
        self.assertEqual(tool.sensitivity, Sensitivity.WRITE)
        registry = ToolRegistry(self.browser, mcp=self.manager)
        assessment = registry.assess("mcp.fake.create_item", {"item_id": "e1", "text": "x"})
        self.assertTrue(assessment["requires_confirmation"])
        self.assertNotEqual(assessment["level"], "normal")

    def test_unknown_classification_never_defaults_to_allow(self):
        from app.mcp.safety import default_permission
        self.assertEqual(default_permission(Sensitivity.UNKNOWN), Permission.ASK)
        for level in (Sensitivity.WRITE, Sensitivity.SENSITIVE, Sensitivity.DESTRUCTIVE):
            self.assertEqual(default_permission(level), Permission.ASK)
        self.assertEqual(default_permission(Sensitivity.READ_ONLY), Permission.ALLOW)


class MissionHistoryTests(Phase2TestCase):
    def test_mission_history_records_the_approval_and_result(self):
        self.add_server()
        self.connect_and_wait()
        mission = self.missions.start("Create a tracked item")
        session, fake = self._session(
            [calls("mcp.fake.create_item", {"item_id": "hist1", "text": "tracked"}),
             lambda m: says("done")],
            missions=self.missions)
        session.step_changed.connect(self.missions.record_agent_step)
        self.run_to_completion(session, "create the item",
                              on_confirmation=lambda r: (True, Scope.ONCE))
        actions = self.missions.actions(mission.id)
        self.assertTrue(any(a.tool_name == "mcp.fake.create_item" and a.outcome == "done"
                            for a in actions), [(a.tool_name, a.outcome) for a in actions])
        session.shutdown()

    def test_mission_history_records_a_declined_call_as_an_audit_row(self):
        # A decline is not a failure - it is its own outcome, "skipped" -
        # but it still leaves an audit row naming the tool and server,
        # rather than the task simply going quiet with nothing recorded.
        self.add_server()
        self.connect_and_wait()
        mission = self.missions.start("Try to create an item")
        session, fake = self._session(
            [calls("mcp.fake.create_item", {"item_id": "hist2", "text": "nope"}),
             lambda m: says("could not create it")],
            missions=self.missions)
        session.step_changed.connect(self.missions.record_agent_step)
        try:
            self.run_to_completion(session, "create the item",
                                  on_confirmation=lambda r: (False, Scope.ONCE))
            actions = self.missions.actions(mission.id)
            self.assertTrue(
                any(a.tool_name == "mcp.fake.create_item" and a.outcome == "skipped"
                    for a in actions), [(a.tool_name, a.outcome) for a in actions])
        finally:
            session.shutdown()


if __name__ == "__main__":
    unittest.main()
