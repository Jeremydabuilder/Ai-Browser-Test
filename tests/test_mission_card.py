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

from app.missions.model import Mission, MissionQuestion, MissionStatus, QuestionStatus  # noqa: E402
from app.ui.missions.mission_card import MissionCard, VISIBLE_QUESTIONS  # noqa: E402

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


class QuestionsSectionTests(unittest.TestCase):
    """Only OPEN questions show, and only when there is at least one - see
    the docstring on MissionCard._render_questions."""

    def setUp(self) -> None:
        self.card = MissionCard(_FakeService())

    def tearDown(self) -> None:
        self.card.deleteLater()
        _app.processEvents()

    def _question(self, id_, status=QuestionStatus.OPEN, text="Is X better?"):
        return MissionQuestion(id=id_, mission_id=1, text=text, status=status)

    def _mission(self, **overrides):
        base = dict(id=1, title="Research", goal="research something",
                   status=MissionStatus.ACTIVE, questions=())
        base.update(overrides)
        return Mission(**base)

    def test_no_open_questions_hides_the_whole_section(self) -> None:
        self.card.show_mission(self._mission(questions=()))
        self.assertFalse(self.card.questions_label.isVisible())

    def test_an_open_question_shows_the_section_with_its_text(self) -> None:
        self.card.show_mission(self._mission(questions=(self._question(1),)))
        self.assertTrue(self.card.questions_label.isVisible())
        self.assertIn("Is X better?", self._questions_text())

    def _questions_text(self) -> str:
        texts = []
        for i in range(self.card._questions_box.count()):
            widget = self.card._questions_box.itemAt(i).widget()
            if widget is not None:
                texts.append(widget.text())
        return " ".join(texts)

    def test_an_answered_question_does_not_appear(self) -> None:
        self.card.show_mission(self._mission(
            questions=(self._question(1, status=QuestionStatus.ANSWERED),)))
        self.assertFalse(self.card.questions_label.isVisible())

    def test_a_mix_of_open_and_answered_counts_only_the_open_ones(self) -> None:
        self.card.show_mission(self._mission(questions=(
            self._question(1, status=QuestionStatus.OPEN, text="Open one?"),
            self._question(2, status=QuestionStatus.ANSWERED, text="Answered one?"),
        )))
        self.assertIn("1", self.card.questions_label.text())
        self.assertIn("Open one?", self._questions_text())
        self.assertNotIn("Answered one?", self._questions_text())

    def test_more_than_the_visible_limit_shows_a_count_of_the_rest(self) -> None:
        many = tuple(self._question(i, text=f"Question {i}?")
                    for i in range(VISIBLE_QUESTIONS + 2))
        self.card.show_mission(self._mission(questions=many))
        self.assertIn("2 more", self._questions_text())

    def test_switching_to_a_mission_with_no_questions_clears_the_section(self) -> None:
        self.card.show_mission(self._mission(id=1, questions=(self._question(1),)))
        self.assertTrue(self.card.questions_label.isVisible())
        self.card.show_mission(self._mission(id=2, questions=()))
        self.assertFalse(self.card.questions_label.isVisible())


if __name__ == "__main__":
    unittest.main()
