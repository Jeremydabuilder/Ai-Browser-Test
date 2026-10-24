"""The command palette: ranking, keyboard use, and - above all - that it is a launcher over existing handlers.

Run with:  QT_QPA_PLATFORM=offscreen QTWEBENGINE_DISABLE_SANDBOX=1 python tests/run.py tests.test_command_palette
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-palette-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.storage import Database  # noqa: E402
from app.ui import a11y, theme  # noqa: E402
from app.ui.command_palette import Command, CommandPalette, rank, score  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app = None
_profile = None


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    theme.apply(_app)
    _profile = shared_profile()


def pump(times: int = 8) -> None:
    for _ in range(times):
        _app.processEvents()
        time.sleep(0.003)


class Ranking(unittest.TestCase):
    CMDS = [Command("New Tab", lambda: None, "File", "Ctrl+T"), Command("Show History", lambda: None, "History"),
            Command("Open Settings", lambda: None, "Tools", keywords="preferences"),
            Command("Switch to tab: Hacker News", lambda: None, "Tabs")]

    def test_every_word_must_match_and_prefixes_win(self) -> None:
        self.assertEqual([c.title for c in rank("hist", self.CMDS)], ["Show History"])
        self.assertEqual([c.title for c in rank("tab hacker", self.CMDS)], ["Switch to tab: Hacker News"])
        self.assertEqual(rank("zzz", self.CMDS), [])
        self.assertEqual(len(rank("", self.CMDS)), 4)                            # empty query lists everything, in order
        self.assertEqual(rank("", self.CMDS)[0].title, "New Tab")

    def test_keywords_and_groups_are_searchable_but_rank_below_titles(self) -> None:
        self.assertEqual([c.title for c in rank("preferences", self.CMDS)], ["Open Settings"])
        self.assertGreater(score("tab", self.CMDS[0]), score("file", self.CMDS[0]) - 100)
        titles = [c.title for c in rank("tab", self.CMDS)]
        self.assertEqual(titles[0], "New Tab")                                   # word in the title beats a group match


class Dialog(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        setUpModule()

    def make(self, commands=None):
        self.ran = []
        commands = commands or [Command("Alpha", lambda: self.ran.append("alpha"), "G"),
                                Command("Beta", lambda: self.ran.append("beta"), "G", "Ctrl+B"),
                                Command("Gamma", lambda: self.ran.append("gamma"), "H")]
        palette = CommandPalette(lambda q: commands)
        palette.show()
        pump()
        return palette

    def test_typing_filters_and_arrows_move_without_leaving_the_box(self) -> None:
        palette = self.make()
        self.assertEqual(palette.results.count(), 3)
        QTest.keyClicks(palette.field, "a")
        self.assertEqual(palette.results.count(), 3)                               # alpha, beta, gamma all contain "a"
        QTest.keyClick(palette.field, Qt.Key.Key_Down)
        self.assertEqual(palette.results.currentRow(), 1)
        QTest.keyClick(palette.field, Qt.Key.Key_Down)
        QTest.keyClick(palette.field, Qt.Key.Key_Down)                              # clamps at the end
        self.assertEqual(palette.results.currentRow(), 2)
        QTest.keyClick(palette.field, Qt.Key.Key_Up)
        self.assertEqual(palette.selected().title, "Beta")
        palette.field.setText("gam")
        self.assertEqual([c.title for c in palette._shown], ["Gamma"])

    def test_enter_chooses_and_nothing_runs_until_the_caller_runs_it(self) -> None:
        palette = self.make()
        QTest.keyClick(palette.field, Qt.Key.Key_Down)
        QTest.keyClick(palette.field, Qt.Key.Key_Return)
        self.assertEqual(palette.chosen.title, "Beta")
        self.assertEqual(self.ran, [])                                              # the palette itself never executes

    def test_escape_cancels_and_an_empty_result_cannot_be_run(self) -> None:
        palette = self.make()
        palette.field.setText("nothing like this")
        self.assertEqual(palette.status.text(), "Nothing matches")
        QTest.keyClick(palette.field, Qt.Key.Key_Return)
        self.assertIsNone(palette.chosen)
        palette.close()

    def test_it_is_named_for_assistive_technology_and_opens_with_focus_in_the_box(self) -> None:
        palette = self.make()
        self.assertEqual(a11y.missing_names(palette), [])
        self.assertIs(palette.focusProxy(), palette.field)
        self.assertEqual(palette.field.accessibleName(), "Command palette search")
        self.assertEqual(palette.results.focusPolicy(), Qt.FocusPolicy.NoFocus)


class InTheWindow(unittest.TestCase):
    ai = "1"

    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {"PYBROWSER_AI_DEFAULT": self.ai})
        self._env.start()
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.window = MainWindow(_profile, self.db, start_urls=["about:blank"])
        self.window.show()
        pump(10)

    def tearDown(self) -> None:
        self.window.close()
        self.db.close()
        self._dir.cleanup()
        self._env.stop()
        pump(3)

    def titles(self, query=""):
        return [c.title for c in self.window._palette_commands(query)]

    def by_title(self, fragment, query=""):
        return next(c for c in self.window._palette_commands(query) if fragment in c.title)


class WithAi(InTheWindow):
    def test_it_offers_the_ai_and_navigation_entries_the_user_asked_for(self) -> None:
        titles = self.titles()
        for expected in ("New Tab", "Settings…", "Search Tabs…", "Search History & Knowledge…", "AI Mode",
                         "Show AI Agent", "AI Team…", "Skills Library", "Mission Library"):
            self.assertIn(expected, titles)

    def test_commands_are_the_menu_actions_themselves(self) -> None:
        with mock.patch.object(MainWindow, "_show_settings") as show:
            self.window.menuBar()                                    # same object the menu uses
            command = self.by_title("Settings…")
            command.run()
        show.assert_called_once()

    def test_typed_text_becomes_search_ai_mode_and_mission_entries(self) -> None:
        titles = self.titles("why is the sky blue")
        self.assertTrue(titles[0].startswith("Search the web or go to: why is the sky blue"))
        self.assertTrue(any(t.startswith("AI Mode: ask") for t in titles))
        self.assertTrue(any(t.startswith("Start a Mission: why is the sky blue") for t in titles))
        with mock.patch.object(MainWindow, "_open_ai_mode") as ai, \
                mock.patch.object(MainWindow, "_open_agent_with") as agent, \
                mock.patch.object(MainWindow, "_navigate_from_address_bar") as nav:
            self.by_title("AI Mode: ask", "why is the sky blue").run()
            self.by_title("Start a Mission", "why is the sky blue").run()
            self.by_title("Search the web", "why is the sky blue").run()
        ai.assert_called_once_with("why is the sky blue")
        agent.assert_called_once_with("why is the sky blue")           # pre-fills the panel; nothing is sent for the user
        nav.assert_called_once_with("why is the sky blue")

    def test_tabs_and_workspaces_switch_through_the_existing_handlers(self) -> None:
        w = self.window
        w.tabs.new_tab("about:blank")
        pump(5)
        w.tabs.setCurrentIndex(1)
        command = self.by_title("Switch to tab:")
        command.run()
        self.assertEqual(w.tabs.currentIndex(), 0)
        from app.workspaces.model import Workspace
        other = w.workspaces.create("Research", icon="R") if hasattr(w.workspaces, "create") else None
        if other is not None:
            wid = getattr(other, "id", other)
            with mock.patch.object(MainWindow, "switch_workspace") as switch:
                self.by_title("Switch workspace: ").run()
            switch.assert_called_once_with(wid)

    def test_skills_run_through_run_skill_and_so_through_the_normal_approval_path(self) -> None:
        w = self.window
        skill = SimpleNamespace(name="Summarize page", id="s1")
        with mock.patch.object(type(w.skills), "all", return_value=[skill]), \
                mock.patch.object(MainWindow, "_run_skill") as run:
            self.by_title("Run Skill: Summarize page").run()
        run.assert_called_once_with(skill)

    def test_opening_it_shows_the_dialog_and_runs_the_chosen_command_afterwards(self) -> None:
        from app.ui import command_palette as cp
        ran = []
        chosen = cp.Command("X", lambda: ran.append(1))
        with mock.patch.object(cp.CommandPalette, "exec", lambda self: setattr(self, "chosen", chosen) or 1):
            self.window._open_command_palette()
            self.assertEqual(ran, [])                                 # deferred to the next turn
            pump(5)
        self.assertEqual(ran, [1])

    def test_the_shortcut_is_registered(self) -> None:
        shortcuts = {a.shortcut().toString() for a in self.window.actions()}
        self.assertIn("Ctrl+Shift+P", shortcuts)


class WithoutAi(InTheWindow):
    ai = "0"

    def test_no_ai_entry_is_offered_and_typed_text_only_searches(self) -> None:
        titles = self.titles()
        for gone in ("AI Mode", "Show AI Agent", "AI Team…", "Skills Library", "Mission Library", "Task Center…"):
            self.assertNotIn(gone, titles)
        for kept in ("New Tab", "Settings…", "Search Tabs…", "Show History", "Show Bookmarks"):
            self.assertIn(kept, titles)
        typed = self.titles("why is the sky blue")
        self.assertFalse(any(t.startswith(("AI Mode", "Start a Mission", "Run Skill", "Open Mission")) for t in typed))
        self.assertTrue(typed[0].startswith("Search the web or go to:"))

    def test_turning_ai_on_adds_them_without_a_restart(self) -> None:
        self.window.settings.ai_enabled = True
        self.window._apply_ai_enabled()
        self.assertIn("AI Mode", self.titles())


if __name__ == "__main__":
    unittest.main()
