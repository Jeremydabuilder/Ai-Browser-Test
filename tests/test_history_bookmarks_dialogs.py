"""History and Bookmarks dialogs: empty states, filtering, deletion.

An empty QTreeWidget with just column headers looks broken rather than
empty on purpose - these tests are here because, before this, there was no
test coverage of these dialogs at all.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_history_bookmarks_dialogs -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.storage.bookmarks import BookmarkStore  # noqa: E402
from app.storage.database import Database  # noqa: E402
from app.storage.history import HistoryStore  # noqa: E402
from app.ui.dialogs import BookmarksDialog, HistoryDialog  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


def _database() -> Database:
    path = os.path.join(tempfile.mkdtemp(prefix="dialogs-tests-"), "browser.sqlite3")
    return Database(path)


class HistoryDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _database()
        self.history = HistoryStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        _app.processEvents()

    def test_empty_history_shows_a_message_not_a_blank_tree(self) -> None:
        dialog = HistoryDialog(self.history)
        self.assertTrue(dialog.tree.isHidden())
        self.assertFalse(dialog._empty_label.isHidden())
        self.assertIn("will appear here", dialog._empty_label.text())
        dialog.deleteLater()

    def test_history_with_entries_shows_the_tree_not_the_message(self) -> None:
        self.history.add_visit("https://example.com/", "Example")
        dialog = HistoryDialog(self.history)
        self.assertFalse(dialog.tree.isHidden())
        self.assertTrue(dialog._empty_label.isHidden())
        self.assertEqual(dialog.tree.topLevelItemCount(), 1)
        dialog.deleteLater()

    def test_a_filter_with_no_matches_gets_its_own_message(self) -> None:
        self.history.add_visit("https://example.com/", "Example")
        dialog = HistoryDialog(self.history)
        dialog.filter_box.setText("nonexistent-term-xyz")
        self.assertTrue(dialog.tree.isHidden())
        self.assertIn("No history matches", dialog._empty_label.text())
        dialog.deleteLater()

    def test_deleting_the_last_entry_brings_back_the_empty_state(self) -> None:
        self.history.add_visit("https://example.com/", "Example")
        dialog = HistoryDialog(self.history)
        self.assertFalse(dialog.tree.isHidden())
        dialog.tree.topLevelItem(0).setSelected(True)
        dialog._delete_selected()
        self.assertFalse(dialog._empty_label.isHidden())
        dialog.deleteLater()

    def test_clearing_all_history_asks_for_confirmation(self) -> None:
        # test_agent.py-style: assert the button exists and is wired, not the
        # native QMessageBox (which cannot be driven in an offscreen test).
        self.history.add_visit("https://example.com/", "Example")
        dialog = HistoryDialog(self.history)
        labels = [dialog.button_row.itemAt(i).widget().text()
                 for i in range(dialog.button_row.count())
                 if dialog.button_row.itemAt(i).widget() is not None]
        self.assertIn("Clear all history", labels)
        dialog.deleteLater()


class BookmarksDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = _database()
        self.bookmarks = BookmarkStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        _app.processEvents()

    def test_empty_bookmarks_shows_a_message_not_a_blank_tree(self) -> None:
        dialog = BookmarksDialog(self.bookmarks)
        self.assertTrue(dialog.tree.isHidden())
        self.assertFalse(dialog._empty_label.isHidden())
        self.assertIn("Nothing saved yet", dialog._empty_label.text())
        dialog.deleteLater()

    def test_bookmarks_with_entries_shows_the_tree_not_the_message(self) -> None:
        self.bookmarks.add("https://example.com/", "Example")
        dialog = BookmarksDialog(self.bookmarks)
        self.assertFalse(dialog.tree.isHidden())
        self.assertTrue(dialog._empty_label.isHidden())
        dialog.deleteLater()

    def test_a_filter_with_no_matches_gets_its_own_message(self) -> None:
        self.bookmarks.add("https://example.com/", "Example")
        dialog = BookmarksDialog(self.bookmarks)
        dialog.filter_box.setText("nonexistent-term-xyz")
        self.assertTrue(dialog.tree.isHidden())
        self.assertIn("No bookmarks match", dialog._empty_label.text())
        dialog.deleteLater()

    def test_deleting_the_last_bookmark_brings_back_the_empty_state(self) -> None:
        self.bookmarks.add("https://example.com/", "Example")
        dialog = BookmarksDialog(self.bookmarks)
        dialog.tree.topLevelItem(0).setSelected(True)
        dialog._delete_selected()
        self.assertFalse(dialog._empty_label.isHidden())
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
