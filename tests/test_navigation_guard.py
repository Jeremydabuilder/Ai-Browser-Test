"""Unit tests for the guard that rejects addresses Qt cannot actually load."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QUrl  # noqa: E402

from app.browser.tab import BrowserTab  # noqa: E402


class NavigableTests(unittest.TestCase):
    def navigable(self, text: str) -> bool:
        return BrowserTab._is_navigable(QUrl(text))

    def test_normal_urls_are_navigable(self):
        for url in ("https://example.com", "http://localhost:8000/x",
                    "file:///etc/hostname", "about:blank"):
            self.assertTrue(self.navigable(url), url)

    def test_scheme_without_a_host_is_rejected(self):
        # QUrl calls these "valid"; setUrl() on them yields a blank page.
        for url in ("http://", "https://", "http:///path", "ftp://"):
            self.assertFalse(self.navigable(url), url)

    def test_malformed_input_is_rejected(self):
        for url in ("", "://x", "ht!tp://x"):
            self.assertFalse(self.navigable(url), url)


if __name__ == "__main__":
    unittest.main()
