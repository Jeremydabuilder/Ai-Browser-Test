"""HighlightStore: persistence, search, note editing, deletion, and the
oversized-selection size limit - see app/storage/highlights.py.

Run with:
    python -m unittest tests.test_highlights -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.storage import Database  # noqa: E402
from app.storage.highlights import MAX_HIGHLIGHT_CHARS, HighlightStore  # noqa: E402


class HighlightStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.highlights = HighlightStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def test_saving_a_highlight_persists_its_fields(self) -> None:
        highlight, truncated = self.highlights.add(
            "https://example.com/article", "An Article", "The important sentence.")
        self.assertFalse(truncated)
        self.assertIsNotNone(highlight)
        self.assertEqual(highlight.url, "https://example.com/article")
        self.assertEqual(highlight.title, "An Article")
        self.assertEqual(highlight.text, "The important sentence.")
        self.assertEqual(highlight.note, "")
        self.assertTrue(highlight.created_at)

    def test_saving_empty_text_saves_nothing(self) -> None:
        highlight, truncated = self.highlights.add("https://x/", "X", "   ")
        self.assertIsNone(highlight)
        self.assertFalse(truncated)
        self.assertEqual(self.highlights.all(), [])

    def test_an_oversized_selection_is_truncated_and_flagged(self) -> None:
        huge = "x" * (MAX_HIGHLIGHT_CHARS + 500)
        highlight, truncated = self.highlights.add("https://x/", "X", huge)
        self.assertTrue(truncated)
        self.assertEqual(len(highlight.text), MAX_HIGHLIGHT_CHARS)

    def test_saved_highlights_persist_across_reopening_the_database(self) -> None:
        self.highlights.add("https://x/", "X", "keep this")
        self.db.close()
        reopened = Database(self.db.path)
        try:
            store = HighlightStore(reopened)
            self.assertEqual(len(store.all()), 1)
            self.assertEqual(store.all()[0].text, "keep this")
        finally:
            reopened.close()

    def test_all_orders_most_recent_first(self) -> None:
        self.highlights.add("https://x/", "X", "first")
        self.highlights.add("https://x/", "X", "second")
        rows = self.highlights.all()
        self.assertEqual(rows[0].text, "second")
        self.assertEqual(rows[1].text, "first")

    def test_search_matches_title_text_note_and_url(self) -> None:
        self.highlights.add("https://weather.example/", "Weather Site", "It will rain")
        self.highlights.add("https://news.example/", "News Site", "Something else")
        self.assertEqual(len(self.highlights.search("weather")), 1)
        self.assertEqual(len(self.highlights.search("rain")), 1)
        self.assertEqual(len(self.highlights.search("news.example")), 1)
        self.assertEqual(len(self.highlights.search("nonexistent")), 0)
        self.assertEqual(len(self.highlights.search("")), 2)

    def test_search_matches_the_note(self) -> None:
        highlight, _ = self.highlights.add("https://x/", "X", "some text")
        self.highlights.set_note(highlight.id, "remember this for later")
        self.assertEqual(len(self.highlights.search("remember")), 1)

    def test_editing_the_note_updates_it(self) -> None:
        highlight, _ = self.highlights.add("https://x/", "X", "some text")
        self.highlights.set_note(highlight.id, "a helpful note")
        refreshed = self.highlights.get(highlight.id)
        self.assertEqual(refreshed.note, "a helpful note")

    def test_deleting_removes_it(self) -> None:
        highlight, _ = self.highlights.add("https://x/", "X", "some text")
        self.highlights.remove(highlight.id)
        self.assertIsNone(self.highlights.get(highlight.id))
        self.assertEqual(self.highlights.all(), [])

    def test_the_highlight_survives_the_original_page_becoming_unavailable(self) -> None:
        """No later lookup of the source URL ever happens - url/title/text
        were copied in at save time, so there is nothing to fail."""
        highlight, _ = self.highlights.add(
            "https://gone.example/404-now", "A Page That Will Disappear",
            "A fact worth keeping.")
        # Simulate the page vanishing: nothing about that touches this store.
        refreshed = self.highlights.get(highlight.id)
        self.assertEqual(refreshed.text, "A fact worth keeping.")
        self.assertEqual(refreshed.title, "A Page That Will Disappear")


if __name__ == "__main__":
    unittest.main()
