"""The tab strip's furniture: the new-tab button, favicons, loading state.

The "+" has two placements - beside the last tab, and in the corner slot when
the strip runs out of room - and swapping between them changes the tab bar's
width, which is exactly the shape of thing that can oscillate. These tests pin
the behaviour down.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_tabstrip -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-tabs-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.browser.tab_manager import TabManager  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_profile = None


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _profile = shared_profile()


def pump(times: int = 8) -> None:
    for _ in range(times):
        _app.processEvents()


def wait(predicate, timeout_ms: int = 5000) -> bool:
    """Pump the event loop for real time, for things driven by a timer."""
    from PySide6.QtCore import QTimer

    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(timeout_ms)
    while not predicate() and not expired[0]:
        _app.processEvents()
    timer.stop()
    return predicate()


class NewTabButtonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tabs = TabManager(_profile, "about:blank")
        self.tabs.resize(760, 400)
        self.tabs.show()
        pump()

    def tearDown(self) -> None:
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        pump(3)

    def add(self, count: int) -> None:
        for _ in range(count):
            self.tabs.new_tab("about:blank")
        pump()

    @property
    def button(self):
        return self.tabs._new_tab_button

    def test_the_button_exists_and_is_reachable(self) -> None:
        self.assertIsNotNone(self.button, "the new-tab button was never built")
        self.assertTrue(self.button.isVisible())
        self.assertTrue(self.button.toolTip())
        self.assertTrue(self.button.accessibleName())

    def test_it_sits_just_after_the_last_tab(self) -> None:
        self.add(2)
        last = self.tabs.tabBar().tabRect(self.tabs.count() - 1)
        gap = self.button.x() - last.right()
        self.assertGreater(gap, 0, "the button overlaps the last tab")
        self.assertLess(gap, 16, f"the button is stranded {gap}px from the tabs")

    def test_it_is_vertically_centred_on_the_tabs(self) -> None:
        self.add(2)
        last = self.tabs.tabBar().tabRect(self.tabs.count() - 1)
        offset = abs((self.button.y() + self.button.height() / 2)
                     - (last.top() + last.height() / 2))
        self.assertLessEqual(offset, 2, "the button is not level with the tabs")

    def test_it_follows_the_tabs_when_one_is_added(self) -> None:
        self.add(2)
        before = self.button.x()
        self.add(1)
        self.assertGreater(self.button.x(), before,
                           "the button did not move when a tab was added")

    def test_it_follows_the_tabs_when_one_is_closed(self) -> None:
        self.add(3)
        before = self.button.x()
        self.tabs.close_tab(0)
        pump()
        self.assertLess(self.button.x(), before)

    def test_it_moves_to_the_corner_when_the_strip_fills_up(self) -> None:
        self.add(12)
        self.assertTrue(self.tabs._button_in_corner,
                        "with a full strip the button must leave the scroll arrows alone")
        self.assertTrue(self.button.isVisible(), "and must still be reachable")

    def test_it_comes_back_when_there_is_room_again(self) -> None:
        self.add(12)
        self.assertTrue(self.tabs._button_in_corner)
        while self.tabs.count() > 2:
            self.tabs.close_tab(0)
        pump()
        self.assertFalse(self.tabs._button_in_corner)
        last = self.tabs.tabBar().tabRect(self.tabs.count() - 1)
        self.assertGreater(self.button.x(), last.right())

    def test_the_placement_settles_instead_of_oscillating(self) -> None:
        """Moving into the corner narrows the bar, which could make it fit again.

        If that fed back on itself the button would flicker between the two
        placements forever, so this pumps the event loop hard at several widths
        and checks the answer stops changing.
        """
        self.add(6)
        for width in (600, 700, 820, 900, 1100):
            self.tabs.resize(width, 400)
            pump(12)
            settled = self.tabs._button_in_corner
            pump(12)
            self.assertEqual(self.tabs._button_in_corner, settled,
                             f"the button is still moving at {width}px wide")

    def test_a_narrow_window_does_not_lose_the_button(self) -> None:
        self.add(8)
        self.tabs.resize(420, 400)
        pump(12)
        self.assertTrue(self.button.isVisible())

    def test_clicking_it_opens_a_tab(self) -> None:
        before = self.tabs.count()
        self.button.click()
        pump()
        self.assertEqual(self.tabs.count(), before + 1)


class TabAppearanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tabs = TabManager(_profile, "about:blank")
        self.tabs.resize(900, 400)
        self.tabs.show()
        self.tab = self.tabs.new_tab("about:blank")
        pump()

    def tearDown(self) -> None:
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        pump(3)

    def test_every_tab_has_an_icon_from_the_moment_it_opens(self) -> None:
        # An empty icon slot means the strip jumps sideways when a favicon
        # arrives, so the slot is filled with a placeholder from the start.
        index = self.tabs.indexOf(self.tab)
        self.assertFalse(self.tabs.tabIcon(index).isNull())

    def test_a_long_title_is_shortened_rather_than_widening_the_tab(self) -> None:
        self.tabs._on_tab_title(self.tab, "A very long page title " * 6)
        label = self.tabs.tabText(self.tabs.indexOf(self.tab))
        self.assertLessEqual(len(label), 25)
        self.assertTrue(label.endswith("…"))

    def test_the_tooltip_carries_the_full_title(self) -> None:
        self.tabs._on_tab_title(self.tab, "A very long page title " * 6)
        tip = self.tabs.tabToolTip(self.tabs.indexOf(self.tab))
        self.assertIn("A very long page title", tip)
        self.assertGreater(len(tip), len(self.tabs.tabText(0)))

    def test_the_spinner_stops_once_nothing_is_loading(self) -> None:
        # A timer ticking behind an idle browser is a battery cost for nothing.
        # It stops on its next tick after the last load finishes, so this waits
        # for real time to pass rather than assuming an instant answer.
        self.assertTrue(
            wait(lambda: not any(t.is_loading for t in self.tabs.tabs())),
            "the fixture tab never finished loading")
        self.assertTrue(wait(lambda: not self.tabs._spinner.isActive()),
                        "the loading spinner kept running with nothing to spin for")


if __name__ == "__main__":
    unittest.main()
