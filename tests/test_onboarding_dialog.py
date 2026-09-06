"""The first-run welcome dialog: three screens, shown once.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_onboarding_dialog -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-onboard-"))

import app.browser  # noqa: E402,F401

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.browser.profile import BrowserProfile  # noqa: E402
from app.storage.database import Database  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.onboarding import FirstRunDialog  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class FirstRunDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dialog = FirstRunDialog()

    def tearDown(self) -> None:
        self.dialog.deleteLater()
        _app.processEvents()

    def test_it_opens_on_the_welcome_screen(self) -> None:
        self.assertEqual(self.dialog.stack.currentIndex(), 0)
        self.assertTrue(self.dialog.back_button.isHidden())

    def test_next_advances_through_all_three_screens(self) -> None:
        self.dialog._go_next()
        self.assertEqual(self.dialog.stack.currentIndex(), 1)
        self.dialog._go_next()
        self.assertEqual(self.dialog.stack.currentIndex(), 2)

    def test_the_last_screen_offers_to_finish_instead_of_advancing_further(self) -> None:
        self.dialog._go_next()
        self.dialog._go_next()
        self.assertEqual(self.dialog.next_button.text(), "Start browsing")
        result = []
        self.dialog.accepted.connect(lambda: result.append(True))
        self.dialog._go_next()
        self.assertTrue(result, "the dialog should close, not try a fourth screen")

    def test_back_returns_to_the_previous_screen(self) -> None:
        self.dialog._go_next()
        self.dialog._go_next()
        self.dialog._go_back()
        self.assertEqual(self.dialog.stack.currentIndex(), 1)
        self.assertFalse(self.dialog.back_button.isHidden())

    def test_skip_closes_the_dialog_from_any_screen(self) -> None:
        result = []
        self.dialog.accepted.connect(lambda: result.append(True))
        self.dialog.skip_button.click()
        self.assertTrue(result)

    def test_configure_now_emits_the_request_and_advances(self) -> None:
        self.dialog._go_next()   # to the provider screen
        seen = []
        self.dialog.configure_provider_requested.connect(lambda: seen.append(True))
        self.dialog._request_configure()
        self.assertTrue(seen)
        self.assertEqual(self.dialog.stack.currentIndex(), 2)

    def test_picking_a_suggestion_emits_its_prompt_and_closes(self) -> None:
        seen = []
        self.dialog.try_mission_requested.connect(seen.append)
        result = []
        self.dialog.accepted.connect(lambda: result.append(True))
        self.dialog._request_mission("Research something for me.")
        self.assertEqual(seen, ["Research something for me."])
        self.assertTrue(result)


class ShowFirstRunTests(unittest.TestCase):
    def setUp(self) -> None:
        path = os.path.join(tempfile.mkdtemp(prefix="onboard-db-"), "browser.sqlite3")
        self.db = Database(path)
        self.profile = BrowserProfile(_app)
        self.window = MainWindow(self.profile, self.db, start_urls=["about:blank"])

    def tearDown(self) -> None:
        self.window.close()
        self.db.close()
        _app.processEvents()

    def test_it_does_not_appear_a_second_time(self) -> None:
        """exec() would block a real event loop, so this only checks the
        early-return path - the actual dialog is covered by
        FirstRunDialogTests above."""
        self.window.settings.set_bool("first_run_dialog_shown", True)
        opened = []
        from app.ui import onboarding

        original = onboarding.FirstRunDialog
        onboarding.FirstRunDialog = lambda *a, **k: opened.append(True) or original(*a, **k)
        try:
            self.window.show_first_run_if_needed()
        finally:
            onboarding.FirstRunDialog = original
        self.assertFalse(opened, "the dialog must not be built once it has already been shown")


if __name__ == "__main__":
    unittest.main()
