"""Tools -> Agent Diagnostics: the developer-facing trace view.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_diagnostics -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-diag-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.config import AgentConfig  # noqa: E402
from app.agent.session import AgentSession  # noqa: E402
from app.browser.controller import BrowserController  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from app.ui.diagnostics import DiagnosticsDialog  # noqa: E402
from tests.fake_claude import ScriptedClaude, calls, find_ref, says  # noqa: E402
from tests.fixture_server import FixtureServer  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

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


def pump(predicate, timeout_ms: int = 15000) -> bool:
    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(timeout_ms)
    while not predicate() and not expired[0]:
        _app.processEvents()
    timer.stop()
    return predicate()


class DiagnosticsDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tabs = TabManager(_profile, _server.base)
        self.browser = BrowserController(self.tabs)
        self.browser.open_tab().wait()
        self.browser.navigate(_server.base).wait()
        self.session: AgentSession | None = None

    def tearDown(self) -> None:
        if self.session is not None:
            self.session.shutdown()
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        _app.processEvents()

    def start(self, script) -> None:
        self.session = AgentSession(self.browser, ScriptedClaude(script), AgentConfig())

    def test_with_no_session_it_says_so_rather_than_showing_an_empty_box(self) -> None:
        dialog = DiagnosticsDialog(None)
        self.assertIn("No AI agent session", dialog._summary.text())
        self.assertEqual(dialog._text.toPlainText(), "")
        dialog.deleteLater()

    def test_before_any_task_it_says_nothing_has_run_yet(self) -> None:
        self.start([says("hi")])
        dialog = DiagnosticsDialog(self.session)
        self.assertIn("No task has run yet", dialog._summary.text())
        dialog.deleteLater()

    def test_after_a_task_the_trace_is_shown(self) -> None:
        self.start([says("Hello.")])
        done = []
        self.session.finished.connect(lambda: done.append(True))
        self.session.send("hi")
        self.assertTrue(pump(lambda: bool(done)))

        dialog = DiagnosticsDialog(self.session)
        text = dialog._text.toPlainText()
        self.assertIn("task_started", text)
        self.assertIn("task_finished", text)
        self.assertIn("event(s) recorded", dialog._summary.text())
        dialog.deleteLater()

    def test_refresh_picks_up_events_recorded_after_the_dialog_opened(self) -> None:
        self.start([says("First."), says("Second.")])
        done = []
        self.session.finished.connect(lambda: done.append(True))
        self.session.send("one")
        self.assertTrue(pump(lambda: bool(done)))

        dialog = DiagnosticsDialog(self.session)
        first_snapshot = dialog._text.toPlainText()
        self.assertIn("task_finished", first_snapshot)

        done.clear()
        self.session.send("two")
        self.assertTrue(pump(lambda: bool(done)))
        # A new task restarts the trace (see Trace.start()), so refreshing
        # shows the *second* task's events, not a growing log of both - the
        # same "current task" scope the summary line advertises.
        dialog._refresh()
        second_snapshot = dialog._text.toPlainText()
        self.assertIn("task_finished", second_snapshot)
        self.assertNotEqual(first_snapshot, second_snapshot)
        dialog.deleteLater()

    def test_copy_puts_the_trace_on_the_clipboard(self) -> None:
        self.start([says("Hello.")])
        done = []
        self.session.finished.connect(lambda: done.append(True))
        self.session.send("hi")
        self.assertTrue(pump(lambda: bool(done)))

        dialog = DiagnosticsDialog(self.session)
        dialog._copy()
        self.assertIn("task_finished", QApplication.clipboard().text())
        dialog.deleteLater()

    def test_the_trace_never_contains_typed_text(self) -> None:
        """The same rule the activity log follows: tracing.py records the
        length of typed text, never the text itself - see SensitiveDataTests
        in tests/test_agent.py for the equivalent activity-log guarantee."""
        def type_password(messages):
            return calls("browser_type", {"ref": find_ref(messages, input_type="password"),
                                          "text": "hunter2-secret"})

        self.start([calls("browser_get_page"), type_password, says("done")])
        self.session.send("Log me in.")
        self.assertTrue(pump(lambda: self.session.state == "awaiting_confirmation"))
        self.session.resolve_confirmation(True)
        done = []
        self.session.finished.connect(lambda: done.append(True))
        self.assertTrue(pump(lambda: bool(done)))

        dialog = DiagnosticsDialog(self.session)
        self.assertNotIn("hunter2-secret", dialog._text.toPlainText())
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
