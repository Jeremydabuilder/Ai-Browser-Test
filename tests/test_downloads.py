"""Downloads: the real engine fetching real files from the fixture server.

Nothing here is simulated. The browser follows a link with a
Content-Disposition header, Chromium starts a download, and the assertions are
about the file that ends up on disk and the state the manager reports.

The unsized case matters as much as the sized one: a server need not send
Content-Length, and the honest answer then is "we do not know", not a
percentage.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_downloads -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-dl-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.browser.downloads import DownloadItem, DownloadManager, human_size  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from tests.fixture_server import FixtureServer  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_server: FixtureServer | None = None
_profile = None


def setUpModule() -> None:
    global _app, _server, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _server = FixtureServer()
    _profile = shared_profile()


def tearDownModule() -> None:
    if _server is not None:
        _server.stop()
    if _app is not None:
        for _ in range(3):
            _app.processEvents()


def pump(predicate, timeout_ms: int = 20000) -> bool:
    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(timeout_ms)
    while not predicate() and not expired[0]:
        _app.processEvents()
    timer.stop()
    return predicate()


class ReportingTests(unittest.TestCase):
    """The parts that are pure arithmetic, and must not lie."""

    def test_human_size(self) -> None:
        self.assertEqual(human_size(512), "512 B")
        self.assertEqual(human_size(2048), "2 KB")
        self.assertEqual(human_size(5 * 1024 * 1024), "5 MB")

    def test_percent_is_none_when_the_total_is_unknown(self) -> None:
        item = DownloadItem(1, "f", "/d", "http://x", state="in_progress", received=900)
        self.assertIsNone(item.percent)
        # And the description must not imply a proportion it cannot know.
        self.assertNotIn("%", item.describe())
        self.assertIn("900 B", item.describe())

    def test_percent_when_the_total_is_known(self) -> None:
        item = DownloadItem(1, "f", "/d", "http://x", state="in_progress",
                            received=50, total=200)
        self.assertEqual(item.percent, 25)
        self.assertIn("25%", item.describe())

    def test_percent_never_exceeds_one_hundred(self) -> None:
        item = DownloadItem(1, "f", "/d", "http://x", received=300, total=200)
        self.assertEqual(item.percent, 100)

    def test_finished_states(self) -> None:
        for state, finished in (("requested", False), ("in_progress", False),
                                ("completed", True), ("cancelled", True),
                                ("interrupted", True)):
            self.assertEqual(
                DownloadItem(1, "f", "/d", "u", state=state).finished, finished, state)

    def test_a_failure_reports_the_reason(self) -> None:
        item = DownloadItem(1, "f", "/d", "u", state="interrupted", reason="No space")
        self.assertIn("No space", item.describe())

    def test_the_manager_starts_empty(self) -> None:
        manager = DownloadManager()
        self.assertEqual(manager.items(), [])
        self.assertEqual(manager.active_count(), 0)
        self.assertFalse(manager.cancel(99))       # unknown id, no exception


class RealDownloadTests(unittest.TestCase):
    """A real file, fetched by Chromium, landing on disk."""

    def setUp(self) -> None:
        self.directory = tempfile.mkdtemp(prefix="pybrowser-dl-")
        self.manager = _profile.downloads
        self.manager.clear_finished()
        self.tabs = TabManager(_profile, _server.base)
        self.tabs.resize(900, 700)
        self.tabs.show()
        self.tab = self.tabs.new_tab(_server.url("/downloads-page"))
        loaded = []
        self.tab.load_finished.connect(loaded.append)
        self.assertTrue(pump(lambda: loaded), "fixture page did not load")

        # Downloads land in the real Downloads directory by default; point them
        # at a temporary one so the test never writes to the user's home.
        self._original_accept = self.manager.accept
        directory = self.directory
        self.manager.accept = lambda request, _dir: self._original_accept(
            request, directory)

    def tearDown(self) -> None:
        # Cancel anything still running, so one slow test cannot make the next
        # one fail on a count it did not cause.
        for item in self.manager.items():
            if not item.finished:
                self.manager.cancel(item.id)
        self.manager.clear_finished()
        self.manager.accept = self._original_accept
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        _app.processEvents()

    def _download(self, element_id: str) -> DownloadItem:
        finished = []
        self.manager.finished.connect(finished.append)
        self.tab.run_javascript(f"document.getElementById('{element_id}').click();")
        self.assertTrue(pump(lambda: finished), "the download never finished")
        return finished[-1]

    def test_a_download_completes_and_the_file_is_on_disk(self) -> None:
        item = self._download("file")
        self.assertEqual(item.state, "completed")
        self.assertEqual(item.file_name, "fixture.bin")
        path = os.path.join(item.directory, item.file_name)
        self.assertTrue(os.path.exists(path), f"{path} was not written")
        self.assertEqual(os.path.getsize(path), item.received)
        self.assertGreater(item.received, 0)

    def test_a_completed_download_reports_a_real_size(self) -> None:
        item = self._download("file")
        self.assertEqual(item.percent, 100)
        self.assertIn("Completed", item.describe())

    def test_a_download_without_a_content_length_still_completes(self) -> None:
        item = self._download("unsized")
        self.assertEqual(item.state, "completed")
        self.assertTrue(os.path.exists(
            os.path.join(item.directory, item.file_name)))

    def test_the_manager_lists_the_download(self) -> None:
        item = self._download("file")
        listed = [row.id for row in self.manager.items()]
        self.assertIn(item.id, listed)
        self.assertEqual(self.manager.get(item.id).state, "completed")

    def test_a_second_download_does_not_overwrite_the_first(self) -> None:
        first = self._download("file")
        second = self._download("file")
        self.assertNotEqual(first.id, second.id)
        # Qt de-duplicates the name itself, which is what stops a download
        # silently replacing a file already in the folder.
        self.assertNotEqual(first.file_name, second.file_name)
        self.assertTrue(os.path.exists(os.path.join(second.directory, second.file_name)))

    def test_clear_finished_empties_the_list(self) -> None:
        self._download("file")
        self.manager.clear_finished()
        self.assertEqual(self.manager.items(), [])

    def test_active_count_returns_to_zero(self) -> None:
        self._download("file")
        self.assertEqual(self.manager.active_count(), 0)


if __name__ == "__main__":
    unittest.main()
