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


if __name__ == "__main__":
    unittest.main()
