""""Search tabs" - the small quick-filter list, not a command palette.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_tab_search -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-tabsearch-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.browser.tab_manager import TabManager  # noqa: E402
from app.ui.tab_search import TabSearchDialog  # noqa: E402
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


class TabSearchDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tabs = TabManager(_profile, "about:blank")
        self.tabs.resize(900, 500)
        self.tabs.show()
        self.tabs.new_tab("about:blank")
        self.tabs.tabBar().setTabText(0, "GitHub - PyBrowser")
        self.tabs.new_tab("about:blank")
        self.tabs.tabBar().setTabText(1, "Python.org")
        self.tabs.new_tab("about:blank")
        self.tabs.tabBar().setTabText(2, "News")
        pump()

    def tearDown(self) -> None:
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        pump(3)

    def test_lists_every_open_tab_with_no_filter(self) -> None:
        dialog = TabSearchDialog(self.tabs, None)
        self.assertEqual(dialog.list.count(), 3)

    def test_filters_by_title_substring(self) -> None:
        dialog = TabSearchDialog(self.tabs, None)
        dialog.field.setText("github")
        self.assertEqual(dialog.list.count(), 1)

    def test_filter_is_case_insensitive(self) -> None:
        dialog = TabSearchDialog(self.tabs, None)
        dialog.field.setText("PYTHON")
        self.assertEqual(dialog.list.count(), 1)

    def test_clearing_the_filter_restores_the_full_list(self) -> None:
        dialog = TabSearchDialog(self.tabs, None)
        dialog.field.setText("github")
        dialog.field.setText("")
        self.assertEqual(dialog.list.count(), 3)

    def test_no_match_shows_the_empty_state_and_hides_the_list(self) -> None:
        dialog = TabSearchDialog(self.tabs, None)
        dialog.field.setText("this matches nothing at all")
        self.assertEqual(dialog.list.count(), 0)
        self.assertFalse(dialog._empty_label.isHidden())

    def test_activating_a_result_switches_to_that_tab(self) -> None:
        dialog = TabSearchDialog(self.tabs, None)
        dialog.field.setText("python")
        dialog._activate(dialog.list.item(0))
        self.assertEqual(self.tabs.currentIndex(), 1)

    def test_arrow_keys_move_the_highlighted_row_while_typing(self) -> None:
        dialog = TabSearchDialog(self.tabs, None)
        self.assertEqual(dialog.list.currentRow(), 0)
        event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.NoModifier)
        dialog.eventFilter(dialog.field, event)
        self.assertEqual(dialog.list.currentRow(), 1)

    def test_enter_in_the_field_activates_the_highlighted_row(self) -> None:
        dialog = TabSearchDialog(self.tabs, None)
        dialog.list.setCurrentRow(2)
        event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier)
        dialog.eventFilter(dialog.field, event)
        # Row 2 in the unfiltered list is tab index 2 - that is the one that
        # should now be current.
        self.assertEqual(self.tabs.currentIndex(), 2)

    def test_new_tab_pages_have_no_domain_shown(self) -> None:
        from app.config import NEW_TAB_URL

        self.tabs.new_tab(NEW_TAB_URL)
        pump()
        dialog = TabSearchDialog(self.tabs, None)
        # PyBrowser's own new-tab page must read as "New Tab", not as the
        # raw internal pybrowser:// address it is implemented with.
        label = dialog.list.item(dialog.list.count() - 1).text()
        self.assertEqual(label, "New Tab")


class MultiTabActionsTests(unittest.TestCase):
    """The "ask Py about selected tabs" and duplicate-detection actions -
    both explicitly user-triggered, neither storing a second copy of what
    tabs exist."""

    def setUp(self) -> None:
        self.tabs = TabManager(_profile, "about:blank")
        self.tabs.resize(900, 500)
        self.tabs.show()
        self.tabs.new_tab("about:blank")
        self.tabs.tabBar().setTabText(0, "GitHub")
        self.tabs.new_tab("about:blank")
        self.tabs.tabBar().setTabText(1, "Python.org")
        pump()

    def tearDown(self) -> None:
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        pump(3)

    def test_ask_py_button_is_hidden_without_a_handler(self) -> None:
        dialog = TabSearchDialog(self.tabs, None)
        self.assertTrue(dialog.ask_py_button.isHidden())

    def test_ask_py_button_shows_when_a_handler_is_supplied(self) -> None:
        dialog = TabSearchDialog(self.tabs, None, on_ask_py=lambda indices: None)
        self.assertFalse(dialog.ask_py_button.isHidden())

    def test_ask_py_button_is_disabled_once_the_list_is_filtered_empty(self) -> None:
        dialog = TabSearchDialog(self.tabs, None, on_ask_py=lambda indices: None)
        self.assertTrue(dialog.ask_py_button.isEnabled())  # row 0 auto-selected
        dialog.field.setText("this matches nothing at all")
        self.assertFalse(dialog.ask_py_button.isEnabled())

    def test_clicking_ask_py_reports_the_selected_tab_manager_indices(self) -> None:
        received = []
        dialog = TabSearchDialog(self.tabs, None, on_ask_py=received.append)
        dialog.list.item(0).setSelected(True)
        dialog.list.item(1).setSelected(True)
        dialog._ask_py_about_selection()
        self.assertEqual(sorted(received[0]), [0, 1])

    def test_no_duplicates_hides_the_notice(self) -> None:
        dialog = TabSearchDialog(self.tabs, None, on_close_duplicates=lambda i: None)
        self.assertTrue(dialog.close_duplicates_button.isHidden())

    def test_two_tabs_on_the_same_url_are_flagged(self) -> None:
        self.tabs.new_tab("http://127.0.0.1:1/same")
        self.tabs.new_tab("http://127.0.0.1:1/same")
        pump()
        dialog = TabSearchDialog(self.tabs, None, on_close_duplicates=lambda i: None)
        self.assertFalse(dialog.close_duplicates_button.isHidden())
        self.assertEqual(len(dialog._duplicate_indices), 1)

    def test_close_duplicates_keeps_one_and_closes_the_rest(self) -> None:
        self.tabs.new_tab("http://127.0.0.1:1/same")
        self.tabs.new_tab("http://127.0.0.1:1/same")
        self.tabs.new_tab("http://127.0.0.1:1/same")
        pump()
        before = self.tabs.count()
        dialog = TabSearchDialog(self.tabs, None, on_close_duplicates=self._close)
        dialog._close_duplicates()
        self.assertEqual(self.tabs.count(), before - 2)

    def _close(self, indices: list[int]) -> None:
        for index in sorted(set(indices), reverse=True):
            self.tabs.close_tab(index)

    def test_new_tab_pages_are_never_flagged_as_duplicates(self) -> None:
        # Every extra blank tab shares the same new-tab address - that must
        # never read as "these are duplicate pages".
        self.tabs.new_tab()
        self.tabs.new_tab()
        pump()
        dialog = TabSearchDialog(self.tabs, None, on_close_duplicates=lambda i: None)
        self.assertTrue(dialog.close_duplicates_button.isHidden())


if __name__ == "__main__":
    unittest.main()
