"""Unit tests for the network-error to plain-English mapping."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.browser.load_error import ErrorCategory, describe  # noqa: E402


class DescribeTests(unittest.TestCase):
    def test_dns_failure(self):
        error = describe("http://x/", -105, "net::ERR_NAME_NOT_RESOLVED")
        self.assertEqual(error.category, ErrorCategory.DNS)
        self.assertIn("could not be found", error.message)

    def test_connection_refused(self):
        self.assertEqual(describe("http://x/", -102).category, ErrorCategory.NETWORK)

    def test_untrusted_certificate(self):
        error = describe("https://x/", -202)
        self.assertEqual(error.category, ErrorCategory.CERTIFICATE)

    def test_unsafe_port_is_a_block_not_an_http_error(self):
        self.assertEqual(describe("http://x:1/", -312).category, ErrorCategory.BLOCKED)

    def test_user_abort_is_silent(self):
        self.assertTrue(describe("http://x/", -3).is_silent)

    def test_unknown_code_falls_back_to_its_range(self):
        self.assertEqual(describe("https://x/", -250).category, ErrorCategory.CERTIFICATE)

    def test_completely_unknown_code_still_reads_sensibly(self):
        error = describe("http://x/", -99999)
        self.assertEqual(error.category, ErrorCategory.UNKNOWN)
        self.assertTrue(error.message.endswith("."))

    def test_success_produces_no_error(self):
        self.assertEqual(describe("http://x/", 0).category, ErrorCategory.NONE)

    def test_messages_never_leak_technical_detail(self):
        """The user-facing sentence must not contain ERR_ codes or numbers."""
        for code in (-2, -3, -7, -105, -102, -202, -312, -324, -450, -800):
            message = describe("http://x/", code, "net::ERR_SOMETHING").message
            self.assertNotIn("ERR_", message)
            self.assertNotIn("net::", message)
            self.assertNotIn(str(code), message)

    def test_technical_detail_is_kept_separately(self):
        error = describe("http://x/", -105, "net::ERR_NAME_NOT_RESOLVED")
        self.assertIn("ERR_NAME_NOT_RESOLVED", error.technical)
        self.assertIn("-105", error.technical)


if __name__ == "__main__":
    unittest.main()
