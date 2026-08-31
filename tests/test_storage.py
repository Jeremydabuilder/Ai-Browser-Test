"""Unit tests for the SQLite stores (no Qt GUI required)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.storage import BookmarkStore, Database, HistoryStore, SettingsStore  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.history = HistoryStore(self.db)
        self.bookmarks = BookmarkStore(self.db)
        self.settings = SettingsStore(self.db)

    def tearDown(self):
        self.db.close()
        self._dir.cleanup()

    def test_history_records_and_reads_back(self):
        self.history.add_visit("https://a.example/", "A")
        self.history.add_visit("https://b.example/", "B")
        urls = [e.url for e in self.history.recent()]
        self.assertEqual(urls, ["https://b.example/", "https://a.example/"])

    def test_consecutive_visits_to_same_url_collapse(self):
        self.history.add_visit("https://a.example/", "A")
        self.history.add_visit("https://a.example/", "A again")
        self.assertEqual(self.history.count(), 1)
        self.assertEqual(self.history.recent()[0].title, "A again")

    def test_blank_and_error_pages_are_not_recorded(self):
        self.history.add_visit("about:blank", "")
        self.history.add_visit("chrome-error://x", "")
        self.assertEqual(self.history.count(), 0)

    def test_title_is_patched_after_the_fact(self):
        self.history.add_visit("https://a.example/", "")
        self.history.update_title("https://a.example/", "Late Title")
        self.assertEqual(self.history.recent()[0].title, "Late Title")

    def test_history_search_and_suggestions(self):
        self.history.add_visit("https://python.org/", "Python")
        self.history.add_visit("https://pypi.org/", "PyPI")
        self.assertEqual(len(self.history.search("py")), 2)
        self.assertIn("https://pypi.org/", self.history.suggestions("pypi"))

    def test_bookmark_add_is_idempotent(self):
        self.assertTrue(self.bookmarks.add("https://a.example/", "A"))
        self.assertFalse(self.bookmarks.add("https://a.example/", "A"))
        self.assertEqual(len(self.bookmarks.all()), 1)

    def test_bookmark_toggle(self):
        self.assertTrue(self.bookmarks.toggle("https://a.example/", "A"))
        self.assertTrue(self.bookmarks.contains("https://a.example/"))
        self.assertFalse(self.bookmarks.toggle("https://a.example/"))
        self.assertFalse(self.bookmarks.contains("https://a.example/"))

    def test_settings_defaults_and_roundtrip(self):
        self.assertTrue(self.settings.home_url.startswith("http"))
        self.settings.home_url = "https://example.org"
        self.assertEqual(self.settings.home_url, "https://example.org")
        self.settings.set_bool("restore_tabs", True)
        self.assertTrue(self.settings.get_bool("restore_tabs"))

    def test_data_survives_reopening_the_database(self):
        path = self.db.path
        self.history.add_visit("https://a.example/", "A")
        self.bookmarks.add("https://a.example/", "A")
        self.db.close()
        reopened = Database(path)
        self.assertEqual(HistoryStore(reopened).count(), 1)
        self.assertTrue(BookmarkStore(reopened).contains("https://a.example/"))
        reopened.close()


if __name__ == "__main__":
    unittest.main()
