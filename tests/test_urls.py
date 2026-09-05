"""Unit tests for address-bar input handling (no Qt GUI required)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.utils import urls  # noqa: E402

SEARCH = "https://duckduckgo.com/?q={query}"


class NormalizeTests(unittest.TestCase):
    def n(self, text: str) -> str:
        return urls.normalize(text, SEARCH).toString()

    def test_bare_hostname_gets_https(self):
        self.assertEqual(self.n("github.com"), "https://github.com")

    def test_full_url_is_preserved(self):
        self.assertEqual(self.n("https://x.com/a?b=1"), "https://x.com/a?b=1")

    def test_localhost_is_not_treated_as_a_scheme(self):
        self.assertEqual(self.n("localhost:8000"), "http://localhost:8000")

    def test_private_addresses_default_to_http(self):
        self.assertEqual(self.n("127.0.0.1:5000/x"), "http://127.0.0.1:5000/x")
        self.assertEqual(self.n("192.168.0.10"), "http://192.168.0.10")

    def test_public_ip_defaults_to_https(self):
        self.assertEqual(self.n("8.8.8.8"), "https://8.8.8.8")

    def test_plain_words_become_a_search(self):
        self.assertEqual(self.n("cheap laptops"), "https://duckduckgo.com/?q=cheap+laptops")
        self.assertTrue(urls.is_probably_search("cheap laptops"))

    def test_hostless_schemes_pass_through(self):
        self.assertEqual(self.n("about:blank"), "about:blank")
        self.assertEqual(self.n("mailto:a@b.com"), "mailto:a@b.com")

    def test_empty_input(self):
        self.assertEqual(self.n("   "), "about:blank")

    def test_short_host_strips_www(self):
        from PySide6.QtCore import QUrl

        self.assertEqual(urls.short_host(QUrl("https://www.google.com")), "google.com")


class TaskDetectionTests(unittest.TestCase):
    """looks_like_a_task: the address bar's "ask Py instead" affordance.

    Conservative on purpose - see the docstring on looks_like_a_task. A false
    positive puts a stray icon next to an ordinary search; a false negative
    just leaves the address bar behaving exactly as it always has."""

    def test_a_url_is_never_a_task(self):
        self.assertFalse(urls.looks_like_a_task("https://example.com"))
        self.assertFalse(urls.looks_like_a_task("github.com"))

    def test_an_empty_box_is_not_a_task(self):
        self.assertFalse(urls.looks_like_a_task(""))
        self.assertFalse(urls.looks_like_a_task("   "))

    def test_a_short_search_is_not_a_task(self):
        self.assertFalse(urls.looks_like_a_task("cheap laptops"))
        self.assertFalse(urls.looks_like_a_task("best budget noise cancelling headphones"))

    def test_a_sentence_starting_with_a_task_verb_is_a_task(self):
        self.assertTrue(urls.looks_like_a_task("find the cheapest flight to tokyo"))
        self.assertTrue(urls.looks_like_a_task("compare these two laptops for me"))
        self.assertTrue(urls.looks_like_a_task("research the best tennis rackets under $250"))
        self.assertTrue(urls.looks_like_a_task("plan a weekend trip to portland"))
        self.assertTrue(urls.looks_like_a_task("summarize this article for me please"))

    def test_a_long_sentence_is_a_task_even_without_a_task_verb(self):
        self.assertTrue(urls.looks_like_a_task(
            "noise cancelling headphones under $150 with good bass and long battery life"))

    def test_a_task_verb_that_is_actually_part_of_a_url_stays_a_url(self):
        self.assertFalse(urls.looks_like_a_task("plan.example.com"))


if __name__ == "__main__":
    unittest.main()
