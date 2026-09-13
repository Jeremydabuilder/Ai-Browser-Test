"""MainWindow._fetch_page_text_for_watch: the real browser-based fetch path
(one background tab, opened and closed per check) - as opposed to
tests/test_watch_runner.py, which uses a fake fetcher to test WatchRunner's
own logic in isolation.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_watch_fetch_integration -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-watch-fetch-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.storage import Database  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_profile = None


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _profile = shared_profile()


def pump(predicate, timeout_ms: int = 8000) -> bool:
    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(timeout_ms)
    while not predicate() and not expired[0]:
        _app.processEvents()
    timer.stop()
    return predicate()


class FetchIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.window = MainWindow(_profile, self.db, start_urls=["about:blank"])

    def tearDown(self) -> None:
        self.window.close()
        self.db.close()
        self._dir.cleanup()
        for _ in range(3):
            _app.processEvents()

    def test_fetching_a_page_returns_its_text_and_leaves_no_extra_tabs(self) -> None:
        before_count = len(self.window.controller.list_tabs())
        result = []
        self.window._fetch_page_text_for_watch(
            "data:text/html,<title>Watched</title><p>hello from the watched page</p>",
            lambda text: result.append(text))
        self.assertTrue(pump(lambda: bool(result)))
        self.assertIsNotNone(result[0])
        self.assertIn("hello from the watched page", result[0])
        # The background tab opened for the check is closed again afterwards.
        self.assertEqual(len(self.window.controller.list_tabs()), before_count)

    def test_the_users_current_tab_is_left_untouched_by_a_background_check(self) -> None:
        self.window.tabs.current_tab().navigate("data:text/html,<title>My Tab</title>")
        self.assertTrue(pump(lambda: self.window.tabs.current_tab().title() == "My Tab"))
        result = []
        self.window._fetch_page_text_for_watch(
            "data:text/html,<p>other content</p>", lambda text: result.append(text))
        self.assertTrue(pump(lambda: bool(result)))
        self.assertEqual(self.window.tabs.current_tab().title(), "My Tab")


if __name__ == "__main__":
    unittest.main()
