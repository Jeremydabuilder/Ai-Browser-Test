"""Running a Skill through MainWindow, end to end: tool-allowlist
enforcement, provider-preference confirmation, @context integration
(without new Skill-specific plumbing), and output-schema validation.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_skill_execution_integration -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-skill-exec-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.agent.config import AgentConfig, PROVIDER_ANTHROPIC, PROVIDER_GROQ  # noqa: E402
from app.agent.session import AgentSession  # noqa: E402
from app.agent.skills import Skill, builtin_skill  # noqa: E402
from app.storage import Database  # noqa: E402
from app.ui.agent_panel import AgentPanel  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from tests.fake_claude import ScriptedClaude, says  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

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


class SkillExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.window = MainWindow(_profile, self.db, start_urls=["about:blank"])
        self.window.resize(1000, 700)
        # A safety net, not a test of anything itself: a scripted answer
        # that is not valid JSON, delivered after a schema-bearing Skill's
        # test method has already made its assertions, would otherwise pop
        # a REAL QMessageBox during teardown - which blocks forever in this
        # offscreen environment, since nothing can click it. Tests that
        # actually check this warning override it locally with their own
        # narrower mock.patch.
        self._warning_patcher = mock.patch("PySide6.QtWidgets.QMessageBox.warning")
        self._warning_patcher.start()

    def tearDown(self) -> None:
        self.window.close()
        self.db.close()
        self._dir.cleanup()
        for _ in range(3):
            _app.processEvents()
        self._warning_patcher.stop()

    def _wire_session(self, script, *, provider: str = PROVIDER_ANTHROPIC) -> ScriptedClaude:
        fake = ScriptedClaude(script)
        session = AgentSession(self.window.controller, fake, AgentConfig(provider=provider))
        self.window._agent_session = session
        panel = AgentPanel(session, self.window, self.window.missions, self.window.mcp,
                           browser=self.window.controller, highlights=self.window.highlights)
        self.window.set_side_panel(panel)
        self.fake = fake
        self.session = session
        return fake

    def test_running_a_skill_scopes_the_tools_offered_to_the_model(self) -> None:
        self._wire_session([says("done")])
        self.window._run_skill(builtin_skill("summarize"))
        pump(lambda: bool(self.fake.requests), 5000)
        names = {tool["name"] for tool in self.fake.requests[0]["tools"]}
        self.assertTrue(names)
        self.assertNotIn("browser_click", names)
        self.assertIn("browser_get_page_text", names)

    def test_the_allowlist_is_lifted_once_the_skill_task_finishes(self) -> None:
        # A plain skill with no output_schema - "summarize" has one, and a
        # scripted plain-text answer would trip real (unmocked) schema-
        # mismatch UI in this test, which is about the allowlist, not that.
        fake = self._wire_session([says("done")])
        done = []
        self.session.finished.connect(lambda: done.append(True))
        skill = Skill(id="s", name="S", description="", instructions="do it",
                     allowed_tools=("browser_get_page_text",))
        self.window._run_skill(skill)
        self.assertTrue(pump(lambda: bool(done)))
        self.assertIsNone(self.session._tools._allowed_tools)

    def test_the_skills_instructions_reach_the_model(self) -> None:
        fake = self._wire_session([says("done")])
        skill = Skill(id="s", name="S", description="", instructions="A UNIQUE INSTRUCTION STRING",
                     allowed_tools=None)
        self.window._run_skill(skill)
        pump(lambda: bool(fake.requests), 5000)
        content = fake.requests[0]["messages"][-1]["content"]
        self.assertIn("A UNIQUE INSTRUCTION STRING", content)

    def test_a_provider_preference_matching_the_current_one_asks_nothing(self) -> None:
        self._wire_session([says("done")], provider=PROVIDER_ANTHROPIC)
        skill = Skill(id="s", name="S", description="", instructions="do it",
                     preferred_provider=PROVIDER_ANTHROPIC)
        with mock.patch("PySide6.QtWidgets.QMessageBox.question") as question:
            self.window._run_skill(skill)
        question.assert_not_called()

    def test_a_mismatched_provider_preference_asks_before_running(self) -> None:
        self._wire_session([says("done")], provider=PROVIDER_GROQ)
        skill = Skill(id="s", name="S", description="", instructions="do it",
                     preferred_provider=PROVIDER_ANTHROPIC)
        with mock.patch("PySide6.QtWidgets.QMessageBox.question",
                       return_value=QMessageBox.StandardButton.Cancel) as question:
            self.window._run_skill(skill)
        question.assert_called_once()
        # Declined - nothing was sent.
        self.assertEqual(self.fake.requests, [])

    def test_continuing_past_the_provider_mismatch_warning_still_runs(self) -> None:
        fake = self._wire_session([says("done")], provider=PROVIDER_GROQ)
        skill = Skill(id="s", name="S", description="", instructions="do it",
                     preferred_provider=PROVIDER_ANTHROPIC)
        with mock.patch("PySide6.QtWidgets.QMessageBox.question",
                       return_value=QMessageBox.StandardButton.Yes):
            self.window._run_skill(skill)
        self.assertTrue(pump(lambda: bool(fake.requests), 5000))

    def test_output_schema_validation_warns_on_a_malformed_answer(self) -> None:
        fake = self._wire_session([says("not json at all")])
        skill = Skill(id="s", name="S", description="", instructions="do it",
                     output_schema={"type": "object", "required": ["summary"]})
        done = []
        self.session.finished.connect(lambda: done.append(True))
        with mock.patch("PySide6.QtWidgets.QMessageBox.warning") as warn:
            self.window._run_skill(skill)
            self.assertTrue(pump(lambda: bool(done)))
        warn.assert_called_once()

    def test_output_schema_validation_is_silent_on_a_matching_answer(self) -> None:
        fake = self._wire_session([says('{"summary": "all good"}')])
        skill = Skill(id="s", name="S", description="", instructions="do it",
                     output_schema={"type": "object", "required": ["summary"]})
        done = []
        self.session.finished.connect(lambda: done.append(True))
        with mock.patch("PySide6.QtWidgets.QMessageBox.warning") as warn:
            self.window._run_skill(skill)
            self.assertTrue(pump(lambda: bool(done)))
        warn.assert_not_called()

    def test_running_while_busy_is_refused_with_a_message(self) -> None:
        import threading

        gate = threading.Event()
        fake = self._wire_session([says("done")])
        fake.delay_event = gate
        self.session.send("something")
        self.assertTrue(pump(lambda: self.session.busy, 5000))
        with mock.patch("PySide6.QtWidgets.QMessageBox.information") as info:
            self.window._run_skill(builtin_skill("summarize"))
        info.assert_called_once()
        gate.set()
        self.assertTrue(pump(lambda: not self.session.busy, 10000))

    def test_auto_context_adds_the_current_tab_when_the_skill_wants_it_and_nothing_is_selected(self) -> None:
        fake = self._wire_session([says("done")])
        self.window.tabs.current_tab().navigate("data:text/html,<title>Auto Context Page</title>")
        self.assertTrue(pump(lambda: self.window.tabs.current_tab().title() == "Auto Context Page"))
        skill = Skill(id="s", name="S", description="", instructions="Summarize this",
                     allowed_tools=None, default_context_kinds=("tab",))
        self.window._run_skill(skill)
        pump(lambda: bool(fake.requests), 5000)
        content = fake.requests[0]["messages"][-1]["content"]
        self.assertIn("Auto Context Page", content)

    def test_an_explicit_selection_is_never_overridden_by_the_skills_default(self) -> None:
        from app.agent.context_items import ContextItem

        fake = self._wire_session([says("done")])
        panel = self.window._side_panel
        panel._composer.add(ContextItem(id="mission:99", kind="mission", title="Explicit Pick",
                                        ref={"mission_id": 99}))
        skill = Skill(id="s", name="S", description="", instructions="Go",
                     default_context_kinds=("tab",))
        self.window._run_skill(skill)
        pump(lambda: bool(fake.requests), 5000)
        content = fake.requests[0]["messages"][-1]["content"]
        self.assertIn("Explicit Pick", content)


if __name__ == "__main__":
    unittest.main()
