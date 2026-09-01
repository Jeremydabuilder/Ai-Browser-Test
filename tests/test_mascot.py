"""Py AI's visual presence.

The mascot is a status indicator with a personality, so what matters is that
its state always tells the truth about what the agent is doing, that it works
before any artwork exists, and that it does not sit there animating when
nothing is happening.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mascot -v
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-mascot-"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.session import AgentState  # noqa: E402
from app.ui import mascot as mascot_module  # noqa: E402
from app.ui.mascot import (  # noqa: E402
    ALL_STATES,
    Mascot,
    MascotState,
    asset_for,
    state_for_agent,
)

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class PlaceholderTests(unittest.TestCase):
    """It has to work before the character exists."""

    def test_every_state_draws_something(self) -> None:
        mascot = Mascot(40)
        for state in ALL_STATES:
            mascot.set_state(state)
            self.assertFalse(mascot.pixmap().isNull(), f"{state} drew nothing")

    def test_every_state_says_what_it_means(self) -> None:
        mascot = Mascot(40)
        for state in ALL_STATES:
            mascot.set_state(state)
            self.assertTrue(mascot.toolTip(), f"{state} has no tooltip")
            self.assertIn("Py AI", mascot.toolTip())

    def test_it_has_an_accessible_name(self) -> None:
        self.assertEqual(Mascot(40).accessibleName(), "Py AI")

    def test_an_unknown_state_is_ignored_rather_than_drawn(self) -> None:
        mascot = Mascot(40)
        mascot.set_state(MascotState.READING)
        mascot.set_state("interpretive-dance")
        self.assertEqual(mascot.state(), MascotState.READING)


class ArtworkTests(unittest.TestCase):
    """Dropping in real artwork must need no code change."""

    def setUp(self) -> None:
        self.original = mascot_module.ASSET_DIR
        self.directory = tempfile.mkdtemp(prefix="mascot-assets-")
        mascot_module.ASSET_DIR = self.directory

    def tearDown(self) -> None:
        mascot_module.ASSET_DIR = self.original
        shutil.rmtree(self.directory, ignore_errors=True)

    def _write(self, name: str) -> None:
        with open(os.path.join(self.directory, name), "w", encoding="utf-8") as handle:
            handle.write('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
                         '<rect width="10" height="10" fill="#4b46d4"/></svg>')

    def test_no_artwork_means_no_asset(self) -> None:
        self.assertIsNone(asset_for(MascotState.IDLE))

    def test_a_state_uses_its_own_file(self) -> None:
        self._write("thinking.svg")
        self.assertTrue(asset_for(MascotState.THINKING).endswith("thinking.svg"))

    def test_a_missing_state_falls_back_to_idle(self) -> None:
        # Shipping one good idle drawing should be enough to replace the
        # placeholder everywhere.
        self._write("idle.svg")
        self.assertTrue(asset_for(MascotState.WORKING).endswith("idle.svg"))

    def test_artwork_is_actually_drawn(self) -> None:
        self._write("idle.svg")
        mascot = Mascot(40)
        mascot.set_state(MascotState.WORKING)
        mascot.set_state(MascotState.IDLE)
        self.assertFalse(mascot.pixmap().isNull())

    def test_the_new_tab_page_picks_the_artwork_up(self) -> None:
        from app.browser.newtab import NewTabData, render

        self.assertNotIn("data:image", render(NewTabData()))
        self._write("idle.svg")
        self.assertIn("data:image", render(NewTabData()),
                      "the new tab page ignored the artwork")


class AgentStateMappingTests(unittest.TestCase):
    """The mascot must not claim something the agent did not do."""

    def test_each_agent_state_maps_somewhere_sensible(self) -> None:
        self.assertEqual(state_for_agent(AgentState.THINKING), MascotState.THINKING)
        self.assertEqual(state_for_agent(AgentState.ACTING), MascotState.WORKING)
        self.assertEqual(state_for_agent(AgentState.AWAITING_CONFIRMATION),
                         MascotState.APPROVAL)

    def test_idle_after_a_good_answer_celebrates(self) -> None:
        self.assertEqual(state_for_agent(AgentState.IDLE, finished_well=True),
                         MascotState.COMPLETE)

    def test_idle_after_a_failure_does_not(self) -> None:
        self.assertEqual(state_for_agent(AgentState.IDLE, finished_well=False),
                         MascotState.IDLE)

    def test_cancelling_is_not_a_celebration(self) -> None:
        self.assertEqual(state_for_agent(AgentState.CANCELLING), MascotState.IDLE)


class MotionTests(unittest.TestCase):
    def test_it_animates_only_while_there_is_something_to_animate(self) -> None:
        mascot = Mascot(40)
        self.assertFalse(mascot._timer.isActive(), "idle must not animate")
        mascot.set_state(MascotState.WORKING)
        self.assertTrue(mascot._timer.isActive())
        mascot.set_state(MascotState.IDLE)
        self.assertFalse(mascot._timer.isActive(), "the animation kept running")

    def test_complete_is_a_moment_not_a_resting_state(self) -> None:
        mascot = Mascot(40)
        mascot.set_state(MascotState.COMPLETE)
        self.assertTrue(mascot._revert.isActive(),
                        "the finished face would have stayed up forever")


if __name__ == "__main__":
    unittest.main()
