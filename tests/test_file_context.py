"""Local file parsing for context: only ever the file a person explicitly
chose - see app/browser/file_context.py for why that's the whole safety
story here.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_file_context -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.browser.file_context import (  # noqa: E402
    MAX_FILE_BYTES, FileParsingError, read_local_file,
)


def _write(suffix: str, data: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return path


class ReadLocalFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._paths: list[str] = []

    def tearDown(self) -> None:
        for path in self._paths:
            if os.path.exists(path):
                os.unlink(path)

    def _make(self, suffix: str, data: bytes) -> str:
        path = _write(suffix, data)
        self._paths.append(path)
        return path

    def test_a_plain_text_file_is_read_verbatim(self) -> None:
        path = self._make(".txt", b"Hello, this is plain text.")
        doc = read_local_file(path)
        self.assertEqual(doc.kind, "text")
        self.assertIn("Hello, this is plain text.", doc.text)
        self.assertFalse(doc.truncated)

    def test_a_markdown_file_is_read_as_text(self) -> None:
        path = self._make(".md", b"# Heading\n\nSome *markdown*.")
        doc = read_local_file(path)
        self.assertEqual(doc.kind, "text")
        self.assertIn("# Heading", doc.text)

    def test_json_is_reformatted_and_validated(self) -> None:
        path = self._make(".json", json.dumps({"a": 1, "b": [1, 2, 3]}).encode())
        doc = read_local_file(path)
        self.assertEqual(doc.kind, "json")
        self.assertIn('"a": 1', doc.text)

    def test_malformed_json_raises_a_clean_error(self) -> None:
        path = self._make(".json", b"{not valid json")
        with self.assertRaises(FileParsingError):
            read_local_file(path)

    def test_csv_rows_are_readable_text(self) -> None:
        path = self._make(".csv", b"name,age\nAda,36\nGrace,85\n")
        doc = read_local_file(path)
        self.assertEqual(doc.kind, "csv")
        self.assertIn("Ada", doc.text)
        self.assertIn("age", doc.text)

    def test_the_filename_and_source_are_reported(self) -> None:
        path = self._make(".txt", b"content")
        doc = read_local_file(path)
        self.assertEqual(doc.filename, os.path.basename(path))
        self.assertEqual(doc.source, path)

    def test_an_unsupported_extension_is_refused(self) -> None:
        path = self._make(".exe", b"MZ\x00\x00binary")
        with self.assertRaises(FileParsingError):
            read_local_file(path)

    def test_a_missing_file_is_refused_cleanly(self) -> None:
        with self.assertRaises(FileParsingError):
            read_local_file("/no/such/file.txt")

    def test_a_file_over_the_size_limit_is_refused_before_parsing(self) -> None:
        path = self._make(".txt", b"x" * (MAX_FILE_BYTES + 1))
        with self.assertRaises(FileParsingError):
            read_local_file(path)

    def test_very_long_text_is_truncated_and_says_so(self) -> None:
        from app.browser.file_context import MAX_TEXT_CHARS

        path = self._make(".txt", b"a" * (MAX_TEXT_CHARS + 500))
        doc = read_local_file(path)
        self.assertTrue(doc.truncated)
        self.assertEqual(len(doc.text), MAX_TEXT_CHARS)

    def test_a_pdf_is_delegated_to_the_pdf_extractor(self) -> None:
        from tests.test_pdf_context import _make_pdf_bytes

        path = self._make(".pdf", _make_pdf_bytes("Text inside a local PDF file"))
        doc = read_local_file(path)
        self.assertEqual(doc.kind, "pdf")
        self.assertIn("Text inside a local PDF file", doc.text)

    def test_a_docx_file_is_readable(self) -> None:
        import docx

        fd, path = tempfile.mkstemp(suffix=".docx")
        os.close(fd)
        self._paths.append(path)
        document = docx.Document()
        document.add_paragraph("A paragraph inside a Word document.")
        document.save(path)
        doc = read_local_file(path)
        self.assertEqual(doc.kind, "docx")
        self.assertIn("A paragraph inside a Word document.", doc.text)

    def test_a_corrupt_docx_raises_a_clean_error(self) -> None:
        path = self._make(".docx", b"not actually a docx file")
        with self.assertRaises(FileParsingError):
            read_local_file(path)


if __name__ == "__main__":
    unittest.main()
