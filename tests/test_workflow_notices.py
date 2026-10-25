"""The real window's collaboration wiring (a regression: it was handed the
Mission *service* where the Mission *store* was required, so sharing a Mission
from the app would have failed on first sync even though every store-level test
passed).

Run with:  QT_QPA_PLATFORM=offscreen QTWEBENGINE_DISABLE_SANDBOX=1 python tests/run.py tests.test_collaboration_window
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-wfnotice-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.storage import Database  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app = None
_profile = None


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    theme.apply(_app)
    _profile = shared_profile()


class WindowWiring(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {"PYBROWSER_AI_DEFAULT": "1"})
        self._env.start()
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.window = MainWindow(_profile, self.db, start_urls=["about:blank"])
        for _ in range(8):
            _app.processEvents()
            time.sleep(0.003)

    def tearDown(self) -> None:
        self.window.close()
        self.db.close()
        self._env.stop()
        self._dir.cleanup()

    def test_a_stopped_automation_leaves_a_readable_notice(self) -> None:
        self.window._on_workflow_paused("tab_lost", "PyBrowser needs your help: the tab is gone.")
        self.assertIn("Automation stopped", self.window.notice._label.text())
        self.assertIn("tab is gone", self.window.notice._label.text())

    def test_skipped_optional_steps_are_reported_at_the_end(self) -> None:
        from types import SimpleNamespace
        self.window._workflow_skipped = []
        self.window._on_workflow_step_skipped(SimpleNamespace(index=1, label="Dismiss banner"))
        self.window._on_workflow_completed()
        self.assertEqual(self.window._workflow_skipped, ["Dismiss banner"])
        self.assertIn("1 optional step(s) failed", self.window.notice._label.text())
        self.assertIn("Dismiss banner", self.window.notice._label.text())


if __name__ == "__main__":
    unittest.main()

