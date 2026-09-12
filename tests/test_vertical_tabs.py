"""Vertical tab sidebar: the shared-model view, its settings, and the
main-window wiring that switches between horizontal and vertical layouts.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_vertical_tabs -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-vtabs-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.browser.tab_manager import TabManager  # noqa: E402
from app.config import database_path  # noqa: E402
from app.storage import Database, SettingsStore  # noqa: E402
from app.storage.settings import (  # noqa: E402
    TAB_LAYOUT_HORIZONTAL,
    TAB_LAYOUT_VERTICAL,
    VERTICAL_TABS_COLLAPSED_WIDTH,
    VERTICAL_TABS_MAX_WIDTH,
    VERTICAL_TABS_MIN_WIDTH,
)
from app.ui.vertical_tabs import VerticalTabList  # noqa: E402
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


class _VerticalTabListTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.settings = SettingsStore(self.db)
        self.tabs = TabManager(_profile, "about:blank")
        self.tabs.resize(900, 500)
        self.tabs.show()
        pump()

    def tearDown(self) -> None:
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        self.db.close()
        self._dir.cleanup()
        pump(3)


class SidebarReflectsTheSharedModelTests(_VerticalTabListTestCase):
    """VerticalTabList never keeps its own tab list - it is only ever a
    reactive read of TabManager, exactly like the tab bar it substitutes
    for."""

    def test_starts_with_one_row_per_existing_tab(self) -> None:
        self.tabs.new_tab("about:blank")
        self.tabs.new_tab("about:blank")
        pump()
        sidebar = VerticalTabList(self.tabs, self.settings)
        self.assertEqual(len(sidebar._rows), self.tabs.count())

    def test_opening_a_tab_adds_a_row(self) -> None:
        sidebar = VerticalTabList(self.tabs, self.settings)
        before = len(sidebar._rows)
        self.tabs.new_tab("about:blank")
        pump()
        self.assertEqual(len(sidebar._rows), before + 1)

    def test_closing_a_tab_removes_its_row(self) -> None:
        self.tabs.new_tab("about:blank")
        pump()
        sidebar = VerticalTabList(self.tabs, self.settings)
        before = len(sidebar._rows)
        self.tabs.close_tab(0)
        pump()
        self.assertEqual(len(sidebar._rows), before - 1)

    def test_activating_a_row_switches_the_tab(self) -> None:
        self.tabs.new_tab("about:blank")
        pump()
        sidebar = VerticalTabList(self.tabs, self.settings)
        sidebar._rows[0].activated.emit()
        self.assertEqual(self.tabs.currentIndex(), 0)

    def test_close_button_closes_the_tab(self) -> None:
        self.tabs.new_tab("about:blank")
        pump()
        sidebar = VerticalTabList(self.tabs, self.settings)
        before = self.tabs.count()
        sidebar._rows[0].close_requested.emit()
        self.assertEqual(self.tabs.count(), before - 1)

    def test_title_and_icon_changes_update_the_row_without_a_rebuild(self) -> None:
        self.tabs.new_tab("about:blank")
        pump()
        sidebar = VerticalTabList(self.tabs, self.settings)
        row = sidebar._rows[0]
        # The tooltip, not tabText, is the row's source of truth - a pinned
        # tab's tabText is blank on purpose (icon-only), so the sidebar must
        # read the real title from somewhere that stays true either way.
        self.tabs.tabBar().setTabToolTip(0, "A New Title")
        self.tabs.tab_updated.emit(0)
        pump()
        self.assertEqual(row._title.text(), "A New Title")

    def test_switching_current_tab_updates_active_highlight(self) -> None:
        self.tabs.new_tab("about:blank")
        self.tabs.new_tab("about:blank")
        pump()
        self.tabs.setCurrentIndex(0)
        sidebar = VerticalTabList(self.tabs, self.settings)
        self.tabs.setCurrentIndex(1)
        pump()
        self.assertFalse(sidebar._rows[0]._active)
        self.assertTrue(sidebar._rows[1]._active)


class CollapseAndResizeTests(_VerticalTabListTestCase):
    def test_defaults_to_expanded(self) -> None:
        sidebar = VerticalTabList(self.tabs, self.settings)
        self.assertFalse(sidebar.collapsed)

    def test_toggle_collapses_and_hides_title_and_close(self) -> None:
        sidebar = VerticalTabList(self.tabs, self.settings)
        sidebar.toggle_collapsed()
        self.assertTrue(sidebar.collapsed)
        self.assertFalse(sidebar.new_tab_label.isVisible())
        for row in sidebar._rows:
            self.assertFalse(row._title.isVisible())
            self.assertFalse(row._close.isVisible())

    def test_collapsed_state_persists_to_settings(self) -> None:
        sidebar = VerticalTabList(self.tabs, self.settings)
        sidebar.toggle_collapsed()
        self.assertTrue(self.settings.vertical_tabs_collapsed)
        sidebar.toggle_collapsed()
        self.assertFalse(self.settings.vertical_tabs_collapsed)

    def test_a_fresh_sidebar_picks_up_the_saved_collapsed_state(self) -> None:
        self.settings.vertical_tabs_collapsed = True
        sidebar = VerticalTabList(self.tabs, self.settings)
        self.assertTrue(sidebar.collapsed)

    def test_collapsed_width_is_the_fixed_icon_only_width(self) -> None:
        sidebar = VerticalTabList(self.tabs, self.settings)
        sidebar.toggle_collapsed()
        self.assertEqual(sidebar.maximumWidth(), VERTICAL_TABS_COLLAPSED_WIDTH)
        self.assertEqual(sidebar.minimumWidth(), VERTICAL_TABS_COLLAPSED_WIDTH)

    def test_expanded_width_is_resizable_not_fixed(self) -> None:
        """A fixed width would silently defeat splitter dragging - the
        expanded sidebar must offer a real min/max range instead."""
        sidebar = VerticalTabList(self.tabs, self.settings)
        self.assertEqual(sidebar.minimumWidth(), VERTICAL_TABS_MIN_WIDTH)
        self.assertEqual(sidebar.maximumWidth(), VERTICAL_TABS_MAX_WIDTH)

    def test_set_expanded_width_persists_when_not_collapsed(self) -> None:
        sidebar = VerticalTabList(self.tabs, self.settings)
        sidebar.set_expanded_width(300)
        self.assertEqual(self.settings.vertical_tabs_width, 300)

    def test_set_expanded_width_is_clamped(self) -> None:
        sidebar = VerticalTabList(self.tabs, self.settings)
        sidebar.set_expanded_width(10)
        self.assertEqual(self.settings.vertical_tabs_width, VERTICAL_TABS_MIN_WIDTH)
        sidebar.set_expanded_width(10000)
        self.assertEqual(self.settings.vertical_tabs_width, VERTICAL_TABS_MAX_WIDTH)

    def test_set_expanded_width_is_ignored_while_collapsed(self) -> None:
        """Dragging while collapsed cannot happen through the UI (there is
        nothing to drag), but a saved preference must never be overwritten
        by a stray call while collapsed."""
        sidebar = VerticalTabList(self.tabs, self.settings)
        self.settings.vertical_tabs_width = 240
        sidebar.toggle_collapsed()
        sidebar.set_expanded_width(300)
        self.assertEqual(self.settings.vertical_tabs_width, 240)


class MainWindowIntegrationTests(unittest.TestCase):
    """MainWindow wiring: one tab model, a splitter that can host either
    view, and layout switching that keeps every tab alive."""

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

    def _open(self, layout: str = TAB_LAYOUT_HORIZONTAL):
        from app.ui.main_window import MainWindow

        self.settings.tab_layout = layout
        self.window = MainWindow(_profile, self.db, start_urls=["about:blank"])
        self.window.resize(1280, 800)
        self.window.show()
        pump()
        return self.window

    def test_horizontal_is_unchanged_by_default(self) -> None:
        window = self._open(TAB_LAYOUT_HORIZONTAL)
        self.assertIsNone(window._vertical_tabs)
        self.assertTrue(window.tabs.tabBar().isVisibleTo(window))

    def test_vertical_mode_creates_the_sidebar_and_hides_the_tab_bar(self) -> None:
        window = self._open(TAB_LAYOUT_VERTICAL)
        self.assertIsNotNone(window._vertical_tabs)
        self.assertFalse(window.tabs.tabBar().isVisible())

    def test_switching_to_vertical_preserves_open_tabs(self) -> None:
        window = self._open(TAB_LAYOUT_HORIZONTAL)
        window.tabs.new_tab("about:blank")
        window.tabs.new_tab("about:blank")
        pump()
        self.settings.tab_layout = TAB_LAYOUT_VERTICAL
        window._apply_tab_layout()
        pump()
        self.assertEqual(window.tabs.count(), 3)
        self.assertEqual(len(window._vertical_tabs._rows), 3)

    def test_switching_back_to_horizontal_preserves_open_tabs(self) -> None:
        window = self._open(TAB_LAYOUT_VERTICAL)
        window.tabs.new_tab("about:blank")
        pump()
        self.settings.tab_layout = TAB_LAYOUT_HORIZONTAL
        window._apply_tab_layout()
        pump()
        self.assertIsNone(window._vertical_tabs)
        self.assertEqual(window.tabs.count(), 2)
        self.assertTrue(window.tabs.tabBar().isVisibleTo(window))

    def test_narrow_window_auto_collapses_vertical_sidebar(self) -> None:
        window = self._open(TAB_LAYOUT_VERTICAL)
        self.assertFalse(window._vertical_tabs.collapsed)
        window.resize(600, 800)
        pump()
        self.assertTrue(window._vertical_tabs.collapsed)
        self.assertFalse(self.settings.vertical_tabs_collapsed,
                          "an automatic collapse must never overwrite the saved preference")

    def test_widening_the_window_lifts_an_automatic_collapse(self) -> None:
        window = self._open(TAB_LAYOUT_VERTICAL)
        window.resize(600, 800)
        pump()
        window.resize(1280, 800)
        pump()
        self.assertFalse(window._vertical_tabs.collapsed)

    def test_widening_the_window_does_not_lift_a_deliberate_collapse(self) -> None:
        window = self._open(TAB_LAYOUT_VERTICAL)
        window._vertical_tabs.toggle_collapsed()  # a deliberate user action
        window.resize(600, 800)
        pump()
        window.resize(1280, 800)
        pump()
        self.assertTrue(window._vertical_tabs.collapsed)

    def test_splitter_drag_persists_the_expanded_width(self) -> None:
        window = self._open(TAB_LAYOUT_VERTICAL)
        window._tabs_splitter.setSizes([300, window.width() - 300])
        window._on_tabs_splitter_moved(0, 0)
        self.assertAlmostEqual(self.settings.vertical_tabs_width, 300, delta=2)

    def test_layout_choice_persists_across_a_restart(self) -> None:
        self._open(TAB_LAYOUT_VERTICAL)
        self.window.deleteLater()
        pump(3)
        self.window = None
        reopened = SettingsStore(self.db)
        self.assertEqual(reopened.tab_layout, TAB_LAYOUT_VERTICAL)
        window2 = self._open(TAB_LAYOUT_VERTICAL)
        self.assertIsNotNone(window2._vertical_tabs)


if __name__ == "__main__":
    unittest.main()
