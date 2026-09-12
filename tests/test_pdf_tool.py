"""PDF support as it reaches the agent: BrowserController.get_pdf_text and
the browser_get_pdf_text tool built on it.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_pdf_tool -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-pdf-tool-tests-"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.tools import UNTRUSTED_CLOSE, UNTRUSTED_OPEN, ToolRegistry  # noqa: E402
from app.browser.controller import BrowserController  # noqa: E402
from app.browser.results import ErrorCode  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from tests.fixture_server import FixtureServer  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402
from tests.test_pdf_context import _make_pdf_bytes  # noqa: E402

_app: QApplication | None = None
_server: FixtureServer | None = None
_profile = None


def setUpModule() -> None:
    global _app, _server, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _server = FixtureServer()
    _profile = shared_profile()


def tearDownModule() -> None:
    if _server is not None:
        _server.stop()
    if _app is not None:
        for _ in range(3):
            _app.processEvents()


class PdfToolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _server
        self.tabs = TabManager(_profile, self.server.base)
        self.tabs.resize(1200, 800)
        self.tabs.show()
        self.browser = BrowserController(self.tabs)
        self._pdf_path = None

    def tearDown(self) -> None:
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        _app.processEvents()
        if self._pdf_path is not None:
            os.unlink(self._pdf_path)

    def open_pdf(self, *page_texts: str) -> None:
        data = _make_pdf_bytes(*page_texts)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
            handle.write(data)
            self._pdf_path = handle.name
        self.browser.open_tab(f"file://{self._pdf_path}").wait()

    def open_html(self) -> None:
        self.browser.open_tab(self.server.url("/")).wait()


class ControllerGetPdfTextTests(PdfToolTestCase):
    def test_extracts_the_pdfs_text_and_page_count(self) -> None:
        self.open_pdf("Hello from a real tab")
        result = self.browser.get_pdf_text()
        self.assertTrue(result.ok, result.error)
        self.assertIn("Hello from a real tab", result.data["text"])
        self.assertEqual(result.data["page_count"], 1)
        self.assertFalse(result.data["is_scanned"])

    def test_a_non_pdf_tab_is_refused(self) -> None:
        self.open_html()
        result = self.browser.get_pdf_text()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.NOT_A_PDF)

    def test_no_tab_is_refused(self) -> None:
        result = self.browser.get_pdf_text(tab_id=999)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.NO_TAB)

    def test_a_missing_file_is_a_clean_extraction_failure(self) -> None:
        self.browser.open_tab("file:///no/such/file.pdf").wait()
        result = self.browser.get_pdf_text()
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.PDF_EXTRACTION_FAILED)


class ToolDispatchTests(PdfToolTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.registry = ToolRegistry(self.browser)

    def test_the_tool_is_registered_and_read_only(self) -> None:
        from app.agent.tools import READ_ONLY_TOOLS, TOOL_NAMES

        self.assertIn("browser_get_pdf_text", TOOL_NAMES)
        self.assertIn("browser_get_pdf_text", READ_ONLY_TOOLS)

    def test_running_the_tool_returns_pdf_text_fenced_as_untrusted(self) -> None:
        self.open_pdf("Some real text on the tabs own PDF")
        outcome = self.registry.run("browser_get_pdf_text", {})
        self.assertIsNotNone(outcome.future)
        result = outcome.future.wait()
        self.assertTrue(result.ok, result.error)
        payload = self.registry.encode(result)
        rendered = self.registry.render(result, payload)
        self.assertIn(UNTRUSTED_OPEN, rendered)
        self.assertIn(UNTRUSTED_CLOSE, rendered)
        fenced = rendered.split(UNTRUSTED_OPEN, 1)[1].split(UNTRUSTED_CLOSE, 1)[0]
        self.assertIn("Some real text on the tabs own PDF", fenced)
        self.assertIn("page_count", fenced)

    def test_running_the_tool_on_a_non_pdf_tab_reports_a_clean_error(self) -> None:
        self.open_html()
        outcome = self.registry.run("browser_get_pdf_text", {})
        result = outcome.future.wait()
        self.assertFalse(result.ok)
        payload = self.registry.encode(result)
        self.assertEqual(payload["error"]["code"], ErrorCode.NOT_A_PDF)

    def test_the_tool_is_never_treated_as_a_write(self) -> None:
        assessment = self.registry.assess("browser_get_pdf_text", {})
        self.assertFalse(assessment["requires_confirmation"])


if __name__ == "__main__":
    unittest.main()
