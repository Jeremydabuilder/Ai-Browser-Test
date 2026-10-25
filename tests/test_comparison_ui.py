"""Starting a Team run from a browsing Mission, and saving its summary back.

Run with:  QT_QPA_PLATFORM=offscreen QTWEBENGINE_DISABLE_SANDBOX=1 python tests/run.py tests.test_comparison_ui
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

import app.browser  # noqa: E402,F401

from app.team.handback import Handoff  # noqa: E402
from app.team.model import MissionStatus  # noqa: E402
from tests.test_team_ui import TeamUITestCase, pump, setUpModule as _ui_setup, wait_for  # noqa: E402


def setUpModule() -> None:
    _ui_setup()


class FakeBrowser:
    def __init__(self, tabs):
        self.tabs = tabs

    def list_tabs(self):
        return self.tabs

    def get_page_text(self, tab_id, max_chars=0):
        class F:
            def then(self_inner, cb):
                cb(SimpleNamespace(ok=True, data={"text": "Widget A costs $10 and ships to Canada."}, error=None))
        return F()


class HandoffAndHandBack(TeamUITestCase):
    def offer(self, urls=()):
        self.controller.offer_handoff(Handoff(
            goal="Pick a widget", requirements=["Under $130", "Ships to Canada"], linked_mission_id=42,
            notes="Notes saved on the user's browsing Mission (not independently verified):\n- A is cheap",
            urls=list(urls)))
        pump(10)

    def test_the_panel_prefills_goal_requirements_and_notes_without_starting(self):
        self.build(browser=FakeBrowser([]))
        self.offer()
        self.assertEqual(self.panel.goal.toPlainText(), "Pick a widget")
        self.assertEqual(self.panel.requirements.toPlainText().splitlines(), ["Under $130", "Ships to Canada"])
        self.assertEqual([s.title for s in self.panel._sources], ["Notes saved on your Mission"])
        self.assertIsNone(self.controller.snapshot())                  # nothing ran

    def test_open_pages_are_attached_and_unopened_ones_are_listed_not_fetched(self):
        browser = FakeBrowser([{"tab_id": 1, "url": "https://a.example", "title": "A", "active": True}])
        self.build(browser=browser)
        self.offer(["https://a.example", "https://not-open.example/x"])
        titles = [s.title for s in self.panel._sources]
        self.assertIn("A", titles)
        self.assertEqual(self.panel._sources[-1].retrieved[:2], "20")  # retrieval date recorded
        self.assertIn("not-open.example", self.panel.compose_error.text())
        self.assertIn("1 page(s)", self.panel.compose_error.text())

    def test_the_run_keeps_the_link_and_requirements_then_saves_back_on_request(self):
        self.build(browser=FakeBrowser([]))
        saved = []
        self.controller.mission_sink = lambda mid, text: saved.append((mid, text)) or None
        self.offer()
        self.panel.start_button.click()
        self.assertTrue(wait_for(lambda: (self.controller.snapshot() or SimpleNamespace(status="")).status
                                 == MissionStatus.COMPLETED))
        pump(10)
        mission = self.controller.snapshot()
        self.assertEqual((mission.linked_mission_id, mission.requirements), (42, ["Under $130", "Ships to Canada"]))
        self.assertTrue(self.panel.mission_button.isVisibleTo(self.panel))
        self.assertEqual(saved, [])                                    # never automatic
        self.panel.mission_button.click()
        self.assertEqual(saved[0][0], 42)
        self.assertIn("Buy A", saved[0][1])
        self.assertIn("Saved to the Mission", self.panel.saved_note.text())

    def test_a_run_not_started_from_a_mission_offers_no_save_button(self):
        self.build()
        self.start_mission()
        self.assertTrue(wait_for(lambda: (self.controller.snapshot() or SimpleNamespace(status="")).status
                                 == MissionStatus.COMPLETED))
        pump(10)
        self.assertFalse(self.panel.mission_button.isVisibleTo(self.panel))
        self.assertIn("not started from a Mission", self.controller.save_to_linked_mission())

    def test_a_failing_sink_is_reported_not_swallowed(self):
        self.build(browser=FakeBrowser([]))
        self.controller.mission_sink = lambda mid, text: "The Mission this run came from no longer exists."
        self.offer()
        self.panel.start_button.click()
        wait_for(lambda: (self.controller.snapshot() or SimpleNamespace(status="")).status == MissionStatus.COMPLETED)
        pump(10)
        self.assertIn("no longer exists", self.controller.save_to_linked_mission())


if __name__ == "__main__":
    unittest.main()


class InTheRealWindow(unittest.TestCase):
    """Mission -> Team -> Mission through MainWindow's own wiring and MissionStore."""

    def setUp(self):
        import tempfile
        from unittest import mock
        from PySide6.QtWidgets import QApplication
        from app.storage import Database
        from app.ui.main_window import MainWindow
        from tests.qt_profile import shared_profile
        self._env = mock.patch.dict(os.environ, {"PYBROWSER_AI_DEFAULT": "1"})
        self._env.start()
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.window = MainWindow(shared_profile(), self.db, start_urls=["about:blank"])
        self.app = QApplication.instance()
        pump(8)

    def tearDown(self):
        self.window.close()
        self.db.close()
        self._env.stop()
        self._dir.cleanup()

    def test_continue_in_team_hands_over_the_active_missions_goal_and_constraints(self):
        w = self.window
        w.missions.start("Pick a widget under $130")
        mid = w.missions.active.id
        w.mission_store.set_constraints(mid, ["Under $130"])
        w.mission_store.add_finding(mid, "Widget A is cheap")
        w._continue_mission_in_team()
        pump(10)
        self.assertEqual(w.team._handoff, None)                 # consumed by the panel
        panel = w._side_panel.team_panel
        self.assertEqual(panel.goal.toPlainText(), "Pick a widget under $130")
        self.assertEqual(panel.requirements.toPlainText(), "Under $130")
        self.assertEqual(panel._linked_mission_id, mid)

    def test_the_sink_writes_the_mission_result_and_refuses_a_missing_mission(self):
        w = self.window
        w.missions.start("Pick a widget")
        mid = w.missions.active.id
        self.assertIsNone(w._save_team_summary_to_mission(mid, "Recommendation: A."))
        self.assertEqual(w.mission_store.get(mid).result, "Recommendation: A.")
        self.assertIn("no longer exists", w._save_team_summary_to_mission(99999, "x"))
        too_long = "x" * 5000
        self.assertIn("could not be saved", w._save_team_summary_to_mission(mid, too_long))

    def test_with_no_active_mission_it_explains_instead_of_opening_an_empty_run(self):
        from unittest import mock
        with mock.patch("app.ui.main_window.QMessageBox.information") as info:
            self.window._continue_mission_in_team()
        self.assertTrue(info.called)
