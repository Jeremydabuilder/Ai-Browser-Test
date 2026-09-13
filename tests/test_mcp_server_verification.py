"""The shared verification engine (app/mcp_server/verification.py):
reachable -> authenticated -> initialize -> tools/list -> a safe call.
One engine, used identically for every client type - see Phase 12, Part 6.

Run with:
    python -m unittest tests.test_mcp_server_verification -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.mcp_server import auth  # noqa: E402
from app.mcp_server.server import PyBrowserMcpServer  # noqa: E402
from app.mcp_server.types import VerificationStatus  # noqa: E402
from app.mcp_server.verification import verify_connection  # noqa: E402
from app.storage import Database, McpServerAccessStore  # noqa: E402

_app: QApplication | None = None
_next_port = [8920]


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


def verify_pumped(url: str, token: str, timeout: float = 5.0):
    """verify_connection(), run on a background thread while this (GUI)
    thread keeps pumping Qt events - a real tool call routes through
    GuiBridge, which needs the GUI event loop running to deliver its
    cross-thread signal. A production "Verify" button must do the same
    (run verification off the GUI thread), never call verify_connection
    directly from a button handler on the GUI thread itself."""
    box: dict = {}

    def worker() -> None:
        box["result"] = verify_connection(url, token, timeout=timeout)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    pump(lambda: "result" in box, timeout_ms=int(timeout * 1000) + 3000)
    thread.join(timeout=1)
    return box.get("result")


class _FakeBrowser:
    """A minimal stand-in so browser.list_tabs (the verification engine's
    probe tool) actually succeeds, as it would against the real
    BrowserController every production server is constructed with -
    app/mcp_server/server.py's browser=None is a test-only shorthand, not
    a real deployment shape."""

    def list_tabs(self):
        return [{"tab_id": 1, "url": "about:blank", "title": "New Tab", "active": True,
                "loading": False}]


class VerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmp.name, "t.sqlite3"))
        self.store = McpServerAccessStore(self.db)
        self.port = _free_port()
        self.server = PyBrowserMcpServer(store=self.store, browser=_FakeBrowser(),
                                         port=self.port)
        self.assertTrue(self.server.start())
        # Give the background accept loop a moment to actually be listening.
        time.sleep(0.05)

    def tearDown(self) -> None:
        self.server.stop()
        self.db.close()
        self._tmp.cleanup()

    def _url(self) -> str:
        return f"http://{self.server.host}:{self.server.port}/mcp"

    def test_a_valid_token_with_read_tabs_verifies_successfully(self) -> None:
        _client, token = auth.pair_client(self.store, display_name="X",
                                         capabilities=["read_tabs"])
        result = verify_pumped(self._url(), token)
        self.assertEqual(result.status, VerificationStatus.VERIFIED)

    def test_a_valid_token_without_read_tabs_still_verifies_the_connection(self) -> None:
        """"Token created" is never conflated with "connected" - and the
        reverse holds too: lacking one optional capability must not be
        reported as a broken connection when everything else works."""
        _client, token = auth.pair_client(self.store, display_name="X", capabilities=[])
        result = verify_pumped(self._url(), token)
        self.assertEqual(result.status, VerificationStatus.VERIFIED)

    def test_an_invalid_token_reports_authentication_failed(self) -> None:
        result = verify_pumped(self._url(), "not-a-real-token")
        self.assertEqual(result.status, VerificationStatus.AUTHENTICATION_FAILED)

    def test_a_revoked_token_reports_authentication_failed(self) -> None:
        client, token = auth.pair_client(self.store, display_name="X", capabilities=["read_tabs"])
        auth.revoke_client(self.store, client.id)
        result = verify_pumped(self._url(), token)
        self.assertEqual(result.status, VerificationStatus.AUTHENTICATION_FAILED)

    def test_an_unreachable_server_reports_unreachable(self) -> None:
        result = verify_pumped("http://127.0.0.1:1/mcp", "any-token", timeout=1.0)
        self.assertEqual(result.status, VerificationStatus.UNREACHABLE)

    def test_a_stopped_server_reports_unreachable(self) -> None:
        url = self._url()
        self.server.stop()
        result = verify_pumped(url, "any-token", timeout=1.0)
        self.assertEqual(result.status, VerificationStatus.UNREACHABLE)


if __name__ == "__main__":
    unittest.main()
