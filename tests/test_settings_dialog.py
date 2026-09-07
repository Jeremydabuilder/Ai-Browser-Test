"""Tools -> Settings: new-tab and search preferences.

No prior test coverage existed for this dialog at all before this file.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_settings_dialog -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialogButtonBox  # noqa: E402

from app.storage.database import Database  # noqa: E402
from app.storage.settings import NEW_TAB_CUSTOM, SettingsStore  # noqa: E402
from app.ui.settings_dialog import SettingsDialog  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


def _database() -> Database:
    path = os.path.join(tempfile.mkdtemp(prefix="settings-dialog-tests-"), "browser.sqlite3")
    return Database(path)


class SettingsDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _database()
        self.settings = SettingsStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        _app.processEvents()

    def test_it_opens_with_the_stored_search_url(self) -> None:
        self.settings.search_url = "https://example.org/find?q={query}"
        dialog = SettingsDialog(self.settings)
        self.assertEqual(dialog.search.text(), "https://example.org/find?q={query}")
        dialog.deleteLater()

    def test_the_save_button_is_the_primary_action(self) -> None:
        dialog = SettingsDialog(self.settings)
        buttons = dialog.findChild(QDialogButtonBox)
        save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.assertEqual(save_button.property("kind"), "primary")
        dialog.deleteLater()

    def test_a_search_url_missing_the_placeholder_is_refused(self) -> None:
        original = self.settings.search_url
        dialog = SettingsDialog(self.settings)
        dialog.search.setText("https://example.org/find?q=fixed")
        dialog._save()
        self.assertIn("{query}", dialog.problem.text())
        self.assertEqual(self.settings.search_url, original)
        dialog.deleteLater()

    def test_saving_a_valid_search_url_persists_it(self) -> None:
        dialog = SettingsDialog(self.settings)
        dialog.search.setText("https://example.org/find?q={query}")
        seen = []
        dialog.saved.connect(lambda: seen.append(True))
        dialog._save()
        self.assertTrue(seen)
        self.assertEqual(self.settings.search_url, "https://example.org/find?q={query}")
        dialog.deleteLater()

    def test_custom_mode_with_no_address_is_refused(self) -> None:
        from app.storage.settings import NEW_TAB_MODES

        dialog = SettingsDialog(self.settings)
        dialog.search.setText("https://example.org/find?q={query}")
        custom_index = next(i for i, (mode, _label) in enumerate(NEW_TAB_MODES)
                            if mode == NEW_TAB_CUSTOM)
        dialog._modes.button(custom_index).setChecked(True)
        dialog.custom.setText("")
        dialog._save()
        self.assertTrue(dialog.problem.text())
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
