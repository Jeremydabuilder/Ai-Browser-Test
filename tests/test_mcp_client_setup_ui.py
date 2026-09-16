"""Phase 12 UI: the "Connect an AI" client cards and ClientSetupDialog.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mcp_client_setup_ui -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.mcp_server.server import PyBrowserMcpServer  # noqa: E402
from app.mcp_server.types import ClientType, VerificationStatus  # noqa: E402
from app.storage import Database, McpServerAccessStore  # noqa: E402
from app.ui.mcp_server_settings import ClientSetupDialog, ExternalAiAccessPanel  # noqa: E402

_app: QApplication | None = None
_next_port = [8980]


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


def _free_port() -> int:
    _next_port[0] += 1
    return _next_port[0]


def pump(predicate, timeout_ms: int = 8000) -> bool:
    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(timeout_ms)
    while not predicate() and not expired[0]:
        _app.processEvents()
    timer.stop()
    return predicate()


class _FakeBrowser:
    def list_tabs(self):
        return [{"tab_id": 1, "url": "about:blank", "title": "New Tab", "active": True,
                "loading": False}]


class ClientSetupDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmp.name, "t.sqlite3"))
        self.store = McpServerAccessStore(self.db)
        self.server = PyBrowserMcpServer(store=self.store, browser=_FakeBrowser(),
                                         port=_free_port())
        self.assertTrue(self.server.start())

    def tearDown(self) -> None:
        self.server.stop()
        self.db.close()
        self._tmp.cleanup()

    def test_cursor_defaults_to_the_research_preset(self) -> None:
        dialog = ClientSetupDialog(ClientType.CURSOR, self.server)
        self.assertEqual(dialog.preset_combo.currentData(), "research")

    def test_chatgpt_defaults_to_read_only(self) -> None:
        dialog = ClientSetupDialog(ClientType.CHATGPT, self.server)
        self.assertEqual(dialog.preset_combo.currentData(), "read_only")

    def test_pairing_cursor_generates_a_cursor_shaped_config(self) -> None:
        dialog = ClientSetupDialog(ClientType.CURSOR, self.server)
        dialog._on_pair()
        self.assertIn("mcpServers", dialog.config_text.toPlainText())
        self.assertIn("streamableHttp", dialog.config_text.toPlainText())
        self.assertTrue(dialog.verify_button.isEnabled())

    def test_pairing_vscode_generates_a_servers_shaped_config(self) -> None:
        dialog = ClientSetupDialog(ClientType.VSCODE, self.server)
        dialog._on_pair()
        text = dialog.config_text.toPlainText()
        self.assertIn('"servers"', text)
        self.assertNotIn('"mcpServers"', text)

    def test_pairing_chatgpt_shows_the_tunnel_warning_and_never_enables_verify(self) -> None:
        dialog = ClientSetupDialog(ClientType.CHATGPT, self.server)
        dialog._on_pair()
        self.assertFalse(dialog.tunnel_warning.isHidden())
        self.assertFalse(dialog.verify_button.isEnabled())
        client = self.store.list_clients()[0]
        self.assertEqual(client.last_verified_status, VerificationStatus.REQUIRES_TUNNEL.value)

    def test_pairing_records_client_type_and_connection_method(self) -> None:
        dialog = ClientSetupDialog(ClientType.CURSOR, self.server)
        dialog._on_pair()
        client = self.store.list_clients()[0]
        self.assertEqual(client.client_type, "cursor")
        self.assertEqual(client.connection_method, "direct_http")

    def test_verifying_a_real_pairing_reaches_verified(self) -> None:
        # Chasing an intermittent native crash in this exact test on real
        # macOS CI hardware (never reproduced on Linux locally): waits on
        # the verifier's own completion signal via a real nested QEventLoop
        # instead of a manual `while not done: processEvents()` busy loop -
        # the crash's own backtrace showed the crashing thread stuck inside
        # that pump loop, and repeated re-entrant processEvents() calls are
        # a plausible source of the kind of Qt event-delivery re-entrancy
        # this crash resembles. A QEventLoop tied to one concrete signal
        # only ever processes one more event before returning, rather than
        # spinning the dispatcher in a tight native loop.
        before_threads = {t.ident for t in threading.enumerate()}
        dialog = ClientSetupDialog(ClientType.CURSOR, self.server)
        dialog.preset_combo.setCurrentIndex(0)  # read_only - has read_tabs
        dialog._apply_preset_to_checks()
        dialog._on_pair()
        dialog._on_verify()

        loop = QEventLoop()
        # Connected AFTER _on_verify() creates _verifier, and after this
        # dialog's own _on_verified is already connected to `done` - so by
        # Qt's in-order delivery to a signal's slots, _on_verified (which
        # does the real cleanup: clears the label, drops the verifier
        # reference, quits and waits on the thread) always runs before this
        # loop.quit(), same as the old pump()'s predicate check did.
        self.assertIsNotNone(dialog._verifier)
        dialog._verifier.done.connect(loop.quit)
        timeout_timer = QTimer()
        timeout_timer.setSingleShot(True)
        timeout_timer.timeout.connect(loop.quit)
        timeout_timer.start(8000)
        loop.exec()
        timeout_timer.stop()

        self.assertEqual(dialog.status_label.text(), "Verified - Connected and verified.")
        client = self.store.list_clients()[0]
        self.assertEqual(client.last_verified_status, VerificationStatus.VERIFIED.value)

        # Thread-lifetime diagnostic only (not asserted): a per-request HTTP
        # handler daemon thread can still be finishing its own exit right
        # after the response was sent, so "extra thread present" isn't by
        # itself a bug - but it's useful context alongside the VERIFYDIAG
        # trace when PYBROWSER_VERIFY_DIAG=1.
        if os.environ.get("PYBROWSER_VERIFY_DIAG") == "1":
            after_threads = {t.ident for t in threading.enumerate()}
            leaked = after_threads - before_threads
            leaked_names = [t.name for t in threading.enumerate() if t.ident in leaked]
            print(f"VERIFYDIAG [test] threads still alive after verify: {leaked_names}",
                  file=sys.stderr, flush=True)

    def test_generic_client_uses_live_capabilities_not_a_client_specific_generator(self) -> None:
        dialog = ClientSetupDialog(ClientType.GENERIC, self.server)
        dialog._on_pair()
        text = dialog.config_text.toPlainText()
        self.assertIn("endpoint", text)
        self.assertIn("scopes", text)


class ClientCardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmp.name, "t.sqlite3"))
        self.store = McpServerAccessStore(self.db)
        self.server = PyBrowserMcpServer(store=self.store, browser=_FakeBrowser(),
                                         port=_free_port())
        self.panel = ExternalAiAccessPanel(self.server)

    def tearDown(self) -> None:
        self.panel.close()
        self.server.stop()
        self.db.close()
        self._tmp.cleanup()

    def test_every_supported_client_gets_its_own_card(self) -> None:
        self.assertEqual(set(self.panel._cards), {
            ClientType.CURSOR, ClientType.CLAUDE, ClientType.CHATGPT,
            ClientType.VSCODE, ClientType.GENERIC})

    def test_an_unconfigured_client_shows_not_configured_and_set_up(self) -> None:
        card = self.panel._cards[ClientType.CURSOR]
        self.assertEqual(card.status_label.text(), "Not configured")
        self.assertEqual(card.action_button.text(), "Set up")

    def test_a_paired_client_shows_ready_and_manage(self) -> None:
        from app.mcp_server import auth

        auth.pair_client(self.store, display_name="Cursor", capabilities=["read_tabs"],
                         client_type=ClientType.CURSOR.value)
        self.panel._refresh()
        card = self.panel._cards[ClientType.CURSOR]
        self.assertIn("Ready", card.status_label.text())
        self.assertEqual(card.action_button.text(), "Manage")

    def test_a_verified_client_shows_verified(self) -> None:
        from app.mcp_server import auth

        client, _token = auth.pair_client(self.store, display_name="Cursor",
                                         capabilities=["read_tabs"],
                                         client_type=ClientType.CURSOR.value)
        self.store.record_verification(client.id, VerificationStatus.VERIFIED.value)
        self.panel._refresh()
        card = self.panel._cards[ClientType.CURSOR]
        self.assertIn("Verified", card.status_label.text())

    def test_a_revoked_client_does_not_count_as_configured(self) -> None:
        from app.mcp_server import auth

        client, _token = auth.pair_client(self.store, display_name="Cursor",
                                         capabilities=["read_tabs"],
                                         client_type=ClientType.CURSOR.value)
        auth.revoke_client(self.store, client.id)
        self.panel._refresh()
        card = self.panel._cards[ClientType.CURSOR]
        self.assertEqual(card.status_label.text(), "Not configured")


if __name__ == "__main__":
    unittest.main()
