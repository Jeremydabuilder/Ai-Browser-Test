"""The AI panel: streaming, quick actions, and clearing the conversation.

The panel is a view - every decision belongs to AgentSession - so these tests
drive a real session with a scripted model and assert on what a person would
see in the transcript.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_agent_panel -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-panel-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.config import AgentConfig  # noqa: E402
from app.agent.session import AgentSession  # noqa: E402
from app.browser.controller import BrowserController  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from app.ui.agent_panel import QUICK_ACTIONS, AgentPanel  # noqa: E402
from tests.fake_claude import ScriptedClaude, says  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_profile = None


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _profile = shared_profile()


def tearDownModule() -> None:
    if _app is not None:
        for _ in range(3):
            _app.processEvents()


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


class PanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tabs = TabManager(_profile, "about:blank")
        self.tabs.resize(900, 700)
        self.tabs.show()
        self.browser = BrowserController(self.tabs)
        self.browser.open_tab("about:blank").wait()
        self.session: AgentSession | None = None
        self.panel: AgentPanel | None = None

    def tearDown(self) -> None:
        if self.session is not None:
            self.session.shutdown()
        if self.panel is not None:
            self.panel.deleteLater()
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        _app.processEvents()

    def start(self, script) -> AgentPanel:
        self.session = AgentSession(self.browser, ScriptedClaude(script), AgentConfig())
        self.panel = AgentPanel(self.session)
        return self.panel

    def run_task(self, panel: AgentPanel, text: str) -> None:
        done = []
        self.session.finished.connect(lambda: done.append(True))
        panel.input.setPlainText(text)
        panel._send()
        self.assertTrue(pump(lambda: done), "the task never finished")

    # -- streaming --------------------------------------------------------
    def test_the_answer_is_written_as_it_arrives(self) -> None:
        fragments: list[str] = []
        panel = self.start([says("Hello from the model.")])
        self.session.assistant_delta.connect(fragments.append)
        self.run_task(panel, "hi")
        self.assertGreater(len(fragments), 1, "the answer arrived in one piece")
        self.assertEqual("".join(fragments), "Hello from the model.")

    def test_a_streamed_answer_appears_exactly_once(self) -> None:
        # The finished message must not be appended on top of the streamed one.
        panel = self.start([says("Only once.")])
        self.run_task(panel, "hi")
        self.assertEqual(panel.transcript.toPlainText().count("Only once."), 1)

    def test_streamed_markup_is_shown_not_rendered(self) -> None:
        # Claude quotes untrusted pages; the transcript must display what it
        # said rather than interpret it.
        panel = self.start([says("The page says <b>buy now</b>.")])
        self.run_task(panel, "what does it say")
        self.assertIn("<b>buy now</b>", panel.transcript.toPlainText())

    # -- quick actions ----------------------------------------------------
    def test_quick_actions_send_a_real_message(self) -> None:
        panel = self.start([says("It is a documentation page.")])
        done = []
        self.session.finished.connect(lambda: done.append(True))
        panel.quick.itemAt(0).widget().click()
        self.assertTrue(pump(lambda: done))
        # The prompt goes through the ordinary path, so it is in the history
        # exactly as a typed message would be.
        self.assertEqual(self.session.messages[0]["content"], QUICK_ACTIONS[0][1])
        self.assertIn("documentation page", panel.transcript.toPlainText())

    def test_quick_actions_are_disabled_while_busy(self) -> None:
        panel = self.start([says("done")])
        button = panel.quick.itemAt(0).widget()
        self.assertTrue(button.isEnabled())
        self.session._set_state("thinking")
        self.assertFalse(button.isEnabled())

    def test_every_quick_action_has_a_prompt(self) -> None:
        for label, prompt in QUICK_ACTIONS:
            self.assertTrue(label.strip())
            self.assertTrue(prompt.strip().endswith(("?", ".")), prompt)

    # -- clearing ---------------------------------------------------------
    def test_clearing_empties_the_conversation_and_the_transcript(self) -> None:
        panel = self.start([says("First answer."), says("Second answer.")])
        self.run_task(panel, "one")
        self.assertTrue(self.session.messages)
        panel._clear()
        self.assertEqual(self.session.messages, [])
        self.assertNotIn("First answer.", panel.transcript.toPlainText())
        self.assertIn("cleared", panel.transcript.toPlainText().lower())

    def test_the_agent_still_works_after_clearing(self) -> None:
        panel = self.start([says("First answer."), says("Second answer.")])
        self.run_task(panel, "one")
        panel._clear()
        self.run_task(panel, "two")
        self.assertIn("Second answer.", panel.transcript.toPlainText())

    def test_clearing_is_refused_mid_task(self) -> None:
        # Dropping the history under an in-flight request would leave the next
        # turn answering tool results whose tool calls it can no longer see.
        panel = self.start([says("hi")])
        self.session._messages.append({"role": "user", "content": "in flight"})
        self.session._set_state("thinking")
        panel._clear()
        self.assertEqual(len(self.session.messages), 1)


if __name__ == "__main__":
    unittest.main()
