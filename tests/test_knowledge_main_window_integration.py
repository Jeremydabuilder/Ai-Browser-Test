"""Phase 13 wired into the real MainWindow: history visits, local PDFs,
local files, and highlight deletion cascading into the knowledge index -
each through its real, live call site rather than calling KnowledgeIndex
directly (see test_knowledge_core.py for that).

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_knowledge_main_window_integration -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-knowledge-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.knowledge.types import SourceType  # noqa: E402
from app.storage import Database  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402
from tests.test_pdf_context import _make_pdf_bytes  # noqa: E402

_app: QApplication | None = None
_profile = None


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _profile = shared_profile()


def pump(predicate, timeout_ms: int = 8000) -> bool:
    import time

    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(timeout_ms)
    while not predicate() and not expired[0]:
        _app.processEvents()
        time.sleep(0.01)
    timer.stop()
    return predicate()


class MainWindowKnowledgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.window = MainWindow(_profile, self.db, start_urls=["about:blank"])
        self.window.settings.semantic_history_enabled = True

    def tearDown(self) -> None:
        self.window.close()
        self.db.close()
        self._dir.cleanup()
        for _ in range(3):
            _app.processEvents()

    def test_visiting_a_page_indexes_its_title_and_url(self) -> None:
        self.window.tabs.new_tab(
            "data:text/html,<title>Knowledge Test Page</title><p>hello</p>")
        self.assertTrue(pump(lambda: any(
            "Knowledge Test Page" in c.text for c in self.window.knowledge_store.all_chunks())))

    def test_disabled_semantic_history_means_no_history_indexing(self) -> None:
        self.window.settings.semantic_history_enabled = False
        self.window.tabs.new_tab(
            "data:text/html,<title>Should Not Be Indexed</title><p>hello</p>")
        _app.processEvents()
        _app.processEvents()
        chunks = self.window.knowledge_store.all_chunks()
        self.assertFalse(any("Should Not Be Indexed" in c.text for c in chunks))

    def test_a_local_pdf_visit_is_indexed_with_real_extracted_text(self) -> None:
        pdf_bytes = _make_pdf_bytes("OAuth security details on page one")
        pdf_path = os.path.join(self._dir.name, "sample.pdf")
        with open(pdf_path, "wb") as handle:
            handle.write(pdf_bytes)
        pdf_url = Path(pdf_path).as_uri()

        self.window.tabs.new_tab(pdf_url)
        self.assertTrue(pump(lambda: bool(
            self.window.knowledge_store.chunks_for_source(SourceType.PDF, pdf_url))))
        chunks = self.window.knowledge_store.chunks_for_source(SourceType.PDF, pdf_url)
        self.assertTrue(any("OAuth security" in c.text for c in chunks))

    def test_saving_a_highlight_indexes_it_and_deleting_removes_it(self) -> None:
        self.window._save_highlight_from_selection(
            "https://example.com/article", "Article Title", "a highlighted passage")
        highlight = self.window.highlights.all()[0]
        chunks = self.window.knowledge_store.chunks_for_source(
            SourceType.HIGHLIGHT, str(highlight.id))
        self.assertEqual(len(chunks), 1)

        from app.ui.highlights_library import HighlightsLibraryDialog

        dialog = HighlightsLibraryDialog(
            self.window.highlights, self.window, knowledge_index=self.window.knowledge_index)
        dialog._selected = lambda: [highlight]
        import app.ui.highlights_library as hl_module

        original = hl_module.confirm_destructive
        hl_module.confirm_destructive = lambda *a, **k: True
        try:
            dialog._delete_selected()
        finally:
            hl_module.confirm_destructive = original
        dialog.close()
        self.assertEqual(
            len(self.window.knowledge_store.chunks_for_source(
                SourceType.HIGHLIGHT, str(highlight.id))), 0)

    def test_local_file_add_flow_indexes_the_file(self) -> None:
        path = os.path.join(self._dir.name, "notes.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("Notes about MCP permission scoping and OAuth.")
        from PySide6.QtWidgets import QFileDialog

        original = QFileDialog.getOpenFileName
        QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (path, ""))
        try:
            self.window._ask_py_about_local_file()
        finally:
            QFileDialog.getOpenFileName = original
        chunks = self.window.knowledge_store.chunks_for_source(SourceType.FILE, path)
        self.assertTrue(chunks)
        self.assertIn("MCP permission scoping", chunks[0].text)

    def test_rebuild_reindexes_history_missions_and_highlights(self) -> None:
        self.window._save_highlight_from_selection(
            "https://example.com/x", "X", "highlighted content")
        self.window.knowledge_store.clear()
        self.assertEqual(self.window.knowledge_store.count(), 0)
        self.window._rebuild_knowledge_index()
        self.assertGreater(self.window.knowledge_store.count(), 0)

    def test_the_search_history_and_knowledge_menu_action_exists(self) -> None:
        self.assertTrue(hasattr(self.window, "_open_knowledge_search"))


if __name__ == "__main__":
    unittest.main()
