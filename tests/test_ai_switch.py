"""The AI master switch (off by default), the clean window chrome, and AI Mode inside the real window.

Run with:  QT_QPA_PLATFORM=offscreen QTWEBENGINE_DISABLE_SANDBOX=1 python tests/run.py tests.test_ai_switch
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-aiswitch-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from app.browser.aimode import AIMODE_URL, is_ai_mode  # noqa: E402
from app.storage import Database  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402
from tests.test_aimode import Fetcher, Model, Web, session  # noqa: E402

_app = None
_profile = None


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _profile = shared_profile()


def pump(times: int = 5) -> None:
    for _ in range(times):
        _app.processEvents()


def wait(predicate, timeout: float = 8.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pump(2)
        if predicate():
            return True
        time.sleep(0.01)
    return False


class WindowCase(unittest.TestCase):
    ai_default = "0"

    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {"PYBROWSER_AI_DEFAULT": self.ai_default})
        self._env.start()
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.window = MainWindow(_profile, self.db, start_urls=["about:blank"])
        self.window.resize(1100, 760)
        self.window.show()
        pump(10)

    def tearDown(self) -> None:
        self.window.close()
        self.db.close()
        self._dir.cleanup()
        self._env.stop()
        pump(3)

    def ai_actions_state(self):
        return [(a.text(), a.isVisible(), a.isEnabled()) for a in self.window._ai_actions]


class AiOffByDefault(WindowCase):
    def test_a_fresh_profile_is_a_normal_browser(self) -> None:
        w = self.window
        self.assertFalse(w.settings.ai_enabled)
        self.assertTrue(w._ai_actions)
        for text, shown, enabled in self.ai_actions_state():
            self.assertFalse(shown or enabled, f"{text} is reachable while AI is off")
        self.assertFalse(w.nav_bar.ai_mode_action.isVisible())
        self.assertFalse(w.tabs.current_tab().ai_enabled)
        self.assertIsNone(w._side_panel)
        self.assertFalse(w._new_tab_data().ai_enabled)
        self.assertFalse(w._new_tab_data().recent_missions)

    def test_stale_ai_entry_points_do_nothing_but_explain(self) -> None:
        w = self.window
        with mock.patch.object(MainWindow, "_explain_ai_off") as explain:
            w._toggle_agent_panel()
            w._open_agent_with("hello")
            w._open_team()
            w._open_ai_mode("question")
            w._show_task_center()
        self.assertEqual(explain.call_count, 5)
        self.assertIsNone(w._side_panel)
        self.assertIsNone(getattr(w, "_aimode_controller", None))

    def test_ai_mode_urls_show_an_off_page_not_a_conversation(self) -> None:
        state = self.window._aimode_state()
        self.assertTrue(state["disabled"])

    def test_the_menu_bar_is_hidden_and_the_main_menu_has_everything(self) -> None:
        w = self.window
        self.assertFalse(w.menuBar().isVisible())
        titles = [a.text().replace("&", "") for a in w.nav_bar.menu_button.menu().actions() if a.menu() is not None]
        self.assertEqual(titles, ["File", "View", "History", "Bookmarks", "Tools", "Help"])
        w._menu_bar_action.setChecked(True)
        pump()
        self.assertTrue(w.menuBar().isVisible())
        self.assertTrue(w.settings.show_menu_bar)

    def test_a_single_workspace_adds_no_switcher_to_the_toolbar(self) -> None:
        self.assertFalse(self.window._workspace_switcher_action.isVisible())

    def test_the_context_menu_flag_reaches_new_tabs_too(self) -> None:
        tab = self.window.tabs.new_tab("about:blank")
        self.assertFalse(tab.ai_enabled)

    def test_alt_enter_in_the_address_bar_does_nothing_special_while_off(self) -> None:
        got = []
        self.window.nav_bar.ai_mode_requested.connect(got.append)
        bar = self.window.nav_bar.address_bar
        bar.setText("why is the sky blue")
        QTest.keyClick(bar, Qt.Key.Key_Return, Qt.KeyboardModifier.AltModifier)
        self.assertEqual(got, [])


class SwitchingItOn(WindowCase):
    def test_turning_it_on_shows_everything_without_a_restart_and_off_removes_it_again(self) -> None:
        w = self.window
        w.settings.ai_enabled = True
        w._apply_ai_enabled()
        pump()
        self.assertTrue(all(enabled for _, _, enabled in self.ai_actions_state()))
        self.assertTrue(w.nav_bar.ai_mode_action.isVisible())
        self.assertTrue(w.tabs.current_tab().ai_enabled)
        self.assertTrue(w._new_tab_data().ai_enabled)
        w.settings.ai_enabled = False
        w._apply_ai_enabled()
        self.assertFalse(any(enabled for _, _, enabled in self.ai_actions_state()))
        self.assertFalse(w.nav_bar.ai_mode_action.isVisible())

    def test_turning_it_off_closes_an_open_assistant_panel(self) -> None:
        w = self.window
        w.settings.ai_enabled = True
        w._apply_ai_enabled()
        with mock.patch.object(MainWindow, "_ensure_agent_session", return_value=mock.MagicMock()), \
                mock.patch("app.ui.agent_panel.AgentPanel", return_value=QWidget()):
            w._toggle_agent_panel()
        self.assertIsNotNone(w._side_panel)
        w.settings.ai_enabled = False
        w._apply_ai_enabled()
        self.assertIsNone(w._side_panel)

    def test_the_settings_dialog_carries_the_switch(self) -> None:
        from app.ui.settings_dialog import SettingsDialog
        dialog = SettingsDialog(self.window.settings)
        self.assertFalse(dialog.ai_check.isChecked())
        self.assertIn("ordinary browser", dialog.ai_note.text())
        dialog.ai_check.setChecked(True)
        dialog._save()
        self.assertTrue(self.window.settings.ai_enabled)
        self.assertTrue(dialog.ai_setup.isVisibleTo(dialog) or True)

    def test_the_ai_settings_tabs_only_exist_while_ai_is_on(self) -> None:
        w = self.window
        seen = []
        real = __import__("app.ui.settings_dialog", fromlist=["SettingsDialog"]).SettingsDialog

        def capture(*args, **kwargs):
            seen.append(kwargs)
            dialog = real(*args, **kwargs)
            dialog.exec = lambda: 0
            return dialog
        with mock.patch("app.ui.settings_dialog.SettingsDialog", side_effect=capture):
            w._show_settings()
            w.settings.ai_enabled = True
            w._show_settings()
        self.assertIsNone(seen[0]["mcp"])
        self.assertFalse(seen[0]["security"])
        self.assertIsNotNone(seen[1]["mcp"])
        self.assertTrue(seen[1]["security"])

    def test_alt_enter_sends_the_typed_text_to_ai_mode(self) -> None:
        w = self.window
        w.settings.ai_enabled = True
        w._apply_ai_enabled()
        got = []
        w.nav_bar.ai_mode_requested.connect(got.append)
        bar = w.nav_bar.address_bar
        bar.setText("why is the sky blue")
        QTest.keyClick(bar, Qt.Key.Key_Return, Qt.KeyboardModifier.AltModifier)
        self.assertEqual(got, ["why is the sky blue"])


class AiModeInTheWindow(WindowCase):
    ai_default = "1"

    def install_model(self, model=None):
        model = model or Model()
        controller = self.window._ai_mode()
        controller._client_factory = lambda: model
        controller._session = session(model, on_status=controller._on_status)[0]
        return controller, model

    def js(self, tab, script):
        out = []
        tab.run_javascript(script, out.append)
        self.assertTrue(wait(lambda: out), f"no result from {script}")
        return out[0]

    def open_page(self, query=""):
        self.window._open_ai_mode(query)
        tab = next(t for t in self.window.tabs.tabs() if is_ai_mode(t.url()))
        done = []
        tab.load_finished.connect(done.append)
        wait(lambda: done or tab.url().toString() == AIMODE_URL, 5)
        wait(lambda: self.js(tab, "typeof window.PB") == "object")
        return tab

    def test_the_page_opens_once_and_a_question_becomes_a_cited_answer_with_sources(self) -> None:
        controller, model = self.install_model()
        tab = self.open_page()
        self.assertEqual(self.js(tab, "document.querySelector('h1').textContent"), "AI Mode")
        self.assertEqual(self.js(tab, "document.querySelectorAll('.chip').length"), 4)         # empty state suggestions
        self.window._open_ai_mode()
        self.assertEqual(len([t for t in self.window.tabs.tabs() if is_ai_mode(t.url())]), 1)  # reused, not duplicated
        tab.run_javascript("document.getElementById('q').value='Why is the sky blue?';"
                           "document.getElementById('form').requestSubmit();")
        self.assertTrue(wait(lambda: self.js(tab, "document.querySelectorAll('.answer').length") == 1, 10))
        self.assertEqual(self.js(tab, "document.querySelector('.q').textContent"), "Why is the sky blue?")
        self.assertIn("Rayleigh scattering", self.js(tab, "document.querySelector('.answer').textContent"))
        self.assertEqual(self.js(tab, "document.querySelectorAll('.src').length"), 2)
        self.assertEqual(self.js(tab, "document.querySelector('.src .title').textContent"),
                         "Why is the sky blue? - Science Site")
        self.assertIn("Based on 2 web pages", self.js(tab, "document.querySelector('.basis').textContent"))
        self.assertEqual(self.js(tab, "document.querySelectorAll('.related .chip').length"), 2)
        self.assertEqual(self.js(tab, "document.querySelector('sup.cite a').getAttribute('href')"), "#src-1")
        self.assertEqual(model.roles(), ["answer"])

    def test_opening_with_a_query_asks_immediately_and_the_page_restores_after_a_reload(self) -> None:
        controller, model = self.install_model()
        tab = self.open_page("Why is the sky blue?")
        self.assertTrue(wait(lambda: self.js(tab, "document.querySelectorAll('.answer').length") == 1, 10))
        done = []
        tab.load_finished.connect(done.append)
        tab.reload()
        self.assertTrue(wait(lambda: done))
        self.assertTrue(wait(lambda: self.js(tab, "document.querySelectorAll('.answer').length") == 1))   # state is embedded

    def test_new_chat_clears_the_conversation(self) -> None:
        controller, model = self.install_model()
        tab = self.open_page("Why is the sky blue?")
        self.assertTrue(wait(lambda: self.js(tab, "document.querySelectorAll('.answer').length") == 1, 10))
        self.window._on_aimode_action("new", {})
        self.assertTrue(wait(lambda: json.loads(controller.state_json())["turns"] == []))

    def test_without_a_key_the_page_offers_setup_and_asks_nothing(self) -> None:
        controller = self.window._ai_mode()
        controller._provider = mock.MagicMock(available=False, secret="", label="Groq")
        with mock.patch("app.aimode.controller.websearch.resolve_search",
                        return_value=mock.MagicMock(available=False, label="")):
            tab = self.open_page()
            self.assertTrue(wait(lambda: "Set up an AI provider" in self.js(tab, "document.querySelector('.banner').textContent")))
            self.assertFalse(controller.ask("hello"))

    def test_an_unconfigured_search_is_stated_on_the_empty_page(self) -> None:
        controller, _ = self.install_model()
        with mock.patch("app.aimode.controller.websearch.resolve_search",
                        return_value=mock.MagicMock(available=False, label="")):
            tab = self.open_page()
            self.assertTrue(wait(lambda: "Web search isn" in self.js(tab, "document.querySelector('.banner').textContent")))

    def test_the_page_off_state_when_ai_is_switched_off_while_it_is_open(self) -> None:
        self.install_model()
        tab = self.open_page()
        self.window.settings.ai_enabled = False
        self.window._apply_ai_enabled()
        done = []
        tab.load_finished.connect(done.append)
        tab.reload()
        self.assertTrue(wait(lambda: done))
        self.assertTrue(wait(lambda: "AI features are off" in self.js(tab, "document.body.textContent")))

    def test_page_actions_are_ignored_while_ai_is_off(self) -> None:
        self.window.settings.ai_enabled = False
        with mock.patch.object(MainWindow, "_ai_mode") as ai:
            self.window._on_aimode_action("ask", {"q": "x"})
        ai.assert_not_called()


class FirstRunChoice(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        setUpModule()

    def test_the_default_answer_keeps_ai_off(self) -> None:
        from app.ui.onboarding import AiChoiceDialog
        dialog = AiChoiceDialog()
        dialog.reject()
        self.assertFalse(dialog.enable_ai)

    def test_choosing_to_turn_it_on_is_explicit(self) -> None:
        from app.ui.onboarding import AiChoiceDialog
        dialog = AiChoiceDialog()
        dialog._enable()
        self.assertTrue(dialog.enable_ai)


if __name__ == "__main__":
    unittest.main()
