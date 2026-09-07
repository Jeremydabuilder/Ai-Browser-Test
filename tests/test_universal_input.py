"""AgentPanel._maybe_start_mission: promoting a typed, task-shaped message
into a Mission automatically, when nothing is already active.

"The browser should usually choose intelligently" between a quick question
and a trackable task - see the docstring on _maybe_start_mission in
app/ui/agent_panel.py. Deliberately narrow: only from the box the user
actually typed into, using the same looks_like_a_task heuristic the address
bar's "ask Py" icon already uses.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_universal_input -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-universal-input-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.config import AgentConfig  # noqa: E402
from app.agent.session import AgentSession  # noqa: E402
from app.browser.controller import BrowserController  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from app.missions import MissionService, MissionStore  # noqa: E402
from app.storage.database import Database  # noqa: E402
from app.ui.agent_panel import AgentPanel  # noqa: E402
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


def _database() -> Database:
    path = os.path.join(tempfile.mkdtemp(prefix="universal-input-db-"), "browser.sqlite3")
    return Database(path)


class MissionPromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tabs = TabManager(_profile, "about:blank")
        self.tabs.resize(900, 700)
        self.tabs.show()
        self.browser = BrowserController(self.tabs)
        self.browser.open_tab("about:blank").wait()
        self.db = _database()
        self.service = MissionService(MissionStore(self.db), self.browser, self.tabs)

    def tearDown(self) -> None:
        self.session.shutdown()
        self.panel.deleteLater()
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        QTest.qWait(10)
        self.db.close()

    def start(self, script) -> AgentPanel:
        self.session = AgentSession(self.browser, ScriptedClaude(script), AgentConfig(),
                                    missions=self.service)
        self.session.briefing_provider = self.service.briefing
        self.panel = AgentPanel(self.session, missions=self.service)
        return self.panel

    def run_task(self, panel: AgentPanel, text: str) -> None:
        done = []
        self.session.finished.connect(lambda: done.append(True))
        panel.input.setPlainText(text)
        panel._send()
        self.assertTrue(pump(lambda: done), "the task never finished")

    def test_a_task_shaped_message_starts_a_mission(self) -> None:
        panel = self.start([says("On it.")])
        self.run_task(panel, "find the cheapest flight to tokyo")
        self.assertIsNotNone(self.service.active)
        self.assertIn("cheapest flight to tokyo", self.service.active.goal)

    def test_an_ordinary_question_does_not_start_a_mission(self) -> None:
        panel = self.start([says("It's a documentation page.")])
        self.run_task(panel, "what is this page about")
        self.assertIsNone(self.service.active)

    def test_a_short_search_shaped_message_does_not_start_a_mission(self) -> None:
        panel = self.start([says("Sure.")])
        self.run_task(panel, "cheap laptops")
        self.assertIsNone(self.service.active)

    def test_the_transcript_says_a_mission_was_started(self) -> None:
        panel = self.start([says("On it.")])
        self.run_task(panel, "research the best budget mechanical keyboards")
        self.assertIn("Tracking this as a mission", panel.transcript.toPlainText())

    def test_an_already_active_mission_is_not_replaced(self) -> None:
        first = self.service.start("an existing mission")
        panel = self.start([says("On it.")])
        self.run_task(panel, "find the cheapest flight to tokyo")
        self.assertEqual(self.service.active.id, first.id)

    def test_a_quick_action_through_ask_does_not_start_a_mission(self) -> None:
        # _ask() carries retries, quick actions and "challenge this claim" -
        # requests the user did not compose themselves - not just the box
        # they typed into. See the docstring on _maybe_start_mission.
        panel = self.start([says("Done.")])
        done = []
        self.session.finished.connect(lambda: done.append(True))
        panel.ask("Research the best budget mechanical keyboards for me.")
        self.assertTrue(pump(lambda: done))
        self.assertIsNone(self.service.active)

    def test_the_mission_card_appears_after_promotion(self) -> None:
        panel = self.start([says("On it.")])
        self.run_task(panel, "compare these two laptops for me please")
        self.assertFalse(panel.mission_card.isHidden())


if __name__ == "__main__":
    unittest.main()
