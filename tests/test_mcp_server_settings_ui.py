"""Settings -> External AI Access: the panel and pairing dialog.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mcp_server_settings_ui -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from app.mcp_server.server import PyBrowserMcpServer  # noqa: E402
from app.storage import Database, McpServerAccessStore  # noqa: E402
from app.ui.mcp_server_settings import ExternalAiAccessPanel, PairClientDialog  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class ExternalAiAccessPanelTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmp.name, "t.sqlite3"))
        self.store = McpServerAccessStore(self.db)
        self.server = PyBrowserMcpServer(store=self.store, port=8891)
        self.panel = ExternalAiAccessPanel(self.server)

    def tearDown(self) -> None:
        self.panel.close()
        self.server.stop()
        self.db.close()
        self._tmp.cleanup()

    def test_shows_stopped_status_by_default(self) -> None:
        self.assertIn("Stopped", self.panel.status_label.text())
        self.assertEqual(self.panel.toggle_button.text(), "Enable")

    def test_enable_starts_the_server_and_updates_the_label(self) -> None:
        self.panel._on_toggle()
        self.assertTrue(self.server.running)
        self.assertIn("Running", self.panel.status_label.text())
        self.assertEqual(self.panel.toggle_button.text(), "Disable")

    def test_pairing_a_client_adds_a_row_to_the_table(self) -> None:
        from app.mcp_server import auth

        auth.pair_client(self.store, display_name="ChatGPT", capabilities=["read_pages"])
        self.panel._refresh()
        self.assertEqual(self.panel.table.rowCount(), 1)
        self.assertEqual(self.panel.table.item(0, 0).text(), "ChatGPT")

    def test_revoke_marks_the_selected_client_revoked(self) -> None:
        from app.mcp_server import auth

        auth.pair_client(self.store, display_name="ChatGPT", capabilities=[])
        self.panel._refresh()
        self.panel.table.selectRow(0)
        self.panel._on_revoke()
        self.assertEqual(self.panel.table.item(0, 4).text(), "Revoked")


class PairClientDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmp.name, "t.sqlite3"))
        self.store = McpServerAccessStore(self.db)
        self.dialog = PairClientDialog(self.store, "127.0.0.1", 8765)

    def tearDown(self) -> None:
        self.dialog.close()
        self.db.close()
        self._tmp.cleanup()

    def test_generate_requires_a_name(self) -> None:
        original = QMessageBox.information
        QMessageBox.information = staticmethod(
            lambda *a, **k: QMessageBox.StandardButton.Ok)
        try:
            self.dialog._on_generate()
        finally:
            QMessageBox.information = original
        self.assertEqual(len(self.store.list_clients()), 0)

    def test_generate_pairs_a_client_with_only_checked_capabilities(self) -> None:
        self.dialog.name_edit.setText("ChatGPT")
        from app.mcp_server.types import Capability

        self.dialog._checks[Capability.READ_PAGES].setChecked(True)
        self.dialog._on_generate()
        clients = self.store.list_clients()
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].capabilities, ("read_pages",))

    def test_the_token_is_shown_once_and_the_form_locks(self) -> None:
        self.dialog.name_edit.setText("X")
        self.dialog._on_generate()
        self.assertFalse(self.dialog._result_area.isHidden())
        self.assertIn("Pairing token", self.dialog._result_area.toPlainText())
        self.assertFalse(self.dialog.name_edit.isEnabled())
        self.assertFalse(self.dialog._generate_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
