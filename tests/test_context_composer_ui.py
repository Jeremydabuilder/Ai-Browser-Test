"""Universal @context wiring inside AgentPanel: the "@" popup, chip row,
vision-conflict warning, and composed send - see app/agent/context_items.py
for the Qt-free logic this drives.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_context_composer_ui -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-context-ui-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.config import AgentConfig, PROVIDER_ANTHROPIC, PROVIDER_GROQ  # noqa: E402
from app.agent.session import AgentSession  # noqa: E402
from app.browser.controller import BrowserController  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from app.ui.agent_panel import AgentPanel  # noqa: E402
from tests.fake_claude import ScriptedClaude, says  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_profile = None


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _profile = shared_profile()


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


class ContextComposerUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tabs = TabManager(_profile, "about:blank")
        self.tabs.resize(900, 700)
        self.tabs.show()
        self.browser = BrowserController(self.tabs)
        self.browser.open_tab("about:blank").wait()
        self.session: AgentSession | None = None
        self.panel: AgentPanel | None = None

    def tearDown(self) -> None:
        if self.session is not None:
            self.session.shutdown()
        if self.panel is not None:
            self.panel.deleteLater()
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        _app.processEvents()

    def start(self, script, *, provider: str = PROVIDER_ANTHROPIC) -> AgentPanel:
        self.session = AgentSession(self.browser, ScriptedClaude(script),
                                    AgentConfig(provider=provider))
        self.panel = AgentPanel(self.session, browser=self.browser)
        return self.panel

    def type_at(self, panel: AgentPanel, text: str) -> None:
        """setPlainText() alone resets the cursor to position 0 - not where
        a real keystroke would leave it - so mention_query() would never see
        the "@" a person just typed. Move the cursor to the end and re-run
        the same check setPlainText's textChanged already triggered once
        (uselessly, from position 0)."""
        from PySide6.QtGui import QTextCursor

        panel.input.setPlainText(text)
        cursor = panel.input.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        panel.input.setTextCursor(cursor)
        panel._update_mentions()

    # -- popup: appears, filters, disappears ------------------------------
    def test_typing_at_opens_the_popup_with_the_two_actions_present(self) -> None:
        panel = self.start([says("ok")])
        self.type_at(panel, "@")
        self.assertFalse(panel._mention_popup.isHidden())
        titles = [c.title for c in panel._mention_candidates]
        self.assertIn("Attach a local file…", titles)
        self.assertIn("Attach an image…", titles)

    def test_typing_a_space_after_at_closes_the_popup(self) -> None:
        panel = self.start([says("ok")])
        self.type_at(panel, "hello @")
        self.assertFalse(panel._mention_popup.isHidden())
        self.type_at(panel, "hello @ ")
        self.assertTrue(panel._mention_popup.isHidden())

    def test_escape_hides_the_popup(self) -> None:
        panel = self.start([says("ok")])
        self.type_at(panel, "@")
        self.assertFalse(panel._mention_popup.isHidden())
        panel._hide_mention_popup()
        self.assertTrue(panel._mention_popup.isHidden())
        self.assertFalse(panel.input.mention_active)

    def test_a_query_filters_candidates_to_matching_tabs(self) -> None:
        panel = self.start([says("ok")])
        second = self.tabs.new_tab("about:blank")
        second.page.runJavaScript("document.title = 'Weather Report';")
        self.assertTrue(pump(lambda: second.title() == "Weather Report"))
        self.type_at(panel, "@weather")
        titles = [c.title for c in panel._mention_candidates]
        self.assertIn("Weather Report", titles)
        self.assertNotIn("Attach a local file…", titles)

    # -- selection: chips, duplicates, removal ----------------------------
    def test_selecting_a_tab_candidate_adds_a_chip_and_clears_the_at_token(self) -> None:
        panel = self.start([says("ok")])
        self.type_at(panel, "@")
        candidate = next(c for c in panel._mention_candidates if c.kind == "tab")
        panel._select_mention(candidate)
        self.assertEqual(len(self.panel_selected()), 1)
        self.assertFalse(panel.context_chips.isHidden())
        self.assertEqual(panel.input.toPlainText(), "")

    def test_selecting_the_same_tab_twice_does_not_duplicate_the_chip(self) -> None:
        panel = self.start([says("ok")])
        self.type_at(panel, "@")
        candidate = next(c for c in panel._mention_candidates if c.kind == "tab")
        panel._select_mention(candidate)
        self.type_at(panel, "@")
        candidate_again = next(c for c in panel._mention_candidates if c.kind == "tab")
        panel._select_mention(candidate_again)
        self.assertEqual(len(self.panel_selected()), 1)

    def test_removing_a_chip_deselects_it(self) -> None:
        panel = self.start([says("ok")])
        self.type_at(panel, "@")
        candidate = next(c for c in panel._mention_candidates if c.kind == "tab")
        panel._select_mention(candidate)
        item_id = self.panel_selected()[0].id
        panel._remove_chip(item_id)
        self.assertEqual(self.panel_selected(), [])
        self.assertTrue(panel.context_chips.isHidden())

    def test_clear_all_button_appears_with_two_or_more_chips_and_clears_them(self) -> None:
        panel = self.start([says("ok")])
        second = self.tabs.new_tab("about:blank")
        self.type_at(panel, "@")
        tab_candidates = [c for c in panel._mention_candidates if c.kind == "tab"]
        self.assertGreaterEqual(len(tab_candidates), 2)
        panel._select_mention(tab_candidates[0])
        self.type_at(panel, "@")
        tab_candidates = [c for c in panel._mention_candidates if c.kind == "tab"]
        panel._select_mention(next(c for c in tab_candidates if c not in self.panel_selected()))
        self.assertEqual(len(self.panel_selected()), 2)
        clear_button = next(
            panel._context_chip_flow.itemAt(i).widget()
            for i in range(panel._context_chip_flow.count())
            if panel._context_chip_flow.itemAt(i).widget().text() == "Clear all")
        clear_button.click()
        self.assertEqual(self.panel_selected(), [])

    def test_a_closed_tab_no_longer_appears_as_a_candidate(self) -> None:
        panel = self.start([says("ok")])
        second = self.tabs.new_tab("about:blank")
        self.tabs.close_tab(self.tabs.indexOf(second))
        self.type_at(panel, "@")
        ids = [c.id for c in panel._mention_candidates]
        self.assertEqual(len([i for i in ids if i.startswith("tab:")]), 1)

    def panel_selected(self):
        return self.panel._composer.selected

    # -- vision capability -------------------------------------------------
    def test_an_image_chip_shows_a_warning_for_a_non_vision_provider(self) -> None:
        from app.agent.context_items import ContextItem

        panel = self.start([says("ok")], provider=PROVIDER_GROQ)
        panel._composer.add(ContextItem(id="image:1", kind="image", title="pic.png",
                                        requires_vision=True,
                                        ref={"mime_type": "image/png", "data": "AAAA"}))
        panel._refresh_context_ui()
        self.assertFalse(panel.vision_warning.isHidden())
        self.assertIn("pic.png", panel.vision_warning.text())
        # Still selected, not silently dropped.
        self.assertEqual(len(self.panel_selected()), 1)

    def test_no_warning_when_the_provider_supports_images(self) -> None:
        from app.agent.context_items import ContextItem

        panel = self.start([says("ok")], provider=PROVIDER_ANTHROPIC)
        panel._composer.add(ContextItem(id="image:1", kind="image", title="pic.png",
                                        requires_vision=True,
                                        ref={"mime_type": "image/png", "data": "AAAA"}))
        panel._refresh_context_ui()
        self.assertTrue(panel.vision_warning.isHidden())

    # -- sending -------------------------------------------------------
    def test_sending_with_a_selected_tab_composes_the_context_into_the_message(self) -> None:
        panel = self.start([says("Summary.")])
        self.type_at(panel, "@")
        candidate = next(c for c in panel._mention_candidates if c.kind == "tab")
        panel._select_mention(candidate)
        done = []
        self.session.finished.connect(lambda: done.append(True))
        panel.input.setPlainText("Summarise this")
        panel._send()
        self.assertTrue(pump(lambda: done))
        sent = self.session.messages[0]["content"]
        self.assertIn("browser_get_page_text", sent)
        self.assertTrue(sent.endswith("Summarise this"))

    def test_sending_clears_the_selection_and_chips(self) -> None:
        panel = self.start([says("Summary.")])
        self.type_at(panel, "@")
        candidate = next(c for c in panel._mention_candidates if c.kind == "tab")
        panel._select_mention(candidate)
        done = []
        self.session.finished.connect(lambda: done.append(True))
        panel.input.setPlainText("Summarise this")
        panel._send()
        self.assertTrue(pump(lambda: done))
        self.assertEqual(self.panel_selected(), [])
        self.assertTrue(panel.context_chips.isHidden())

    def test_sending_with_no_selection_behaves_exactly_as_before(self) -> None:
        panel = self.start([says("plain answer")])
        done = []
        self.session.finished.connect(lambda: done.append(True))
        panel.input.setPlainText("just a question")
        panel._send()
        self.assertTrue(pump(lambda: done))
        self.assertEqual(self.session.messages[0]["content"], "just a question")


if __name__ == "__main__":
    unittest.main()
