"""Multi-tab Ask Py: selecting several open tabs and handing them to Py to
summarize or compare, plus closing detected duplicate tabs - both wired
through MainWindow, composing tools Py already has rather than adding new
ones or a second tab store.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_multi_tab_ask_py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-multitab-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.config import database_path  # noqa: E402
from app.storage import Database  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
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


def wait(predicate, timeout_ms: int = 5000) -> bool:
    from PySide6.QtCore import QTimer
    from PySide6.QtTest import QTest

    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(timeout_ms)
    while not predicate() and not expired[0]:
        _app.processEvents()
        QTest.qWait(1)
    timer.stop()
    return predicate()


class MultiTabAskPyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.profile = _profile
        self.window = MainWindow(self.profile, self.db, start_urls=["about:blank"])
        self.window.resize(1000, 700)
        for index, name in enumerate(("First", "Second", "Third")):
            if index == 0:
                tab = self.window.tabs.current_tab()
            else:
                tab = self.window.tabs.new_tab("about:blank")
            tab.navigate(f"data:text/html,<title>{name}</title>")
            wait(lambda t=tab, n=name: t.title() == n)
        pump()
        self._asked: list[str] = []
        self.window._ask_py = self._asked.append

    def tearDown(self) -> None:
        self.window.close()
        self.db.close()
        self._dir.cleanup()
        pump(3)

    def test_asking_about_two_tabs_names_both_by_stable_tab_id(self) -> None:
        rows = self.window.controller.list_tabs()
        self.window._ask_py_about_tabs([0, 2])
        self.assertEqual(len(self._asked), 1)
        prompt = self._asked[0]
        self.assertIn(f"tab_id={rows[0]['tab_id']}", prompt)
        self.assertIn(f"tab_id={rows[2]['tab_id']}", prompt)
        self.assertIn("First", prompt)
        self.assertIn("Third", prompt)
        self.assertNotIn("Second", prompt)

    def test_asking_about_one_tab_says_summarize_not_compare(self) -> None:
        self.window._ask_py_about_tabs([1])
        self.assertIn("Summarize the following", self._asked[0])
        self.assertNotIn("compare", self._asked[0])

    def test_asking_about_several_tabs_says_summarize_and_compare(self) -> None:
        self.window._ask_py_about_tabs([0, 1, 2])
        self.assertIn("Summarize and compare", self._asked[0])

    def test_an_empty_or_stale_selection_asks_nothing(self) -> None:
        self.window._ask_py_about_tabs([])
        self.window._ask_py_about_tabs([999])
        self.assertEqual(self._asked, [])

    def test_the_prompt_tells_py_to_read_each_tab_first(self) -> None:
        self.window._ask_py_about_tabs([0, 1])
        self.assertIn("browser_get_page_text", self._asked[0])


class CloseTabsAtTests(unittest.TestCase):
    """Index math for `_close_tabs_at` - no real page loads needed, so tab
    labels are set directly rather than waiting on WebEngine title events."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.profile = _profile
        self.window = MainWindow(self.profile, self.db, start_urls=["about:blank"])
        self.window.resize(1000, 700)
        for index, name in enumerate(("First", "Second", "Third")):
            if index > 0:
                self.window.tabs.new_tab("about:blank")
            self.window.tabs.tabBar().setTabText(index, name)
        pump()

    def tearDown(self) -> None:
        self.window.close()
        self.db.close()
        self._dir.cleanup()
        pump(3)

    def test_closing_tabs_by_index_removes_exactly_those_tabs(self) -> None:
        before = self.window.tabs.count()
        self.window._close_tabs_at([0, 2])
        self.assertEqual(self.window.tabs.count(), before - 2)
        self.assertEqual(self.window.tabs.tabText(0), "Second")

    def test_closing_is_safe_against_duplicate_or_out_of_range_indices(self) -> None:
        before = self.window.tabs.count()
        self.window._close_tabs_at([1, 1, 50])
        self.assertEqual(self.window.tabs.count(), before - 1)


if __name__ == "__main__":
    unittest.main()
