"""Pinned tabs: one flag on the shared tab model, not a second store.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_pinned_tabs -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-pinned-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.browser.tab_manager import TabManager  # noqa: E402
from app.config import database_path  # noqa: E402
from app.storage import Database, SettingsStore  # noqa: E402
from app.storage.settings import TAB_LAYOUT_HORIZONTAL, TAB_LAYOUT_VERTICAL  # noqa: E402
from app.ui.vertical_tabs import VerticalTabList  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_profile = None


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _profile = shared_profile()


def pump(times: int = 5) -> None:
    for _ in range(times):
        _app.processEvents()


class TabManagerPinningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tabs = TabManager(_profile, "about:blank")
        self.tabs.resize(900, 500)
        self.tabs.show()
        for i in range(4):
            self.tabs.new_tab("about:blank")
        pump()

    def tearDown(self) -> None:
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        pump(3)

    def test_a_fresh_tab_is_not_pinned(self) -> None:
        self.assertFalse(self.tabs.is_pinned(0))

    def test_pinning_sets_the_flag(self) -> None:
        widget = self.tabs.widget(2)
        self.assertTrue(self.tabs.set_pinned(2, True))
        self.assertTrue(self.tabs.is_pinned(self.tabs.indexOf(widget)))

    def test_pinning_an_already_pinned_tab_is_a_no_op(self) -> None:
        self.tabs.set_pinned(0, True)
        self.assertFalse(self.tabs.set_pinned(0, True))

    def test_unpinning_clears_the_flag(self) -> None:
        self.tabs.set_pinned(1, True)
        self.tabs.set_pinned(1, False)
        self.assertFalse(self.tabs.is_pinned(1))

    def test_pinning_moves_the_tab_to_the_front(self) -> None:
        # Pin the tab that was originally last.
        widget = self.tabs.widget(3)
        self.tabs.set_pinned(3, True)
        self.assertEqual(self.tabs.indexOf(widget), 0)

    def test_pinned_tabs_stay_ahead_of_unpinned_ones(self) -> None:
        self.tabs.set_pinned(1, True)
        self.tabs.set_pinned(3, True)
        pinned_positions = [i for i in range(self.tabs.count()) if self.tabs.is_pinned(i)]
        unpinned_positions = [i for i in range(self.tabs.count()) if not self.tabs.is_pinned(i)]
        self.assertEqual(pinned_positions, [0, 1])
        self.assertEqual(unpinned_positions, [2, 3])

    def test_unpinning_a_tab_not_at_the_pin_boundary_still_keeps_order_correct(self) -> None:
        widget_a = self.tabs.widget(0)
        widget_b = self.tabs.widget(1)
        self.tabs.set_pinned(self.tabs.indexOf(widget_a), True)
        self.tabs.set_pinned(self.tabs.indexOf(widget_b), True)
        # Unpin the first of the two pinned tabs - it must not end up ahead
        # of the tab that is still pinned.
        self.tabs.set_pinned(self.tabs.indexOf(widget_a), False)
        self.assertFalse(self.tabs.is_pinned(self.tabs.indexOf(widget_a)))
        self.assertTrue(self.tabs.is_pinned(self.tabs.indexOf(widget_b)))
        self.assertLess(self.tabs.indexOf(widget_b), self.tabs.indexOf(widget_a))

    def test_pinned_urls_reflects_pin_order(self) -> None:
        widget_a = self.tabs.widget(0)
        widget_b = self.tabs.widget(1)
        widget_a.navigate("http://127.0.0.1:1/a")
        widget_b.navigate("http://127.0.0.1:1/b")
        self.tabs.set_pinned(self.tabs.indexOf(widget_b), True)
        self.tabs.set_pinned(self.tabs.indexOf(widget_a), True)
        self.assertEqual(self.tabs.pinned_urls(),
                         ["http://127.0.0.1:1/b", "http://127.0.0.1:1/a"])

    def test_dragging_an_unpinned_tab_ahead_of_a_pinned_one_is_corrected(self) -> None:
        self.tabs.set_pinned(0, True)
        # Simulate the user dragging tab 1 (unpinned) to position 0 by hand -
        # the same low-level signal a real drag emits.
        self.tabs.tabBar().moveTab(1, 0)
        pinned_positions = [i for i in range(self.tabs.count()) if self.tabs.is_pinned(i)]
        self.assertEqual(pinned_positions, [0])

    # -- close protection -------------------------------------------------
    def test_pinning_hides_the_horizontal_close_button(self) -> None:
        self.tabs.set_pinned(0, True)
        self.assertIsNone(self.tabs.tabBar().tabButton(0, self.tabs.tabBar().ButtonPosition.RightSide))

    def test_unpinning_restores_the_close_button(self) -> None:
        self.tabs.set_pinned(0, True)
        self.tabs.set_pinned(0, False)
        self.assertIsNotNone(
            self.tabs.tabBar().tabButton(0, self.tabs.tabBar().ButtonPosition.RightSide))

    def test_a_pinned_tab_can_still_be_closed_explicitly(self) -> None:
        self.tabs.set_pinned(0, True)
        before = self.tabs.count()
        self.tabs.close_tab(0)
        self.assertEqual(self.tabs.count(), before - 1)

    def test_pinned_tab_text_is_blank_icon_only(self) -> None:
        self.tabs.tabBar().setTabToolTip(0, "Some Title")
        self.tabs.set_pinned(0, True)
        self.assertEqual(self.tabs.tabText(0), "")
        # But the real title survives in the tooltip.
        self.assertIn("Some Title", self.tabs.tabToolTip(0))


class VerticalPinnedTabTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.settings = SettingsStore(self.db)
        self.tabs = TabManager(_profile, "about:blank")
        self.tabs.resize(900, 500)
        self.tabs.show()
        for i in range(3):
            self.tabs.new_tab("about:blank")
        pump()

    def tearDown(self) -> None:
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        self.db.close()
        self._dir.cleanup()
        pump(3)

    def test_pinned_row_hides_title_and_close_even_when_expanded(self) -> None:
        self.tabs.set_pinned(0, True)
        sidebar = VerticalTabList(self.tabs, self.settings)
        row = sidebar._rows[0]
        self.assertFalse(row._title.isVisible() and not row._title.isHidden())
        self.assertTrue(row._title.isHidden())
        self.assertTrue(row._close.isHidden())

    def test_collapsed_sidebar_shows_pinned_favicons(self) -> None:
        self.tabs.set_pinned(0, True)
        sidebar = VerticalTabList(self.tabs, self.settings)
        sidebar.toggle_collapsed()
        row = sidebar._rows[0]
        self.assertFalse(row._icon.isHidden())

    def test_pinning_triggers_a_rebuild_reordering_rows(self) -> None:
        sidebar = VerticalTabList(self.tabs, self.settings)
        self.tabs.set_pinned(2, True)
        pump()
        # The row for what is now index 0 (the tab that got pinned) must
        # exist and reflect pinned state.
        self.assertTrue(sidebar._rows[0]._pinned)


class PinnedTabSettingsPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.settings = SettingsStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def test_defaults_to_empty(self) -> None:
        self.assertEqual(self.settings.pinned_tab_urls, [])

    def test_round_trips(self) -> None:
        self.settings.pinned_tab_urls = ["https://a.example/", "https://b.example/"]
        self.assertEqual(self.settings.pinned_tab_urls,
                         ["https://a.example/", "https://b.example/"])

    def test_persists_across_reopening(self) -> None:
        path = self.db.path
        self.settings.pinned_tab_urls = ["https://a.example/"]
        self.db.close()
        reopened = Database(path)
        self.assertEqual(SettingsStore(reopened).pinned_tab_urls, ["https://a.example/"])
        reopened.close()

    def test_corrupted_value_degrades_to_empty(self) -> None:
        self.settings.set("pinned_tabs", "not json")
        self.assertEqual(self.settings.pinned_tab_urls, [])

    def test_non_string_entries_are_dropped(self) -> None:
        import json
        self.settings.set("pinned_tabs", json.dumps(["https://a.example/", 5, None]))
        self.assertEqual(self.settings.pinned_tab_urls, ["https://a.example/"])


class MainWindowPinnedTabIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        os.environ["PYBROWSER_DATA_DIR"] = self._dir.name
        self.db = Database(database_path())
        self.settings = SettingsStore(self.db)

    def tearDown(self) -> None:
        if getattr(self, "window", None) is not None:
            self.window.close()
            self.window.deleteLater()
        self.db.close()
        self._dir.cleanup()
        pump(3)

    def _open(self, layout=TAB_LAYOUT_HORIZONTAL):
        from app.ui.main_window import MainWindow

        self.settings.tab_layout = layout
        self.window = MainWindow(_profile, self.db, start_urls=["about:blank"])
        self.window.resize(1000, 700)
        pump()
        return self.window

    def test_pinning_persists_immediately(self) -> None:
        window = self._open()
        window.tabs.widget(0).navigate("http://127.0.0.1:1/pinned")
        pump()
        window.tabs.set_pinned(0, True)
        pump()
        self.assertEqual(self.settings.pinned_tab_urls, ["http://127.0.0.1:1/pinned"])

    def test_closing_a_pinned_tab_removes_it_from_the_saved_list(self) -> None:
        window = self._open()
        window.tabs.widget(0).navigate("http://127.0.0.1:1/pinned")
        pump()
        window.tabs.set_pinned(0, True)
        pump()
        window.tabs.new_tab("about:blank")
        window.tabs.close_tab(0)
        pump(5)
        self.assertEqual(self.settings.pinned_tab_urls, [])

    def test_session_restore_recreates_pinned_tabs_in_order(self) -> None:
        window = self._open()
        window.tabs.widget(0).navigate("http://127.0.0.1:1/first")
        pump()
        window.tabs.new_tab("http://127.0.0.1:1/second")
        pump()
        window.tabs.set_pinned(1, True)
        window.tabs.set_pinned(0, True)
        pump()
        expected = self.settings.pinned_tab_urls
        window.close()
        pump(3)
        self.window = None

        from app.ui.main_window import MainWindow
        window2 = MainWindow(_profile, self.db, start_urls=["about:blank"])
        pump()
        self.window = window2
        restored = [window2.tabs.widget(i).url().toString()
                   for i in range(window2.tabs.count()) if window2.tabs.is_pinned(i)]
        self.assertEqual(restored, expected)
        # Pinned tabs must be at the very front, ahead of the ordinary
        # start-up tab.
        self.assertTrue(all(window2.tabs.is_pinned(i) for i in range(len(expected))))

    def test_switching_to_vertical_and_back_does_not_lose_pinned_state(self) -> None:
        window = self._open(TAB_LAYOUT_HORIZONTAL)
        window.tabs.set_pinned(0, True)
        pump()
        self.settings.tab_layout = TAB_LAYOUT_VERTICAL
        window._apply_tab_layout()
        pump()
        self.assertTrue(window.tabs.is_pinned(0))
        self.settings.tab_layout = TAB_LAYOUT_HORIZONTAL
        window._apply_tab_layout()
        pump()
        self.assertTrue(window.tabs.is_pinned(0))


if __name__ == "__main__":
    unittest.main()
