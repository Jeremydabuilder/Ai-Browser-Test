"""MissionCard: the active-mission summary at the top of the AI panel.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mission_card -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.missions.model import Mission, MissionStatus  # noqa: E402
from app.ui.missions.mission_card import MissionCard  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class _FakeService:
    def open_keys(self):
        return set()


class ProgressLineTests(unittest.TestCase):
    """'Currently doing' is distinct from the goal (why) and status (what
    stage) - see the comment on Mission.progress in app/missions/model.py."""

    def setUp(self) -> None:
        self.card = MissionCard(_FakeService())

    def tearDown(self) -> None:
        self.card.deleteLater()
        _app.processEvents()

    def _mission(self, **overrides) -> Mission:
        base = dict(id=1, title="Find shoes", goal="find running shoes",
                   status=MissionStatus.ACTIVE, progress="")
        base.update(overrides)
        return Mission(**base)

    def test_an_active_missions_progress_is_shown(self) -> None:
        self.card.show_mission(self._mission(progress="Reviewing 8 sources"))
        self.assertTrue(self.card.progress_line.isVisible())
        self.assertIn("Reviewing 8 sources", self.card.progress_line.text())

    def test_no_progress_label_shows_nothing(self) -> None:
        self.card.show_mission(self._mission(progress=""))
        self.assertFalse(self.card.progress_line.isVisible())

    def test_a_paused_missions_progress_is_not_shown_as_current(self) -> None:
        # "Reviewing 8 sources" would be stale and misleading once nothing is
        # actually happening - only an ACTIVE mission has a *current* action.
        self.card.show_mission(
            self._mission(status=MissionStatus.PAUSED, progress="Reviewing 8 sources"))
        self.assertFalse(self.card.progress_line.isVisible())

    def test_a_completed_missions_progress_is_not_shown_as_current(self) -> None:
        self.card.show_mission(
            self._mission(status=MissionStatus.COMPLETED, progress="Reviewing 8 sources"))
        self.assertFalse(self.card.progress_line.isVisible())

    def test_switching_from_a_mission_with_progress_to_one_without_clears_it(self) -> None:
        self.card.show_mission(self._mission(progress="Comparing options"))
        self.assertTrue(self.card.progress_line.isVisible())
        self.card.show_mission(self._mission(id=2, progress=""))
        self.assertFalse(self.card.progress_line.isVisible())


class ResultLineTests(unittest.TestCase):
    """A pure research/comparison mission has a result but no decision - see
    the comment on Mission.result in app/missions/model.py. The card must
    still say the mission is done, not show nothing at all."""

    def setUp(self) -> None:
        self.card = MissionCard(_FakeService())

    def tearDown(self) -> None:
        self.card.deleteLater()
        _app.processEvents()

    def _mission(self, **overrides) -> Mission:
        base = dict(id=1, title="Research tidal power", goal="compare tidal power sources",
                   status=MissionStatus.COMPLETED, result="")
        base.update(overrides)
        return Mission(**base)

    def test_a_result_with_no_decision_is_shown(self) -> None:
        self.card.show_mission(self._mission(result="Tidal stream generators are more "
                                                     "cost-effective than barrages."))
        self.assertTrue(self.card.result_line.isVisible())
        self.assertIn("Tidal stream generators", self.card.result_line.text())

    def test_no_result_shows_nothing(self) -> None:
        self.card.show_mission(self._mission(result=""))
        self.assertFalse(self.card.result_line.isVisible())

    def test_a_long_result_is_shortened(self) -> None:
        self.card.show_mission(self._mission(result="x" * 300))
        self.assertLess(len(self.card.result_line.text()), 300)

    def test_a_decision_takes_precedence_over_the_bare_result_line(self) -> None:
        from app.missions.model import MissionDecision

        decision = MissionDecision(id=1, mission_id=1, decision="Bose QuietComfort Ultra",
                                   rationale="Best noise cancellation for the price.")
        self.card.show_mission(self._mission(result="Some findings.", decision=decision))
        self.assertFalse(self.card.result_line.isVisible())
        self.assertTrue(self.card.decision.isVisible())


if __name__ == "__main__":
    unittest.main()
