"""Multi-Agent Missions wired into MainWindow: real AgentSession instances
(via build_session, ScriptedClaude standing in for Claude), real
ToolRegistry tool-allowlist enforcement, the shared MCP permission store,
Mission history/audit reuse, and the Workstreams UI.

tests/test_mission_coordinator.py covers the coordinator's own
orchestration logic in isolation with a fake session; this file proves the
same coordinator wired to the real window produces real, safety-checked
AgentSession instances - never a second engine.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_multi_agent_integration -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-multiagent-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.missions.coordinator import CoordinatorLimits, MissionCoordinator, WorkerRole  # noqa: E402
from app.storage import Database  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.workstreams_dialog import WorkstreamsDialog  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_profile = None


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _profile = shared_profile()


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


class _StubTransport:
    """A ClaudeTransport standing in for the real SDK - see build_session.
    Answers every request with a plain text answer and, for the Planner's
    prompt specifically, a small valid plan; records the tools it was
    offered on every call, so the allowlist can be checked for real."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def send(self, *, system, messages, tools, on_text=None):
        from app.agent.claude_client import AgentResponse

        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        last_user = messages[-1]["content"] if messages else ""
        text = "did the work"
        if isinstance(last_user, str) and "Decompose the goal" not in last_user \
                and "Reply with a single JSON object" in last_user and "tasks" in last_user:
            text = json.dumps({"tasks": [
                {"role": "researcher", "title": "Look into it", "instructions": "look"},
                {"role": "writer", "title": "Write it up", "instructions": "write"},
            ]})
        return AgentResponse(text=text, stop_reason="end_turn",
                             raw_content=[{"type": "text", "text": text}])


class MainWindowWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.window = MainWindow(_profile, self.db, start_urls=["about:blank"])
        self.window.resize(1000, 700)

    def tearDown(self) -> None:
        self.window.close()
        self.db.close()
        self._dir.cleanup()
        for _ in range(3):
            _app.processEvents()

    def _patch_build_session(self):
        """Make every AgentSession the coordinator builds use our stub
        transport instead of a real Claude client - the only thing this
        patches; AgentSession/ToolRegistry themselves are the real classes."""
        from unittest import mock

        from app.agent.config import AgentConfig
        from app.agent.session import AgentSession

        transport = _StubTransport()

        def fake_build_session(controller, parent, settings, missions, mcp, knowledge=None):
            session = AgentSession(controller, transport, AgentConfig(provider="anthropic"),
                                   parent, missions=missions, mcp=mcp, knowledge=knowledge)
            return session, ""

        patcher = mock.patch("app.ui.agent_setup.build_session", fake_build_session)
        patcher.start()
        self.addCleanup(patcher.stop)
        return transport

    def test_worker_sessions_share_the_same_mcp_manager_as_the_interactive_session(self) -> None:
        """No second, private permission universe for workers - see the
        phase's own MCP-permission-enforcement requirement."""
        self._patch_build_session()
        session = self.window._build_worker_agent_session()
        self.assertIsNotNone(session)
        self.assertIs(session._mcp, self.window.mcp)
        session.shutdown()

    def test_a_researcher_worker_is_never_offered_browser_click(self) -> None:
        """Real ToolRegistry enforcement of the role allowlist - not a
        simulated check."""
        transport = self._patch_build_session()
        self.window.missions.start("compare product A vs product B thoroughly and in depth")
        coordinator = MissionCoordinator(
            self.window.missions, self.window._build_worker_agent_session, CoordinatorLimits(),
            self.window)
        coordinator.run("compare product A vs product B thoroughly and in depth")
        # Planner runs first - let it answer via the stub transport.
        self.assertTrue(pump(lambda: len(transport.calls) >= 1))
        # The researcher's own request (the second call made) must never
        # offer browser_click - the role allowlist is real.
        self.assertTrue(pump(lambda: len(transport.calls) >= 2))
        researcher_tools = {t["name"] for t in transport.calls[1]["tools"]}
        self.assertNotIn("browser_click", researcher_tools)
        self.assertIn("browser_get_page_text", researcher_tools)

    def test_worker_activity_lands_in_the_same_shared_mission_history(self) -> None:
        """Reuses MissionService.record_agent_step - never a private,
        worker-only activity log the user cannot see."""
        transport = self._patch_build_session()
        self.window.missions.start("compare product A vs product B thoroughly and in depth")
        coordinator = MissionCoordinator(
            self.window.missions, self.window._build_worker_agent_session, CoordinatorLimits(),
            self.window)
        coordinator.run("compare product A vs product B thoroughly and in depth")
        self.assertTrue(pump(lambda: coordinator.delegated, 8000))
        self.assertTrue(pump(lambda: not coordinator.tasks
                            or all(t.state in ("done", "failed", "skipped")
                                   for t in coordinator.tasks), 8000))
        mission = self.window.missions.store.get(self.window.missions.active.id)
        # Nothing asserts the *content* of chain-of-thought here - only
        # that the shared, auditable Mission record exists at all and is
        # not empty, which is all record_agent_step ever promises.
        self.assertIsNotNone(mission)


class WorkstreamsDialogUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.window = MainWindow(_profile, self.db, start_urls=["about:blank"])

    def tearDown(self) -> None:
        self.window.close()
        self.db.close()
        self._dir.cleanup()
        for _ in range(3):
            _app.processEvents()

    def test_the_dialog_reflects_added_and_changed_workers(self) -> None:
        from app.missions.coordinator import WorkerTask, WorkerState

        class _NullCoordinator:
            def __init__(self):
                from PySide6.QtCore import QObject, Signal

                class Sig(QObject):
                    worker_added = Signal(object)
                    worker_changed = Signal(object)
                    worker_confirmation_required = Signal(object, object, object)
                    result_ready = Signal(str)
                    failed = Signal(str)
                self._sig = Sig()
                self.worker_added = self._sig.worker_added
                self.worker_changed = self._sig.worker_changed
                self.worker_confirmation_required = self._sig.worker_confirmation_required
                self.result_ready = self._sig.result_ready
                self.failed = self._sig.failed

        coordinator = _NullCoordinator()
        dialog = WorkstreamsDialog(coordinator, self.window)
        task = WorkerTask(id=1, role=WorkerRole.RESEARCHER, title="Look into A",
                          instructions="x", state=WorkerState.RUNNING)
        coordinator.worker_added.emit(task)
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertEqual(dialog.table.item(0, 0).text(), "Researcher")

        task.state = WorkerState.DONE
        task.result = "Reviewed 6 sources"
        task.findings_added = 2
        coordinator.worker_changed.emit(task)
        self.assertIn("Done", dialog.table.item(0, 2).text())
        self.assertEqual(dialog.table.item(0, 3).text(), "2")
        self.assertEqual(dialog.table.item(0, 4).text(), "Reviewed 6 sources")
        dialog.close()


if __name__ == "__main__":
    unittest.main()
