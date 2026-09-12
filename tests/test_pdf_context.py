"""PDF text extraction: page numbers, search, and honest failure for
anything that isn't a real, readable, text-bearing PDF.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_pdf_context -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.browser.pdf_context import (  # noqa: E402
    MAX_PAGES,
    PdfExtractionError,
    extract_pdf,
    is_pdf_url,
)


def _make_pdf_bytes(*page_texts: str) -> bytes:
    """A minimal, valid, hand-built multi-page PDF - no external tool
    needed, and no dependency on a PDF-writing library actually being able
    to produce real text content (most convenience wrappers only add
    blank pages)."""
    n = len(page_texts)
    kids = " ".join(f"{3 + i} 0 R" for i in range(n))
    objects = [
        f"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>",
    ]
    page_objs = []
    content_objs = []
    font_obj_index = 3 + n
    for i, text in enumerate(page_texts):
        page_objs.append(
            f"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 "
            f"{font_obj_index} 0 R >> >> /MediaBox [0 0 612 792] "
            f"/Contents {font_obj_index + 1 + i} 0 R >>")
    for text in page_texts:
        stream = f"BT /F1 24 Tf 100 700 Td ({text}) Tj ET".encode()
        content_objs.append(stream)
    objects.extend(page_objs)
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    body = "%PDF-1.4\n"
    obj_num = 1
    for obj in objects:
        body += f"{obj_num} 0 obj\n{obj}\nendobj\n"
        obj_num += 1
    for stream in content_objs:
        body += (f"{obj_num} 0 obj\n<< /Length {len(stream)} >>\nstream\n"
                 f"{stream.decode()}\nendstream\nendobj\n")
        obj_num += 1
    body += f"trailer\n<< /Size {obj_num} /Root 1 0 R >>\nstartxref\n0\n%%EOF\n"
    return body.encode()


class IsPdfUrlTests(unittest.TestCase):
    def test_a_pdf_extension_is_recognised(self) -> None:
        self.assertTrue(is_pdf_url("https://example.com/report.pdf"))

    def test_case_is_ignored(self) -> None:
        self.assertTrue(is_pdf_url("https://example.com/REPORT.PDF"))

    def test_an_ordinary_page_is_not_a_pdf(self) -> None:
        self.assertFalse(is_pdf_url("https://example.com/report.html"))

    def test_query_string_after_pdf_still_counts(self) -> None:
        self.assertTrue(is_pdf_url("https://example.com/report.pdf?v=2"))

    def test_pdf_appearing_only_in_the_query_does_not_count(self) -> None:
        self.assertFalse(is_pdf_url("https://example.com/view?file=report.pdf"))


class ExtractPdfTests(unittest.TestCase):
    def test_extracts_text_with_a_page_number(self) -> None:
        data = _make_pdf_bytes("Hello World")
        doc = extract_pdf("upload://test.pdf", data=data)
        self.assertEqual(doc.page_count, 1)
        self.assertEqual(doc.pages[0].number, 1)
        self.assertIn("Hello World", doc.pages[0].text)

    def test_full_text_labels_each_page(self) -> None:
        data = _make_pdf_bytes("First page", "Second page")
        doc = extract_pdf("upload://test.pdf", data=data)
        self.assertEqual(doc.page_count, 2)
        self.assertIn("[Page 1]", doc.full_text)
        self.assertIn("[Page 2]", doc.full_text)
        self.assertIn("First page", doc.full_text)
        self.assertIn("Second page", doc.full_text)

    def test_a_pdf_with_real_text_is_not_reported_as_scanned(self) -> None:
        data = _make_pdf_bytes("Some real text")
        doc = extract_pdf("upload://test.pdf", data=data)
        self.assertFalse(doc.is_scanned)

    def test_corrupt_bytes_raise_a_clean_error(self) -> None:
        with self.assertRaises(PdfExtractionError):
            extract_pdf("upload://bad.pdf", data=b"not a pdf at all")

    def test_an_unreadable_source_scheme_raises_a_clean_error(self) -> None:
        with self.assertRaises(PdfExtractionError):
            extract_pdf("ftp://example.com/report.pdf")

    def test_search_finds_a_matching_page_and_snippet(self) -> None:
        data = _make_pdf_bytes("Nothing here", "The secret keyword appears here")
        doc = extract_pdf("upload://test.pdf", data=data)
        results = doc.search("secret keyword")
        self.assertEqual(len(results), 1)
        page_number, snippet = results[0]
        self.assertEqual(page_number, 2)
        self.assertIn("secret keyword", snippet)

    def test_search_is_case_insensitive(self) -> None:
        data = _make_pdf_bytes("Findable Text Here")
        doc = extract_pdf("upload://test.pdf", data=data)
        self.assertTrue(doc.search("findable text"))

    def test_search_with_no_match_returns_nothing(self) -> None:
        data = _make_pdf_bytes("Something")
        doc = extract_pdf("upload://test.pdf", data=data)
        self.assertEqual(doc.search("not present anywhere"), [])

    def test_page_count_is_capped(self) -> None:
        data = _make_pdf_bytes(*[f"Page {i}" for i in range(5)])
        doc = extract_pdf("upload://test.pdf", data=data, max_pages=2)
        self.assertEqual(doc.page_count, 2)

    def test_reading_a_real_local_file(self) -> None:
        import tempfile

        data = _make_pdf_bytes("From a real file")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
            handle.write(data)
            path = handle.name
        try:
            doc = extract_pdf(f"file://{path}")
            self.assertIn("From a real file", doc.full_text)
        finally:
            os.unlink(path)

    def test_a_missing_local_file_raises_a_clean_error(self) -> None:
        with self.assertRaises(PdfExtractionError):
            extract_pdf("file:///no/such/file.pdf")


if __name__ == "__main__":
    unittest.main()
