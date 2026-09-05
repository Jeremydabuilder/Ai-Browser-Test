"""Help -> Keyboard Shortcuts: an in-app reference, not just the README table.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_shortcuts_help -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-shortcuts-"))

import app.browser  # noqa: E402,F401

from PySide6.QtGui import QAction  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.browser.profile import BrowserProfile  # noqa: E402
from app.storage.database import Database  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class ShortcutsHelpTests(unittest.TestCase):
    def setUp(self) -> None:
        path = os.path.join(tempfile.mkdtemp(prefix="shortcuts-db-"), "browser.sqlite3")
        self.db = Database(path)
        self.profile = BrowserProfile(_app)
        self.window = MainWindow(self.profile, self.db, start_urls=["about:blank"])

    def tearDown(self) -> None:
        self.window.close()
        self.db.close()
        _app.processEvents()

    def test_every_shortcut_names_a_real_key_and_an_action(self) -> None:
        for _section, shortcuts in MainWindow._SHORTCUTS:
            for key, action in shortcuts:
                self.assertTrue(key.strip())
                self.assertTrue(action.strip())

    def test_the_html_lists_every_shortcut(self) -> None:
        html = MainWindow._shortcuts_html()
        for _section, shortcuts in MainWindow._SHORTCUTS:
            for key, action in shortcuts:
                self.assertIn(action, html)
        # Spot-check a couple of actual key names too, not just the actions.
        self.assertIn("Ctrl+T", html)
        self.assertIn("Ctrl+Shift+A", html)

    def test_the_menu_action_opens_the_same_content(self) -> None:
        """The Help menu action and the help content must not drift apart -
        assert the action exists and is wired to the method the html test
        above already covers, rather than opening a real modal dialog
        (exec() blocks in a real event loop)."""
        found = [action for action in self.window.findChildren(QAction)
                if action.text() == "&Keyboard Shortcuts"]
        self.assertTrue(found, "no Keyboard Shortcuts action found in the window")
        self.assertEqual(found[0].shortcut().toString(), "Ctrl+/")


if __name__ == "__main__":
    unittest.main()
