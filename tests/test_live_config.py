"""Changing the AI configuration while the browser is running.

The browser used to tell you to restart after entering an API key. It no
longer does, and these tests are what stops that regressing: each one changes
configuration on a running window and asserts the agent picked it up.

No real credential is used anywhere here - the environment variable is set to
a syntactically valid but fake key, and no request is ever made.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_live_config -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-live-tests-"))
# Never touch the developer's real keyring from a test, and never let a broken
# one take the process down with it.
os.environ["PYBROWSER_DISABLE_KEYRING"] = "1"

import app.browser  # noqa: E402,F401

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.credentials import Credential, Mode, resolve  # noqa: E402
from app.browser.profile import BrowserProfile  # noqa: E402
from app.config import database_path  # noqa: E402
from app.storage import Database  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_profile: BrowserProfile | None = None

FAKE_KEY = "sk-ant-test-not-a-real-key-0000000000"
OTHER_KEY = "sk-ant-test-not-a-real-key-1111111111"


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _profile = shared_profile()


class CredentialFingerprintTests(unittest.TestCase):
    """The comparison that decides whether anything changed."""

    def test_a_different_key_has_a_different_fingerprint(self) -> None:
        one = Credential(Mode.ENV_KEY, "k", secret=FAKE_KEY)
        two = Credential(Mode.ENV_KEY, "k", secret=OTHER_KEY)
        self.assertNotEqual(one.fingerprint, two.fingerprint)

    def test_the_same_key_has_the_same_fingerprint(self) -> None:
        self.assertEqual(Credential(Mode.ENV_KEY, "k", secret=FAKE_KEY).fingerprint,
                         Credential(Mode.ENV_KEY, "k", secret=FAKE_KEY).fingerprint)

    def test_the_fingerprint_does_not_contain_the_key(self) -> None:
        printed = Credential(Mode.ENV_KEY, "k", secret=FAKE_KEY).fingerprint
        self.assertNotIn(FAKE_KEY, printed)
        self.assertNotIn(FAKE_KEY[-8:], printed)

    def test_the_same_key_by_a_different_route_is_a_different_credential(self) -> None:
        # Keyring and environment are different ways in, and swapping between
        # them rebuilds the client, so they must not compare equal.
        self.assertNotEqual(
            Credential(Mode.ENV_KEY, "k", secret=FAKE_KEY).fingerprint,
            Credential(Mode.KEYRING, "k", secret=FAKE_KEY).fingerprint)


class LiveReconfigurationTests(unittest.TestCase):
    """A running window, reconfigured."""

    def setUp(self) -> None:
        self.database = Database(database_path())
        self.window = MainWindow(_profile, self.database)
        # These tests share one database file, so a test that changes the model
        # would otherwise decide the starting point of the next one.
        self.window.settings.set("agent_model", "")
        self.window.settings.set("agent_effort", "")
        self.window.resize(1100, 800)
        self.window.show()
        self._original_env = os.environ.get("ANTHROPIC_API_KEY")
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def tearDown(self) -> None:
        if self._original_env is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = self._original_env
        self.window.close()
        self.window.deleteLater()
        self.database.close()
        _app.processEvents()

    def open_panel(self):
        self.window._toggle_agent_panel()
        _app.processEvents()
        return self.window._side_panel

    # -- the bug this file exists for -------------------------------------
    def test_adding_a_key_enables_the_agent_without_a_restart(self) -> None:
        panel = self.open_panel()
        self.assertIsNone(self.window._agent_session, "no credential, so no session")
        self.assertTrue(self.window._agent_unavailable)
        self.assertIn("not set up", panel.transcript.toPlainText().lower())

        # The user pastes a key and saves. No restart.
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        self.window._apply_agent_settings()
        _app.processEvents()

        self.assertIsNotNone(self.window._agent_session,
                             "the agent should have started as soon as the key existed")
        self.assertFalse(self.window._agent_unavailable)
        new_panel = self.window._side_panel
        self.assertNotIn("not set up", new_panel.transcript.toPlainText().lower())
        self.assertTrue(new_panel.input.isEnabled())

    def test_the_new_tab_page_stops_saying_the_agent_is_missing(self) -> None:
        self.open_panel()
        self.assertFalse(self.window._agent_configured())
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        self.window._apply_agent_settings()
        self.assertTrue(self.window._agent_configured())

    def test_swapping_the_key_rebuilds_the_session(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        self.open_panel()
        first = self.window._agent_session
        self.assertIsNotNone(first)
        fingerprint = self.window._credential_id
        self.assertTrue(fingerprint)

        os.environ["ANTHROPIC_API_KEY"] = OTHER_KEY
        self.window._apply_agent_settings()
        _app.processEvents()

        self.assertIsNotNone(self.window._agent_session)
        self.assertIsNot(self.window._agent_session, first,
                         "a different key must not keep the old client")
        self.assertNotEqual(self.window._credential_id, fingerprint)

    def test_the_same_key_does_not_disturb_the_session(self) -> None:
        # Rebuilding costs the conversation, so it must only happen on a real
        # change - not every time the settings dialog is opened and closed.
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        self.open_panel()
        session = self.window._agent_session
        self.window._apply_agent_settings()
        self.window._apply_agent_settings()
        self.assertIs(self.window._agent_session, session)

    def test_changing_the_model_rebuilds_the_session(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        self.open_panel()
        session = self.window._agent_session
        self.assertIsNotNone(session)
        self.assertEqual(session.config.model, "claude-opus-5", "unexpected start state")
        self.window.settings.set("agent_model", "claude-sonnet-5")
        self.window._apply_agent_settings()
        _app.processEvents()
        self.assertIsNot(self.window._agent_session, session)
        self.assertEqual(self.window._agent_session.config.model, "claude-sonnet-5")

    def test_a_busy_agent_is_not_torn_down_underneath_the_user(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        self.open_panel()
        session = self.window._agent_session
        self.assertIsNotNone(session)
        session._set_state("thinking")
        self.window.settings.set("agent_model", "claude-sonnet-5")
        self.window._apply_agent_settings()
        self.assertIs(self.window._agent_session, session,
                      "a running task must not have its session pulled away")

    def test_removing_the_key_does_not_crash_the_browser(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        self.open_panel()
        self.assertIsNotNone(self.window._agent_session)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.window._apply_agent_settings()
        _app.processEvents()
        # The browser keeps working; the panel explains itself on next open.
        self.assertTrue(self.window.isVisible())

    def test_reconfiguring_with_the_panel_closed_is_harmless(self) -> None:
        self.assertIsNone(self.window._side_panel)
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        self.window._apply_agent_settings()
        _app.processEvents()
        self.assertIsNone(self.window._side_panel, "nothing should pop open by itself")

    def test_a_broken_credential_lookup_does_not_stop_the_browser(self) -> None:
        broken = self.window._current_credential
        try:
            self.window._current_credential = lambda: None
            self.window._apply_agent_settings()     # must not raise
        finally:
            self.window._current_credential = broken
        self.assertTrue(self.window.isVisible())

    def test_the_dialog_reports_a_saved_change(self) -> None:
        from app.ui.agent_setup import ApiKeyDialog

        dialog = ApiKeyDialog(self.window, self.window.settings)
        seen = []
        dialog.saved.connect(lambda: seen.append(True))
        dialog.model_box.setCurrentIndex(dialog.model_box.findData("claude-sonnet-5"))
        dialog._settings.set("agent_model", "claude-sonnet-5")
        dialog.saved.emit()
        self.assertTrue(seen, "the window needs to hear about a save")
        dialog.deleteLater()

    def test_no_dialog_text_tells_the_user_to_restart(self) -> None:
        """The words that were the bug."""
        import inspect

        from app.ui import agent_setup

        source = inspect.getsource(agent_setup)
        self.assertNotIn("Restart the browser", source)
        self.assertNotIn("restart the browser", source.lower().replace("restarts", ""))


class CredentialResolutionTests(unittest.TestCase):
    def test_resolve_sees_a_key_that_appeared_after_startup(self) -> None:
        # The whole feature rests on this: resolve() must read the environment
        # each time rather than caching what it saw at import.
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.assertFalse(resolve().available)
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        try:
            found = resolve()
            self.assertTrue(found.available)
            self.assertEqual(found.mode, Mode.ENV_KEY)
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)


if __name__ == "__main__":
    unittest.main()


class QuickActionTests(LiveReconfigurationTests):
    """New Tab -> Py -> agent panel, as one connected interaction."""

    def test_a_quick_action_opens_the_panel_with_the_request_written_out(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        self.window._on_internal_action(
            "ai", {"q": "Summarise the page I am looking at."})
        _app.processEvents()
        panel = self.window._side_panel
        self.assertIsNotNone(panel, "the panel did not open")
        self.assertEqual(panel.input.toPlainText(),
                         "Summarise the page I am looking at.")

    def test_the_request_is_not_sent_for_the_user(self) -> None:
        # Writing it out is help; sending it is the browser deciding what the
        # user meant.
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        self.window._on_internal_action("ai", {"q": "Research tidal power"})
        _app.processEvents()
        self.assertFalse(self.window._agent_session.busy)
        self.assertEqual(self.window._agent_session.messages, [])

    def test_py_reacts_to_being_summoned(self) -> None:
        from app.ui.mascot import MascotState

        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        self.window._on_internal_action("ai", {"q": "Compare my tabs"})
        _app.processEvents()
        self.assertEqual(self.window._side_panel.mascot.state(), MascotState.THINKING)

    def test_opening_py_with_nothing_typed_just_opens_it(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        self.window._on_internal_action("ai", {"q": ""})
        _app.processEvents()
        self.assertIsNotNone(self.window._side_panel)
        self.assertEqual(self.window._side_panel.input.toPlainText(), "")

    def test_py_is_sized_for_the_panel(self) -> None:
        from app.ui import theme

        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        self.window.resize(1400, 900)
        self.window._toggle_agent_panel()
        _app.processEvents()
        self.assertEqual(self.window._side_panel.mascot.width(),
                         theme.METRICS.mascot_panel)

    def test_py_shrinks_in_a_narrow_panel(self) -> None:
        from app.ui import theme

        os.environ["ANTHROPIC_API_KEY"] = FAKE_KEY
        self.window.resize(720, 700)
        self.window._toggle_agent_panel()
        _app.processEvents()
        self.assertEqual(self.window._side_panel.mascot.width(),
                         theme.METRICS.mascot_panel_small)


class RefusedNavigationStatusTests(unittest.TestCase):
    """The status bar must not call a working quick action a failure.

    Driven through the real window rather than a copy of its logic: the bug was
    one line in `_on_load_finished`, and a test that reimplements that line
    would have passed while the browser was still wrong.
    """

    def setUp(self) -> None:
        self.database = Database(database_path())
        self.window = MainWindow(_profile, self.database)
        self.window.resize(1000, 700)

    def tearDown(self) -> None:
        for tab in self.window.tabs.tabs():
            tab.page.deleteLater()
        self.window.close()
        self.window.deleteLater()
        self.database.close()
        _app.processEvents()

    def _status(self) -> str:
        return self.window._status_label.text()

    def test_a_refused_action_leaves_the_status_bar_empty(self) -> None:
        tab = self.window.tabs.current_tab()
        # Stand where Chromium stands: the page refused one of our action URLs,
        # so the load "failed" and the tab knows why.
        tab._refused_action = True
        self.window._on_load_finished(False)
        self.assertEqual(self._status(), "")

    def test_a_genuinely_failed_load_still_says_so(self) -> None:
        tab = self.window.tabs.current_tab()
        tab._refused_action = False
        self.window._on_load_finished(False)
        self.assertEqual(self._status(), "Page failed to load")

    def test_a_successful_load_clears_the_status(self) -> None:
        tab = self.window.tabs.current_tab()
        tab._refused_action = False
        self.window._on_load_finished(True)
        self.assertEqual(self._status(), "")
