"""Phase 12 UI: the "Connect an AI" client cards and ClientSetupDialog.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mcp_client_setup_ui -v
"""

from __future__ import annotations

import gc
import os
import sys
import tempfile
import threading
import time
import unittest
import weakref
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer, qInstallMessageHandler  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import app.ui.mcp_server_settings as mcp_server_settings  # noqa: E402
from app.mcp_server.server import PyBrowserMcpServer  # noqa: E402
from app.mcp_server.types import ClientType, VerificationStatus  # noqa: E402
from app.mcp_server.verification import VerificationResult  # noqa: E402
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


class _QtMessageCapture:
    """Captures every message Qt itself emits (warnings, criticals, ...)
    via qInstallMessageHandler for the duration of a `with` block. Used to
    assert the absence of specific native/Qt warning text - a killTimer
    or "Destroyed while thread is still running" warning wouldn't
    necessarily fail a test any other way (it can print to stderr and the
    test still "passes"), which is exactly how these went unnoticed
    before they escalated to a real crash."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self._previous = None

    def _handler(self, _msg_type, _context, message) -> None:
        self.messages.append(str(message))

    def __enter__(self) -> "_QtMessageCapture":
        self._previous = qInstallMessageHandler(self._handler)
        return self

    def __exit__(self, *exc_info) -> None:
        qInstallMessageHandler(self._previous)

    def assert_clean(self, test: unittest.TestCase) -> None:
        forbidden = ("killTimer", "Destroyed while thread is still running")
        hits = [m for m in self.messages if any(f in m for f in forbidden)]
        test.assertEqual(hits, [], f"forbidden Qt warning(s) captured: {hits}")


def _dialog_verify_idle(dialog: ClientSetupDialog) -> bool:
    return dialog._thread is None


def _flush_finished_thread_cleanup(timeout_ms: int = 60000) -> bool:
    """thread.wait() returning only guarantees the worker OS thread has
    stopped and its `finished` signal has been emitted - the queued
    deleteLater()/_IN_FLIGHT_VERIFY_THREADS-discard handlers connected to
    it still need one or more GUI-thread event-loop turns to actually
    run. pump()'s own predicate can go true (dialog._thread is None) the
    instant _teardown_verify() clears it, before those queued handlers
    have had a turn - this gives them one, so a stress loop's own
    bookkeeping (_IN_FLIGHT_VERIFY_THREADS) is observed in its settled
    state rather than mid-flush."""
    return pump(
        lambda: not mcp_server_settings._IN_FLIGHT_VERIFY_THREADS
        and not mcp_server_settings._IN_FLIGHT_VERIFIERS,
        timeout_ms)


class ClientSetupVerifyLifecycleTests(unittest.TestCase):
    """Focused coverage for the deterministic verify lifecycle that
    replaced _on_verify's forced gc.collect() - see
    app/ui/mcp_server_settings.py's _VerifyReceiver and
    _IN_FLIGHT_VERIFY_THREADS docstrings for the cycle this eliminates."""

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

    def _paired_dialog(self) -> ClientSetupDialog:
        dialog = ClientSetupDialog(ClientType.CURSOR, self.server)
        dialog.preset_combo.setCurrentIndex(0)  # read_only - has read_tabs
        dialog._apply_preset_to_checks()
        dialog._on_pair()
        return dialog

    def test_verify_success(self) -> None:
        dialog = self._paired_dialog()
        dialog._on_verify()
        self.assertTrue(pump(lambda: _dialog_verify_idle(dialog)))
        self.assertEqual(dialog.status_label.text(), "Verified - Connected and verified.")
        self.assertIsNone(dialog._verifier)
        self.assertIsNone(dialog._thread)
        self.assertIsNone(dialog._verify_receiver)

    def test_verify_failure(self) -> None:
        # A stopped server makes verify_connection() report UNREACHABLE -
        # a real failure path, not a mocked one.
        self.server.stop()
        dialog = self._paired_dialog()
        dialog._on_verify()
        self.assertTrue(pump(lambda: _dialog_verify_idle(dialog)))
        self.assertIn("Unreachable", dialog.status_label.text())
        self.assertIsNone(dialog._verifier)
        self.assertIsNone(dialog._thread)

    def test_verify_timeout(self) -> None:
        # verify_connection() itself owns the real timeout/retry logic
        # (tested elsewhere) - here it's stood in for so this test is fast
        # and deterministic rather than depending on real socket timing,
        # while still exercising the exact same dialog-side lifecycle a
        # real timeout would.
        timeout_result = VerificationResult(VerificationStatus.UNREACHABLE, "timed out")
        with patch.object(mcp_server_settings, "verify_connection", return_value=timeout_result):
            dialog = self._paired_dialog()
            dialog._on_verify()
            self.assertTrue(pump(lambda: _dialog_verify_idle(dialog)))
        self.assertIn("timed out", dialog.status_label.text())
        self.assertIsNone(dialog._thread)

    def test_close_dialog_during_verify(self) -> None:
        # A slow verify_connection() so the close() below reliably lands
        # while the worker thread is still running, not after it already
        # finished.
        def _slow_verify(*_args, **_kwargs):
            time.sleep(0.3)
            return VerificationResult(VerificationStatus.VERIFIED, "Connected and verified.")

        with patch.object(mcp_server_settings, "verify_connection", side_effect=_slow_verify):
            dialog = self._paired_dialog()
            dialog._on_verify()
            thread = dialog._thread
            self.assertIsNotNone(thread)
            self.assertTrue(thread.isRunning())

            # reject() -> done() (see ClientSetupDialog.done()) - the same
            # path Esc or the window's own X button take. Using reject()
            # directly rather than close(): close() is a no-op on a
            # QWidget that was never shown, which this dialog, built
            # headless for the test, never is.
            # Must not block, crash, or destroy the still-running thread.
            dialog.reject()
            self.assertIsNone(dialog._thread)
            self.assertIsNone(dialog._verifier)
            self.assertIsNone(dialog._verify_receiver)
            self.assertTrue(thread.isRunning() or thread in mcp_server_settings._IN_FLIGHT_VERIFY_THREADS)

            # The abandoned thread still finishes and cleans itself up on
            # its own - nothing waits on it, nothing forces it, and
            # nothing crashes when its result arrives at a dialog that
            # already closed.
            self.assertTrue(pump(
                lambda: thread not in mcp_server_settings._IN_FLIGHT_VERIFY_THREADS, timeout_ms=15000))

    def test_repeated_verify_calls(self) -> None:
        # A slow verify so the second call lands while the first is still
        # in flight - the dialog-level guard in _on_verify() must make the
        # second call a safe no-op rather than starting a concurrent
        # second round trip.
        def _slow_verify(*_args, **_kwargs):
            time.sleep(0.2)
            return VerificationResult(VerificationStatus.VERIFIED, "Connected and verified.")

        with patch.object(mcp_server_settings, "verify_connection", side_effect=_slow_verify):
            dialog = self._paired_dialog()
            dialog._on_verify()
            first_thread = dialog._thread
            dialog._on_verify()  # should be a no-op - a verify is already in flight
            self.assertIs(dialog._thread, first_thread)

            self.assertTrue(pump(lambda: _dialog_verify_idle(dialog)))
        self.assertEqual(dialog.status_label.text(), "Verified - Connected and verified.")

    def test_100_sequential_verify_cycles(self) -> None:
        dialog = self._paired_dialog()
        for _ in range(100):
            dialog._on_verify()
            self.assertTrue(pump(lambda: _dialog_verify_idle(dialog)))
            self.assertIsNone(dialog._verifier)
            self.assertIsNone(dialog._thread)
            self.assertIsNone(dialog._verify_receiver)
        self.assertTrue(_flush_finished_thread_cleanup())

    def test_worker_thread_always_exits(self) -> None:
        dialog = self._paired_dialog()
        dialog._on_verify()
        thread = dialog._thread
        self.assertIsNotNone(thread)
        self.assertTrue(pump(lambda: _dialog_verify_idle(dialog)))
        self.assertFalse(thread.isRunning())

    def test_worker_and_qthread_references_cleared(self) -> None:
        dialog = self._paired_dialog()
        dialog._on_verify()
        self.assertTrue(pump(lambda: _dialog_verify_idle(dialog)))
        self.assertIsNone(dialog._verifier)
        self.assertIsNone(dialog._thread)
        self.assertIsNone(dialog._verify_receiver)

    def test_no_qt_warnings_across_repeated_verify_cycles(self) -> None:
        dialog = self._paired_dialog()
        with _QtMessageCapture() as capture:
            for _ in range(20):
                dialog._on_verify()
                self.assertTrue(pump(lambda: _dialog_verify_idle(dialog)))
        capture.assert_clean(self)
        self.assertTrue(_flush_finished_thread_cleanup())

    def test_no_forced_gc_collect_in_verification_path(self) -> None:
        # Structural: the whole point of this rewrite is that nothing in
        # the verify path needs to force a cyclic-GC pass anymore. Assert
        # it stays that way rather than relying on nobody ever adding it
        # back - both that the module no longer imports gc, and that
        # _on_verify's own source has no gc.collect() call.
        self.assertFalse(hasattr(mcp_server_settings, "gc"))

        # Also confirm gc.collect() isn't secretly still required to
        # reclaim this round trip's own objects - i.e. dialog<->_Verifier
        # (via the old direct bound-method connection) is not still a
        # reference cycle in disguise. With GC disabled, only ordinary
        # refcounting (plus Qt's own deferred-delete mechanism, forced to
        # run explicitly below via sendPostedEvents rather than left to
        # chance) can free anything; _VerifyReceiver holds nothing but a
        # weakref back to the dialog, so once the dialog drops its
        # _verifier/_verify_receiver attributes (see _teardown_verify) and
        # the done->deliver connection is disconnected, nothing should be
        # left referencing either object.
        gc.disable()
        try:
            dialog = self._paired_dialog()
            dialog._on_verify()
            verifier_ref = weakref.ref(dialog._verifier)
            receiver_ref = weakref.ref(dialog._verify_receiver)
            self.assertTrue(pump(lambda: _dialog_verify_idle(dialog)))
            # _IN_FLIGHT_VERIFIERS keeps its own strong reference until
            # thread.finished discards it (see _on_verify) - which needs
            # a further event-loop turn or two after _teardown_verify
            # already ran, same as _flush_finished_thread_cleanup's own
            # reasoning.
            self.assertTrue(_flush_finished_thread_cleanup())
            # _Verifier.deleteLater() (posted from its own run()) is an
            # ordinary, unrelated Qt mechanism, not part of what this test
            # checks - but plain processEvents() calls do not reliably
            # flush DeferredDelete events on every pass, so force it
            # explicitly via Qt's own API for exactly that, rather than
            # polling and risking a flaky pass/fail on unrelated timing.
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            _app.processEvents()
            self.assertIsNone(verifier_ref(), "the verifier was not reclaimed without gc.collect()")
            self.assertIsNone(receiver_ref(), "the receiver was not reclaimed without gc.collect()")
        finally:
            gc.enable()


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
