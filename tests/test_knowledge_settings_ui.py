"""Settings -> Privacy / Memory / Knowledge panel, and the Search History
& Knowledge dialog.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_knowledge_settings_ui -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.knowledge.index import KnowledgeIndex  # noqa: E402
from app.storage import Database, KnowledgeStore  # noqa: E402
from app.storage.settings import SettingsStore  # noqa: E402
from app.ui.knowledge_search_dialog import KnowledgeSearchDialog  # noqa: E402
from app.ui.knowledge_settings import KnowledgeSettingsPanel  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


def _make():
    tmp = tempfile.TemporaryDirectory()
    db = Database(os.path.join(tmp.name, "t.sqlite3"))
    store = KnowledgeStore(db)
    settings = SettingsStore(db)
    index = KnowledgeIndex(store, settings)
    return index, settings, db, tmp


class KnowledgeSettingsPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index, self.settings, self.db, self._tmp = _make()
        self.panel = KnowledgeSettingsPanel(self.index, self.settings)

    def tearDown(self) -> None:
        self.panel.close()
        self.db.close()
        self._tmp.cleanup()

    def test_off_by_default(self) -> None:
        self.assertFalse(self.panel.enable_check.isChecked())
        self.assertFalse(self.settings.semantic_history_enabled)

    def test_checking_the_box_enables_the_setting(self) -> None:
        self.panel.enable_check.setChecked(True)
        self.assertTrue(self.settings.semantic_history_enabled)

    def test_unchecking_disables_but_keeps_existing_index_entries(self) -> None:
        self.panel.enable_check.setChecked(True)
        self.index.index_highlight(1, "some text")
        self.panel.enable_check.setChecked(False)
        self.assertFalse(self.settings.semantic_history_enabled)
        self.assertEqual(self.index.store.count(), 1)

    def test_disabling_stops_new_indexing(self) -> None:
        self.panel.enable_check.setChecked(True)
        self.panel.enable_check.setChecked(False)
        self.index.index_highlight(1, "should not be indexed")
        self.assertEqual(self.index.store.count(), 0)

    def test_clear_removes_everything(self) -> None:
        import app.ui.knowledge_settings as module

        original = module.confirm_destructive
        module.confirm_destructive = lambda *a, **k: True
        try:
            self.panel.enable_check.setChecked(True)
            self.index.index_highlight(1, "some text")
            self.panel._on_clear()
        finally:
            module.confirm_destructive = original
        self.assertEqual(self.index.store.count(), 0)

    def test_rebuild_calls_the_injected_callback_when_enabled(self) -> None:
        called = []
        self.panel.enable_check.setChecked(True)
        self.panel.rebuild_callback = lambda: called.append(True)
        self.panel._on_rebuild()
        self.assertTrue(called)

    def test_rebuild_does_nothing_while_disabled(self) -> None:
        called = []
        self.panel.rebuild_callback = lambda: called.append(True)
        original = QMessageBox.information
        QMessageBox.information = staticmethod(lambda *a, **k: None)
        try:
            self.panel._on_rebuild()
        finally:
            QMessageBox.information = original
        self.assertFalse(called)

    def test_status_shows_chunk_and_source_counts(self) -> None:
        self.panel.enable_check.setChecked(True)
        self.index.index_highlight(1, "some text")
        self.panel._refresh_status()
        self.assertIn("1 chunks", self.panel.status_label.text())


class KnowledgeSearchDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.index, self.settings, self.db, self._tmp = _make()
        self.settings.semantic_history_enabled = True

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_shows_a_disabled_message_when_semantic_history_is_off(self) -> None:
        self.settings.semantic_history_enabled = False
        dialog = KnowledgeSearchDialog(self.index)
        self.assertFalse(hasattr(dialog, "search_box"))
        dialog.close()

    def test_typing_a_query_populates_results(self) -> None:
        self.index.index_highlight(1, "MCP permissions are scoped per client")
        dialog = KnowledgeSearchDialog(self.index)
        dialog.search_box.setText("MCP permissions")
        self.assertEqual(dialog.list_widget.count(), 1)
        dialog.close()

    def test_clearing_the_query_clears_results(self) -> None:
        self.index.index_highlight(1, "MCP permissions are scoped per client")
        dialog = KnowledgeSearchDialog(self.index)
        dialog.search_box.setText("MCP permissions")
        dialog.search_box.setText("")
        self.assertEqual(dialog.list_widget.count(), 0)
        dialog.close()

    def test_opening_a_result_invokes_the_callback(self) -> None:
        self.index.index_highlight(1, "MCP permissions are scoped per client")
        opened = []
        dialog = KnowledgeSearchDialog(self.index, on_open=lambda r: opened.append(r))
        dialog.search_box.setText("MCP permissions")
        dialog.list_widget.setCurrentRow(0)
        dialog._open_selected()
        self.assertEqual(len(opened), 1)


if __name__ == "__main__":
    unittest.main()
