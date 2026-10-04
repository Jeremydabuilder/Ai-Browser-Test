"""The Team panel and controller, driven for real: a mission is typed in,
started with the button, run by the engine on its own thread against a
scripted provider, and rendered back through the controller's dispatcher.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_team_ui -v
"""

from __future__ import annotations

import gc
import os
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QScrollBar, QWidget  # noqa: E402

from app.agent.credentials import Credential, Mode  # noqa: E402
from app.storage.database import Database  # noqa: E402
from app.storage.team_store import TeamStore  # noqa: E402
from app.team.model import (  # noqa: E402
    AgentId, Artifact, ArtifactKind, Mission, MissionStatus, Source, SourceKind, SourceStatus, Task,
    TaskStatus,
)
from app.team.runner import TeamController, collect_tab_sources, paste_source  # noqa: E402
from app.ui import theme  # noqa: E402
from app.ui.team_panel import TeamPanel  # noqa: E402
from tests.test_team_engine import (  # noqa: E402
    APPROVE, RESEARCH_WRITE_REVIEW, FakeClient, plan_json, task,
)

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    theme.apply(_app)


def pump(times: int = 5) -> None:
    for _ in range(times):
        _app.processEvents()


def wait_for(condition, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pump(2)
        if condition():
            return True
        time.sleep(0.01)
    return False


def happy_client(**overrides) -> FakeClient:
    script = {"plan": [RESEARCH_WRITE_REVIEW], "researcher": ["Findings: A is $10 [S1]"],
              "writer": ["# Comparison\nA is cheap [S1]"], "reviewer": [APPROVE],
              "final": ["**Buy A.** [S1]"]}
    script.update(overrides)
    return FakeClient(script)


class TeamUITestCase(unittest.TestCase):
    def build(self, client=None, browser=None, settings=None, **controller_kwargs) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.store = TeamStore(self.db)
        self.client = client or happy_client()
        self.controller = TeamController(self.store, settings, None, client_factory=lambda: self.client,
                                         **controller_kwargs)
        self.panel = TeamPanel(self.controller, browser)
        self.panel.resize(380, 700)
        self.panel.show()
        pump()

    def tearDown(self) -> None:
        if hasattr(self, "controller"):
            self.controller.shutdown()
            self.panel.hide()
            self.panel.deleteLater()
            self.controller.deleteLater()
            pump(3)
            self.db.close()
            self._dir.cleanup()
            gc.collect()

    def start_mission(self, goal="Compare the products", text="Product A costs $10 and is great.") -> None:
        self.panel._sources.append(paste_source(self.panel._sources, text))
        self.panel._refresh_included()
        self.panel.goal.setPlainText(goal)
        self.assertTrue(self.panel.start_button.isEnabled())
        self.panel.start_button.click()


class FlowThroughTheUITests(TeamUITestCase):
    def test_a_mission_runs_end_to_end_and_every_section_shows_real_state(self) -> None:
        self.build()
        self.start_mission()
        self.assertTrue(wait_for(lambda: (self.controller.snapshot() or SimpleNamespace(status="")).status
                                 == MissionStatus.COMPLETED), "mission never completed")
        pump(10)
        mission = self.controller.snapshot()
        self.assertEqual(self.panel.pages.currentIndex(), 1)

        # Agent cards: actual status per agent, unused agents say so.
        cards = [self.panel._agents_box.itemAt(i).widget().text() for i in range(6)]
        by_agent = dict(zip(AgentId.ALL, cards))
        self.assertIn("done", by_agent[AgentId.COORDINATOR])
        self.assertIn("1 task(s) finished", by_agent[AgentId.RESEARCHER])
        self.assertIn("not needed", by_agent[AgentId.CODER])
        self.assertIn("not needed", by_agent[AgentId.TESTER])

        # Task board with dependencies.
        tasks_text = " ".join(self.panel._tasks_box.itemAt(i).widget().text()
                              for i in range(self.panel._tasks_box.count() - 1))
        self.assertIn("after T1", tasks_text)
        self.assertIn("after T2", tasks_text)
        self.assertIn("received", tasks_text)       # explicit artifact handoffs are visible

        # Live activity feed.
        activity = self.panel.activity.toPlainText()
        self.assertIn("Plan ready", activity)
        self.assertIn("Coordinator", activity)

        # Results: the final answer is selected first, with sources and per-agent artifacts to browse.
        self.assertEqual(self.panel.viewer_choice.itemText(0), "Final result")
        self.assertIn("Buy A", self.panel.viewer.toPlainText())
        keys = [self.panel.viewer_choice.itemData(i) for i in range(self.panel.viewer_choice.count())]
        self.assertIn("__sources__", keys)
        self.assertGreaterEqual(len(keys), 6)
        self.assertEqual(self.panel.run_tabs.currentIndex(), 3)        # jumped to Results when done

        # Saved history.
        self.assertEqual(self.panel.history_list.count(), 1)
        self.assertIn("Compare the products", self.panel.history_list.item(0).text())
        self.assertFalse(self.panel.cancel_button.isVisibleTo(self.panel))
        self.assertTrue(self.panel.new_button.isVisibleTo(self.panel))
        self.assertEqual(mission.status, MissionStatus.COMPLETED)

    def test_starting_is_disabled_without_a_mission_and_while_one_runs(self) -> None:
        gate = threading.Event()
        client = happy_client()
        client.hold = {"researcher": gate}
        self.build(client)
        self.assertFalse(self.panel.start_button.isEnabled())
        self.start_mission()
        self.assertTrue(wait_for(lambda: self.controller.is_running))
        self.panel.goal.setPlainText("another")
        pump()
        self.assertFalse(self.panel.start_button.isEnabled())
        self.assertIsNone(self.controller.start("second", []))      # a second start is refused
        self.controller.cancel()
        gate.set()
        self.assertTrue(wait_for(lambda: not self.controller.is_running))

    def test_cancel_and_retry_controls_work_from_the_panel(self) -> None:
        gate = threading.Event()
        client = happy_client()
        client.hold = {"researcher": gate}
        self.build(client)
        self.start_mission()
        self.assertTrue(wait_for(lambda: self.panel.cancel_button.isVisibleTo(self.panel)))
        self.assertTrue(wait_for(lambda: "researcher" in client.entered))
        self.panel.cancel_button.click()
        gate.set()
        self.assertTrue(wait_for(lambda: (self.controller.snapshot() or SimpleNamespace(status="")).status
                                 == MissionStatus.CANCELLED), "never reached cancelled")
        self.assertNotIn("writer", client.roles())
        pump(10)
        self.assertTrue(self.panel.retry_button.isVisibleTo(self.panel))
        self.assertFalse(self.panel.cancel_button.isVisibleTo(self.panel))
        client.hold = {}
        self.panel.retry_button.click()
        self.assertTrue(wait_for(lambda: self.controller.snapshot().status == MissionStatus.COMPLETED),
                        "retry did not finish the mission")
        self.assertIn("writer", client.roles())

    def test_a_failure_is_shown_with_its_reason_and_offers_retry(self) -> None:
        from app.agent.claude_client import ClaudeError
        client = happy_client(plan=[ClaudeError("Groq rejected the API key. Check it in Tools → Configure AI Agent.")])
        self.build(client)
        self.start_mission()
        self.assertTrue(wait_for(lambda: self.controller.snapshot().status == MissionStatus.FAILED))
        pump(10)
        self.assertIn("rejected the API key", self.panel.run_status.text())
        self.assertTrue(self.panel.retry_button.isVisibleTo(self.panel))


class EnvironmentHonestyTests(TeamUITestCase):
    def test_a_missing_credential_is_explained_with_a_way_to_fix_it(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        none = Credential(Mode.NONE, "no Groq credential configured", provider="groq")
        with patch("app.agent.credentials.resolve_for", return_value=none):
            self.controller = TeamController(TeamStore(self.db), None, None)
            self.panel = TeamPanel(self.controller)
            self.panel.show()
            pump()
            text = self.panel.env.text()
            self.assertIn("not set up", text)
            self.assertIn("GROQ_API_KEY", self.panel.env.toolTip())      # the full instructions are one hover away
            self.assertTrue(self.panel.configure_button.isVisibleTo(self.panel))
            self.assertEqual(self.panel.configure_button.text(), "Set up Groq…")
            seen = []
            self.panel.configure_requested.connect(lambda: seen.append(1))
            self.panel.configure_button.click()
            self.assertEqual(seen, [1])
            self.panel.goal.setPlainText("anything")
            self.panel.start_button.click()
            pump()
            # Nothing is started or saved: the reason is shown right where the user is looking.
            self.assertTrue(self.panel.compose_error.isVisibleTo(self.panel))
            self.assertIn("GROQ_API_KEY", self.panel.compose_error.text())
            self.assertIsNone(self.controller.snapshot())
            self.assertEqual(self.controller.history(), [])
            self.assertEqual(self.panel.pages.currentIndex(), 0)

    def test_a_working_provider_hides_the_setup_prompt_and_shows_the_model(self) -> None:
        self.build()
        self.assertFalse(self.panel.configure_button.isVisibleTo(self.panel))
        self.assertIn("scripted", self.panel.env.text())
        self.assertIn("Web: off", self.panel.env.text())    # limitations are shown up front
        self.assertIn("Sandbox:", self.panel.env.text())


class FakeBrowser:
    """Just enough BrowserController for collect_tab_sources."""

    class Future:
        def __init__(self, result):
            self.result = result

        def then(self, callback):
            callback(self.result)
            return self

    def __init__(self, tabs, texts):
        self._tabs, self._texts = tabs, texts

    def list_tabs(self):
        return self._tabs

    def get_page_text(self, tab_id, max_chars=20000):
        text = self._texts[tab_id]
        if isinstance(text, Exception):
            return self.Future(SimpleNamespace(ok=False, data={}, error=SimpleNamespace(message=str(text))))
        return self.Future(SimpleNamespace(ok=True, data={"text": text, "truncated": False}, error=None))

    def get_pdf_text(self, tab_id):
        return SimpleNamespace(ok=True, data={"text": "pdf words", "truncated": False}, error=None)


class AttachedSourcesTests(TeamUITestCase):
    def test_tabs_are_read_through_the_browser_and_unreadable_ones_are_reported_honestly(self) -> None:
        tabs = [
            {"tab_id": 1, "title": "Good page", "url": "https://good.example/a"},
            {"tab_id": 2, "title": "New Tab", "url": "pybrowser://newtab"},
            {"tab_id": 3, "title": "Login wall", "url": "https://bank.example/"},
            {"tab_id": 4, "title": "Blank", "url": "https://blank.example/"},
            {"tab_id": 5, "title": "Paper", "url": "https://x.example/paper.pdf"},
            {"tab_id": 9, "title": "Gone", "url": "https://gone.example/"},
        ]
        browser = FakeBrowser(tabs[:5], {1: "Hello world", 3: RuntimeError("Reading the page took too long."), 4: "   "})
        got = []
        collect_tab_sources(browser, [1, 2, 3, 4, 5, 9], [], got.append)
        sources = got[0]
        self.assertEqual([s.id for s in sources], ["S1", "S2", "S3", "S4", "S5", "S6"])
        by_title = {s.title: s for s in sources}
        self.assertTrue(by_title["Good page"].usable)
        self.assertEqual(by_title["New Tab"].status, SourceStatus.INACCESSIBLE)
        self.assertIn("internal", by_title["New Tab"].error)
        self.assertIn("took too long", by_title["Login wall"].error)
        self.assertEqual(by_title["Blank"].status, SourceStatus.INACCESSIBLE)
        self.assertIn("no readable text", by_title["Blank"].error)
        self.assertTrue(by_title["Paper"].usable)
        self.assertEqual(sources[5].error, "That tab was closed.")
        self.assertTrue(all(s.text == "" for s in sources if not s.usable))     # nothing unreadable is ever sent on

    def test_the_included_list_marks_each_page_and_warns_about_the_ones_left_out(self) -> None:
        self.build()
        self.panel._sources = [
            Source("S1", SourceKind.TAB, "Good page", "https://good.example/", "words words"),
            Source("S2", SourceKind.TAB, "Login wall", "https://bank.example/", "", SourceStatus.INACCESSIBLE,
                   "The page has no readable text"),
        ]
        self.panel._refresh_included()
        texts = [self.panel.included.item(i).text() for i in range(self.panel.included.count())]
        self.assertTrue(texts[0].startswith("✓"))
        self.assertTrue(texts[1].startswith("✗"))
        self.assertIn("not included", texts[1])
        self.assertIn("1 page(s) could not be read and will NOT be sent", self.panel.included_note.text())

    def test_pasted_text_and_empty_paste_are_handled(self) -> None:
        self.assertTrue(paste_source([], "some text").usable)
        empty = paste_source([], "   ")
        self.assertFalse(empty.usable)
        self.assertEqual(empty.status, SourceStatus.INACCESSIBLE)


class KnowledgeSearchAcrossThreadsTests(TeamUITestCase):
    def test_the_engine_thread_searches_the_real_index_through_the_gui_dispatcher(self) -> None:
        from app.knowledge.embeddings import embed
        from app.knowledge.types import Chunk
        text = "The extended warranty on Widget B covers three years of accidental damage."
        chunk = Chunk(1, "history", "h1", 0, text, "hash", "2026-01-01T00:00:00+00:00", tuple(embed(text)),
                      "2026-01-01T00:00:00+00:00", title="Widget B warranty note", location="https://b.example/warranty")
        knowledge = SimpleNamespace(enabled=True, store=SimpleNamespace(all_chunks=lambda: [chunk]))
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.store = TeamStore(self.db)
        client = happy_client(researcher=["Warranty is 3 years [S1]"])
        original = threading.current_thread()
        self.controller = TeamController(self.store, None, knowledge, client_factory=lambda: client)
        self.panel = TeamPanel(self.controller)
        self.panel.show()
        pump()
        self.assertIn("Knowledge: on", self.panel.env.text())
        self.panel.goal.setPlainText("What warranty does Widget B have?")
        self.panel.start_button.click()
        self.assertTrue(wait_for(lambda: self.controller.snapshot().status == MissionStatus.COMPLETED))
        mission = self.controller.snapshot()
        found = mission.source("S1")
        self.assertEqual(found.kind, SourceKind.KNOWLEDGE)
        self.assertEqual(found.url, "https://b.example/warranty")
        self.assertIn("three years", client.users("researcher")[0])
        self.assertTrue(any(e.kind == "tool" for e in mission.events))
        self.assertIs(threading.current_thread(), original)
        final = mission.artifact(mission.final_artifact_id).content
        self.assertIn("not the open web", final)       # the limit of what was searched is stated

class HistoryTests(TeamUITestCase):
    def test_saved_missions_can_be_reopened_deleted_and_survive_a_restart(self) -> None:
        self.build()
        self.start_mission(goal="First mission")
        self.assertTrue(wait_for(lambda: self.controller.snapshot().status == MissionStatus.COMPLETED))
        pump(10)
        self.panel.new_button.click()
        pump()
        self.assertEqual(self.panel.pages.currentIndex(), 0)                  # back to compose
        self.panel.top_tabs.setCurrentIndex(1)
        pump()
        self.assertEqual(self.panel.history_list.count(), 1)
        self.panel.history_list.setCurrentRow(0)
        self.panel._open_history()
        pump(10)
        self.assertEqual(self.panel.top_tabs.currentIndex(), 0)
        self.assertEqual(self.panel.pages.currentIndex(), 1)
        self.assertIn("Buy A", self.panel.viewer.toPlainText())               # the saved final result
        self.assertFalse(self.controller.is_running)

        # A new controller on the same database = an app restart.
        mission_id = self.controller.snapshot().id
        stuck = self.store.load(mission_id)
        stuck.status = MissionStatus.RUNNING
        stuck.tasks[1].status = TaskStatus.RUNNING
        self.store.save_dict(stuck.id, stuck.to_dict())
        again = TeamController(TeamStore(self.db), None, None, client_factory=lambda: self.client)
        try:
            self.assertEqual(again.history()[0].status, MissionStatus.INTERRUPTED)
            self.assertTrue(again.open_mission(mission_id) is not None)
        finally:
            again.shutdown()
            again.deleteLater()

        self.panel.top_tabs.setCurrentIndex(1)
        pump()
        self.panel.history_list.setCurrentRow(0)
        with patch("app.ui.team_panel.confirm_destructive", return_value=False):
            self.panel._delete_history()
        self.assertEqual(len(self.controller.history()), 1)
        with patch("app.ui.team_panel.confirm_destructive", return_value=True):
            self.panel._delete_history()
        self.assertEqual(len(self.controller.history()), 0)


class WorkspaceApplyTests(TeamUITestCase):
    def test_apply_is_offered_only_for_unapplied_proposals_and_needs_confirmation(self) -> None:
        self.build()
        with tempfile.TemporaryDirectory() as root:
            mission = Mission(goal="edit", status=MissionStatus.COMPLETED_WITH_ISSUES, workspace_path=root)
            mission.tasks.append(Task("T1", "Edit", AgentId.CODER, status=TaskStatus.DONE, outputs=["A1"]))
            mission.artifacts.append(Artifact("A1", ArtifactKind.FILE, "x.py", "print(1)\n", AgentId.CODER, "T1",
                                              meta={"path": "x.py", "is_new": True, "diff": "+print(1)\n"}))
            self.store.create(mission)
            self.controller.open_mission(mission.id)
            pump(10)
            self.panel.run_tabs.setCurrentIndex(3)             # the Results tab holds these buttons
            pump()
            self.assertTrue(self.panel.apply_button.isVisibleTo(self.panel))
            self.assertTrue(self.panel.save_files_action.isVisible())
            # Declining the confirmation writes nothing.
            with patch("app.ui.team_panel.QMessageBox.exec"), \
                    patch("app.ui.team_panel.QMessageBox.clickedButton", return_value=None):
                self.panel._apply_to_workspace()
            self.assertFalse(os.path.exists(os.path.join(root, "x.py")))
            # Confirming writes exactly that file.
            written = self.controller.apply_files(["A1"])
            self.assertEqual(written, ["x.py"])
            self.assertEqual(open(os.path.join(root, "x.py"), encoding="utf-8").read(), "print(1)\n")
            pump(10)
            self.assertFalse(self.panel.apply_button.isVisibleTo(self.panel))


class NarrowWidthTests(TeamUITestCase):
    def test_nothing_overflows_a_300px_sidebar_in_any_state(self) -> None:
        self.build()
        self.panel.setFixedWidth(300)
        self.panel.goal.setPlainText("Compare the products in these tabs and write a recommendation for me please")
        self.panel._sources = [Source("S1", SourceKind.TAB, "A very long page title " * 6, "https://x.example/" + "a" * 80, "t"),
                               Source("S2", SourceKind.TAB, "Blocked", "", "", SourceStatus.INACCESSIBLE,
                                      "The page has no readable text (it may still be loading, blank, or need sign-in).")]
        self.panel._refresh_included()
        pump(5)
        self._assert_fits(self.panel.pages.widget(0))
        self.assertLessEqual(self.panel.minimumSizeHint().width(), 300)

        self.start_mission(goal="Compare these", text="data")
        self.assertTrue(wait_for(lambda: self.controller.snapshot().status == MissionStatus.COMPLETED))
        pump(10)
        for index in range(4):
            self.panel.run_tabs.setCurrentIndex(index)
            pump(5)
            self._assert_fits(self.panel.run_tabs.currentWidget())
        self._assert_fits(self.panel.pages.widget(1))
        self.assertLessEqual(self.panel.minimumSizeHint().width(), 300)

    def _assert_fits(self, root: QWidget) -> None:
        for child in root.findChildren(QWidget):
            if not child.isVisibleTo(root) or child.width() == 0:
                continue
            top_left = child.mapTo(root, child.rect().topLeft())
            if isinstance(child, QScrollBar):
                continue
            self.assertLessEqual(top_left.x() + child.width(), root.width() + 2,
                                 f"{type(child).__name__} overflows a narrow panel")
        for bar in root.findChildren(QScrollBar):
            if bar.orientation().name == "Horizontal" and bar.isVisibleTo(root):
                self.assertEqual(bar.maximum(), 0, "an unexpected horizontal scrollbar")


class UnexpectedContentTests(TeamUITestCase):
    def test_hostile_text_is_shown_as_text_never_interpreted_as_markup(self) -> None:
        self.build()
        hostile = "<img src=x onerror=alert(1)>"
        self.panel._sources.append(Source("S1", SourceKind.TAB, hostile, "https://x.example/", "page text"))
        self.panel.goal.setPlainText("<b>bold goal</b>")
        self.panel.start_button.click()
        self.assertTrue(wait_for(lambda: self.controller.snapshot().status == MissionStatus.COMPLETED))
        pump(10)
        self.assertEqual(self.panel.run_goal.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(self.panel.run_goal._full, "<b>bold goal</b>")
        self.panel.viewer_choice.setCurrentIndex(self.panel.viewer_choice.findData("__sources__"))
        pump()
        html = self.panel.viewer.toHtml().lower()
        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_clicking_a_source_link_opens_it_in_a_browser_tab(self) -> None:
        opened = []
        self.build(browser=SimpleNamespace(open_tab=opened.append, list_tabs=lambda: []))
        from PySide6.QtCore import QUrl
        self.panel._open_link(QUrl("https://good.example/a"))
        self.panel._open_link(QUrl("javascript:alert(1)"))
        self.panel._open_link(QUrl("file:///etc/passwd"))
        self.assertEqual(opened, ["https://good.example/a"])


class DownloadsIntegrationTests(TeamUITestCase):
    def build_with_downloads(self) -> None:
        from app.browser.downloads import DownloadManager
        self.downloads_dir = tempfile.mkdtemp(prefix="pybrowser-dl-")
        self.manager = DownloadManager()
        self.build(downloads=self.manager, downloads_dir=self.downloads_dir)
        self.saved_messages = []
        self.controller.file_saved.connect(self.saved_messages.append)

    def tearDown(self) -> None:
        super().tearDown()
        if hasattr(self, "downloads_dir"):
            import shutil
            shutil.rmtree(self.downloads_dir, ignore_errors=True)

    def finish_mission(self) -> None:
        self.start_mission()
        self.assertTrue(wait_for(lambda: self.controller.snapshot().status == MissionStatus.COMPLETED))
        pump(10)
        self.panel.run_tabs.setCurrentIndex(3)
        pump()

    def test_save_goes_through_the_download_manager_and_never_overwrites(self) -> None:
        self.build_with_downloads()
        self.finish_mission()
        self.panel.save_button.click()
        self.assertEqual(len(self.manager.items()), 1)
        item = self.manager.items()[0]
        self.assertEqual((item.state, item.url.startswith("pybrowser://ai-team/")), ("completed", True))
        path = os.path.join(item.directory, item.file_name)
        self.assertEqual(os.path.dirname(path), self.downloads_dir)
        with open(path, encoding="utf-8") as handle:
            self.assertIn("Buy A", handle.read())
        self.assertIn(path, self.panel.saved_note.text())
        self.assertTrue(self.saved_messages and "Downloads" in self.saved_messages[0])
        self.panel.save_button.click()                                  # the same file again...
        names = sorted(i.file_name for i in self.manager.items())
        self.assertEqual(len(names), 2)
        self.assertTrue(any("(1)" in n for n in names))                 # ...is a new file, not an overwrite

    def test_save_as_writes_where_the_user_chose_and_still_lists_it_in_downloads(self) -> None:
        self.build_with_downloads()
        self.finish_mission()
        chosen = os.path.join(self.downloads_dir, "elsewhere", "mine.md")
        os.makedirs(os.path.dirname(chosen))
        with open(chosen, "w", encoding="utf-8") as handle:
            handle.write("old")
        with patch("app.ui.team_panel.QFileDialog.getSaveFileName", return_value=(chosen, "")):
            self.panel.save_as_action.trigger()                           # the dialog already asked "replace?"
        with open(chosen, encoding="utf-8") as handle:
            self.assertIn("Buy A", handle.read())
        self.assertEqual(self.manager.items()[0].file_name, "mine.md")

    def test_generated_files_are_saved_into_one_downloads_subfolder(self) -> None:
        self.build_with_downloads()
        mission = Mission(goal="Write a calculator!?", status=MissionStatus.COMPLETED)
        mission.artifacts += [
            Artifact("A1", ArtifactKind.FILE, "calc.py", "v1\n", AgentId.CODER, "T1", meta={"path": "calc.py"}),
            Artifact("A2", ArtifactKind.FILE, "calc.py", "v2\n", AgentId.CODER, "T3", 2, "A1", meta={"path": "calc.py"}),
            Artifact("A3", ArtifactKind.FILE, "tests/test_calc.py", "t\n", AgentId.CODER, "T1",
                     meta={"path": "tests/test_calc.py"})]
        self.store.create(mission)
        self.controller.open_mission(mission.id)
        pump(10)
        self.panel.run_tabs.setCurrentIndex(3)
        pump()
        self.panel.save_files_action.trigger()
        saved = {os.path.relpath(os.path.join(i.directory, i.file_name), self.downloads_dir): i
                 for i in self.manager.items()}
        folder = next(iter(saved)).split(os.sep)[0]
        self.assertTrue(folder.startswith("AI Team - Write a calculator"))
        self.assertEqual(sorted(k.split(os.sep, 1)[1] for k in saved), ["calc.py", os.path.join("tests", "test_calc.py")])
        with open(os.path.join(self.downloads_dir, folder, "calc.py"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "v2\n")                      # only the latest version of each file

    def test_a_generated_file_name_can_never_escape_the_downloads_folder(self) -> None:
        self.build_with_downloads()
        path = self.controller.save_to_downloads("../../../evil.txt", "x", 7)
        self.assertTrue(os.path.realpath(path).startswith(os.path.realpath(self.downloads_dir) + os.sep))
        path = self.controller.save_to_downloads("C:\\Windows\\system32\\evil.txt", "x", 7)
        self.assertTrue(os.path.realpath(path).startswith(os.path.realpath(self.downloads_dir) + os.sep))

    def test_the_downloads_window_lists_a_generated_file_like_any_other(self) -> None:
        from app.ui.downloads_panel import DownloadsDialog
        self.build_with_downloads()
        self.controller.save_to_downloads("notes.md", "# hi", 3)
        dialog = DownloadsDialog(self.manager)
        try:
            texts = " ".join(label.text() for label in dialog.findChildren(QLabel))
            self.assertIn("notes.md", texts)
            self.assertIn("Completed", texts)
        finally:
            dialog.deleteLater()
            pump()


class SetupFlowTests(TeamUITestCase):
    KEY = "gsk_setup_flow_key_0123456789abcdef"

    def setUp(self) -> None:
        import keyring
        from tests.test_team_websearch import MemoryKeyring
        self._previous = keyring.get_keyring()
        self.memory = MemoryKeyring()
        keyring.set_keyring(self.memory)
        self.addCleanup(keyring.set_keyring, self._previous)
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in ("GROQ_API_KEY", "PYBROWSER_DISABLE_KEYRING", "PYBROWSER_TEAM_PROVIDER", "PYBROWSER_TEAM_MODEL"):
            os.environ.pop(name, None)

    def build_real_provider(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        from app.storage.settings import SettingsStore
        self.settings = SettingsStore(self.db)
        self.controller = TeamController(TeamStore(self.db), self.settings, None)    # no injected client
        self.panel = TeamPanel(self.controller)
        self.panel.show()
        pump()

    def test_the_key_dialog_opens_on_groq(self) -> None:
        from app.storage.settings import SettingsStore
        from app.ui.agent_setup import ApiKeyDialog
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.controller = TeamController(TeamStore(self.db), None, None, client_factory=lambda: None)
        self.panel = TeamPanel(self.controller)
        dialog = ApiKeyDialog(None, SettingsStore(self.db), initial_provider="groq")
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.provider_box.currentData(), "groq")
        self.assertTrue(dialog._other_widget.isVisibleTo(dialog) or not dialog._other_widget.isHidden())

    def test_the_window_passes_groq_to_the_existing_dialog(self) -> None:
        from app.ui import main_window as mw
        seen = {}

        class FakeDialog:
            def __init__(self, parent, settings, initial_provider=None):
                seen["provider"] = initial_provider

            def exec(self):
                return 0

        fake_window = SimpleNamespace(settings=object(), _apply_agent_settings=lambda: seen.setdefault("applied", True))
        with patch("app.ui.agent_setup.ApiKeyDialog", FakeDialog):
            mw.MainWindow._configure_agent(fake_window, "groq")
            self.assertEqual(seen, {"provider": "groq", "applied": True})
            seen.clear()
            mw.MainWindow._configure_agent(fake_window)             # the Tools-menu path is unchanged
            self.assertIsNone(seen["provider"])

    def test_a_key_entered_once_is_reused_everywhere_without_being_shown(self) -> None:
        from app.agent.credentials import provider_key_store
        from app.agent.openai_compatible import GroqClient
        from app.team.llm import make_client_factory, resolve_provider
        self.build_real_provider()
        self.assertNotIn(">Test</a>", self.panel.env.text())
        self.assertIn("not set up", self.panel.env.text())
        provider_key_store("groq").set_key(self.KEY)        # exactly what the dialog's "Save API key" does
        with patch.object(GroqClient, "test_connection", return_value=(True, "Connected. The model accepted a request.")):
            self.panel.refresh_environment()
            self.assertTrue(wait_for(lambda: "Connected" in self.panel.env.text()))
        text = self.panel.env.text()
        self.assertIn("Groq</b> \u00b7 llama-3.3-70b-versatile", text)
        self.assertIn(">Test</a>", text)
        self.assertFalse(self.panel.configure_button.isVisibleTo(self.panel))
        self.assertNotIn(self.KEY, text + self.panel.env.toolTip())
        status = resolve_provider(self.settings)
        self.assertEqual(status.secret, self.KEY)
        client = make_client_factory(status, self.settings)()
        self.assertEqual(client._client.headers["Authorization"], f"Bearer {self.KEY}")   # same key, same store
        self.assertNotIn(self.KEY, self.db.query_one("SELECT group_concat(value) FROM settings")[0] or "")

    def test_a_failed_connection_test_is_shown_with_the_key_redacted(self) -> None:
        from app.agent.credentials import provider_key_store
        from app.agent.openai_compatible import GroqClient
        self.build_real_provider()
        provider_key_store("groq").set_key(self.KEY)
        with patch.object(GroqClient, "test_connection",
                          return_value=(False, f"Groq rejected the request (401): bad key {self.KEY}")):
            self.panel.refresh_environment()
            self.assertTrue(wait_for(lambda: "rejected" in self.panel.env.text()))
        self.assertNotIn(self.KEY, self.panel.env.text())
        self.assertIn("[redacted]", self.panel.env.text())

    def test_the_test_button_runs_the_check_on_demand(self) -> None:
        from app.agent.credentials import provider_key_store
        from app.agent.openai_compatible import GroqClient
        self.build_real_provider()
        provider_key_store("groq").set_key(self.KEY)
        self.panel._refresh_env()
        calls = []
        with patch.object(GroqClient, "test_connection",
                          side_effect=lambda key, model: (calls.append((key == self.KEY, model)), (True, "ok"))[1]):
            self.panel._on_env_link("test")
            self.assertTrue(wait_for(lambda: bool(calls)))
        self.assertEqual(calls, [(True, "llama-3.3-70b-versatile")])


class SettingsDialogTests(TeamUITestCase):
    def setUp(self) -> None:
        import keyring
        from tests.test_team_websearch import MemoryKeyring
        self._previous = keyring.get_keyring()
        self.memory = MemoryKeyring()
        keyring.set_keyring(self.memory)
        self.addCleanup(keyring.set_keyring, self._previous)
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for name in ("PYBROWSER_SEARCH_PROVIDER", "TAVILY_API_KEY", "BRAVE_SEARCH_API_KEY",
                     "PYBROWSER_DISABLE_KEYRING"):
            os.environ.pop(name, None)

    def make(self):
        from app.storage.settings import SettingsStore
        from app.ui.team_settings import TeamSettingsDialog
        self.build(settings=SettingsStore(Database(os.path.join(tempfile.mkdtemp(), "s.sqlite3"))))
        dialog = TeamSettingsDialog(self.controller.settings, self.controller)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_a_search_key_goes_to_the_keyring_and_never_into_the_settings_table(self) -> None:
        dialog = self.make()
        self.assertFalse(dialog.search_key.isEnabled())                  # off until a provider is chosen
        dialog.search_provider.setCurrentIndex(dialog.search_provider.findData("tavily"))
        self.assertTrue(dialog.search_key.isEnabled())
        self.assertIn("TAVILY_API_KEY", dialog.search_help.text())
        self.assertIn("tavily.com", dialog.search_help.text())
        dialog.search_key.setText("tvly-dialog-key-0123456789")
        dialog.search_save.click()
        self.assertEqual(self.memory.data[("PyBrowser", "tavily-api-key")], "tvly-dialog-key-0123456789")
        self.assertEqual(dialog.search_key.text(), "")                    # cleared; never shown again
        self.assertIn("Key found", dialog.search_status.text())
        stored = self.controller.settings._db.query("SELECT key, value FROM settings")
        self.assertFalse(any("tvly-dialog" in (row[1] or "") for row in stored))
        self.assertEqual(self.controller.web_status().available, True)
        self.assertEqual(self.controller.web_status().provider, "tavily")
        dialog.search_remove.click()
        self.assertFalse(self.controller.web_status().available)

    def test_saving_persists_limits_provider_and_sandbox_choices_and_has_no_unsafe_option(self) -> None:
        from app.team.limits import TeamLimits
        dialog = self.make()
        self.assertFalse(hasattr(dialog, "unisolated"))                  # there is no switch for unconfined execution
        dialog._inputs["max_concurrency"].setValue(1)
        dialog._inputs["max_revision_rounds"].setValue(3)
        dialog.model.setText("openai/gpt-oss-20b")
        dialog.backend.setCurrentIndex(dialog.backend.findData("container"))
        dialog.image.setText("python:3.11-slim")
        dialog._save()
        limits = TeamLimits.from_settings(self.controller.settings)
        self.assertEqual((limits.max_concurrency, limits.max_revision_rounds), (1, 3))
        self.assertEqual(self.controller._sandbox_params(), ("container", "python:3.11-slim"))
        self.assertEqual(self.controller.settings.get("team_model"), "openai/gpt-oss-20b")

    def test_the_sandbox_section_explains_what_is_missing_and_how_to_fix_it(self) -> None:
        unavailable = sandbox_unavailable("the container daemon is not running.",
                                          "Install Docker Desktop, then run: docker pull python:3.12-slim")
        with patch("app.team.sandbox.probe", return_value=unavailable):
            dialog = self.make()
            self.controller.recheck_sandbox()
            self.assertTrue(wait_for(lambda: not self.controller.sandbox_status().checking))
            dialog._show_sandbox_status()
        self.assertIn("Unavailable: the container daemon is not running", dialog.sandbox_status.text())
        self.assertIn("docker pull python:3.12-slim", dialog.sandbox_status.text())


def sandbox_unavailable(reason: str, hint: str):
    from app.team.sandbox import SandboxStatus
    return SandboxStatus(False, reason, setup_hint=hint)


class SandboxStatusInThePanelTests(TeamUITestCase):
    def test_probing_never_blocks_the_gui_and_the_panel_updates_when_it_finishes(self) -> None:
        from app.team import sandbox as sb

        def slow_probe(*args, **kwargs):
            time.sleep(0.6)
            return sandbox_unavailable("Docker/Podman was not found on PATH.", "Install Docker Desktop.")

        with patch("app.team.sandbox.probe", side_effect=slow_probe):
            started = time.monotonic()
            self.build()
            self.assertLess(time.monotonic() - started, 0.5)            # constructing the panel did not wait
            self.assertIn("Sandbox: checking", self.panel.env.text())
            self.assertTrue(wait_for(lambda: "how to enable" in self.panel.env.text(), 10))
            with patch("app.ui.team_panel.QMessageBox.information") as shown:
                self.panel._on_env_link("sandbox-setup")
            body = shown.call_args[0][2]
            self.assertIn("Docker/Podman was not found", body)
            self.assertIn("Install Docker Desktop", body)
            self.assertIn("no unrestricted fallback", body)

    def test_the_engine_waits_for_the_probe_off_the_gui_thread(self) -> None:
        from app.team.sandbox import SandboxStatus

        def slow_probe(*args, **kwargs):
            time.sleep(0.4)
            return SandboxStatus(False, "none here")

        with patch("app.team.sandbox.probe", side_effect=slow_probe):
            self.build()
            self.start_mission()                                       # starts while the probe is still running
            self.assertTrue(wait_for(lambda: self.controller.snapshot().status == MissionStatus.COMPLETED))
        self.assertFalse(self.controller._sandbox.available)


class StaleStateTests(TeamUITestCase):
    def test_a_missing_key_warning_goes_away_once_the_key_exists(self) -> None:
        from app.team.llm import ProviderStatus
        self.build()
        missing = ProviderStatus("groq", "Groq", "m", False, "No Groq API key is configured.")
        with patch.object(self.controller, "provider_status", return_value=missing):
            self.panel.goal.setPlainText("anything")
            self.panel.start_button.click()
            self.assertTrue(self.panel.compose_error.isVisibleTo(self.panel))
        self.panel.refresh_environment()                  # the key dialog closed; a key exists now
        self.assertFalse(self.panel.compose_error.isVisibleTo(self.panel))

    def test_web_search_defaults_on_when_it_becomes_available_and_off_when_it_goes_away(self) -> None:
        from app.team.websearch import SearchStatus
        self.build()
        off = SearchStatus("", "", False, "off")
        on = SearchStatus("tavily", "Tavily", True, "ok", "keyring", "k" * 12)
        with patch.object(self.controller, "web_status", return_value=off):
            self.panel._refresh_env()
            self.assertFalse(self.panel.web_check.isChecked())
        with patch.object(self.controller, "web_status", return_value=on):
            self.panel._refresh_env()
            self.assertTrue(self.panel.web_check.isChecked())
            self.panel.web_check.setChecked(False)          # the user's per-mission choice sticks
            self.panel._refresh_env()
            self.assertFalse(self.panel.web_check.isChecked())
        with patch.object(self.controller, "web_status", return_value=off):
            self.panel._refresh_env()
            self.assertFalse(self.panel.web_check.isEnabled())


class WebSearchToggleTests(TeamUITestCase):
    def test_the_checkbox_follows_whether_a_search_provider_is_set_up_and_is_passed_to_the_mission(self) -> None:
        from app.team.websearch import SearchStatus
        self.build()
        with patch.object(self.controller, "web_status",
                          return_value=SearchStatus("", "", False, "Web search is off. Pick Tavily or Brave.")):
            self.panel._refresh_env()
            self.assertFalse(self.panel.web_check.isEnabled())
            self.assertFalse(self.panel.web_check.isChecked())
            self.assertIn("not set up", self.panel.web_check.text())
        ready = SearchStatus("tavily", "Tavily", True, "Tavily key from the OS keyring", "keyring", "k" * 12)
        seen = []
        with patch.object(self.controller, "web_status", return_value=ready), \
                patch.object(self.controller, "start", side_effect=lambda *a, **k: seen.append(k) or None):
            self.panel._refresh_env()
            self.assertTrue(self.panel.web_check.isEnabled())
            self.assertIn("Tavily", self.panel.web_check.text())
            self.assertIn("sent to Tavily", self.panel.web_check.toolTip())
            self.panel.web_check.setChecked(True)
            self.panel.goal.setPlainText("research this")
            self.panel.start_button.click()
            self.panel.web_check.setChecked(False)
            self.panel.goal.setPlainText("research that")
            self.panel.start_button.click()
        self.assertEqual([k["web_search"] for k in seen], [True, False])


class RecoveryAndGuidanceTests(TeamUITestCase):
    def failing_writer(self):
        from app.agent.claude_client import ClaudeError
        boom = ClaudeError("provider down", retryable=False)
        return happy_client(writer=[boom, "# Comparison\nA is cheap [S1]"])

    def test_next_step_guides_from_empty_to_ready(self) -> None:
        self.build()
        self.assertIn("describe the mission", self.panel.next_step.text())
        self.panel.goal.setPlainText("Compare the products")
        pump()
        self.assertIn("Ready.", self.panel.next_step.text())
        self.assertIn("model calls", self.panel.next_step.text())

    def test_failed_task_offers_retry_and_skip_links_and_only_that_task_reruns(self) -> None:
        client = self.failing_writer()
        self.build(client)
        self.start_mission()
        self.assertTrue(wait_for(lambda: self.controller.snapshot().status in (
            MissionStatus.FAILED, MissionStatus.COMPLETED_WITH_ISSUES)))
        pump(10)
        self.assertEqual(self.panel.retry_button.text(), "Retry failed")
        self.assertTrue(self.panel.progress.isVisibleTo(self.panel))
        failed = next(t for t in self.controller.snapshot().tasks if t.status == TaskStatus.FAILED)
        cards = [self.panel._tasks_box.itemAt(i).widget() for i in range(self.panel._tasks_box.count() - 1)]
        self.assertTrue(any(f"retry:{failed.id}" in (c.text() if c else "") for c in cards))
        researchers = client.roles().count("researcher")
        self.panel._on_task_link(f"retry:{failed.id}")
        self.assertTrue(wait_for(lambda: self.controller.snapshot().status == MissionStatus.COMPLETED))
        self.assertEqual(client.roles().count("researcher"), researchers)       # finished work is not repeated

    def test_interrupted_mission_explains_what_is_kept(self) -> None:
        self.build()
        mission = Mission(goal="g", status=MissionStatus.INTERRUPTED, tasks=[
            Task("T1", "t", AgentId.RESEARCHER, "i", status=TaskStatus.DONE),
            Task("T2", "t", AgentId.WRITER, "i", status=TaskStatus.CANCELLED)])
        self.controller._view = mission
        self.panel.refresh()
        pump()
        self.assertEqual(self.panel.retry_button.text(), "Resume")
        self.assertIn("1 finished task(s) are kept", self.panel.run_status.text())
        self.assertTrue(self.controller.interrupted)

    def test_sources_view_separates_pages_from_snippets(self) -> None:
        self.build()
        mission = Mission(goal="g", sources=[
            Source("S1", SourceKind.WEB, "Full", "https://a.example/", "t", SourceStatus.INCLUDED,
                   depth="page", retrieved="2026-01-02"),
            Source("S2", SourceKind.WEB, "Snip", "https://b.example/", "t", SourceStatus.INCLUDED,
                   depth="snippet", note="Page not opened: blocked")])
        html = self.panel._sources_html(mission)
        self.assertIn("Web pages (retrieved text)", html)
        self.assertIn("page text retrieved 2026-01-02", html)
        self.assertIn("page not opened", html)
        self.assertIn("Page not opened: blocked", html)


    def test_remaining_allowance_is_shown_and_explains_cancellation(self) -> None:
        from app.team.limits import TeamLimits
        self.assertEqual(TeamLimits(max_model_calls=60).allowance(10, 0), (50, 170))
        self.assertEqual(TeamLimits(max_model_calls=60).allowance(70, 60), (50, 110))
        self.assertEqual(TeamLimits(max_model_calls=60).allowance(180, 120), (0, 0))
        self.build()
        mission = Mission(goal="g", status=MissionStatus.CANCELLED, model_calls=25, tasks=[
            Task("T1", "t", AgentId.RESEARCHER, "i", status=TaskStatus.DONE)])
        self.controller._view = mission
        self.panel.refresh()
        pump()
        text = self.panel.run_status.text()
        self.assertIn("25 model calls", text)
        self.assertIn("155 left", text)                                   # 3 runs x 60 - 25
        self.assertIn("cannot be undone", text)
        self.assertIn("cannot undo calls already made", self.panel.run_status.toolTip())
        self.assertIn("capped at 180", self.panel.run_status.toolTip())
        self.assertIn("cannot be undone", self.panel.cancel_button.toolTip())
        self.assertTrue(self.panel.retry_button.isEnabled())

    def test_a_mission_with_no_allowance_left_cannot_be_retried_from_the_panel(self) -> None:
        self.build()
        mission = Mission(goal="g", status=MissionStatus.FAILED, model_calls=180, tasks=[
            Task("T1", "t", AgentId.RESEARCHER, "i", status=TaskStatus.FAILED)])
        self.controller._view = mission
        self.panel.refresh()
        pump()
        self.assertFalse(self.panel.retry_button.isEnabled())
        self.assertIn("whole model-call allowance", self.panel.retry_button.toolTip())

    def test_next_step_states_the_lifetime_cap(self) -> None:
        self.build()
        self.panel.goal.setPlainText("Compare the products")
        pump()
        self.assertIn("capped at 180", self.panel.next_step.text())
        self.assertIn("cannot undo", self.panel.next_step.text())

    def test_replaced_artifacts_are_labelled_in_the_results_list(self) -> None:
        self.build()
        mission = Mission(goal="g", status=MissionStatus.COMPLETED, artifacts=[
            Artifact("A1", ArtifactKind.REPORT, "Draft", "old", AgentId.WRITER, "T2", meta={"replaced": True}),
            Artifact("A2", ArtifactKind.REPORT, "Draft", "new", AgentId.WRITER, "T2")])
        self.controller._view = mission
        self.panel.refresh()
        pump()
        labels = [self.panel.viewer_choice.itemText(i) for i in range(self.panel.viewer_choice.count())]
        self.assertTrue(any("A1" in t and "replaced" in t for t in labels))
        self.assertFalse(any("A2" in t and "replaced" in t for t in labels))

    def test_page_reading_is_wired_and_can_be_switched_off(self) -> None:
        from app.team.webfetch import PageFetcher
        self.build()
        self.assertIsInstance(self.controller.page_fetcher(), PageFetcher)
        self.assertEqual(self.controller.page_fetcher().max_chars, self.controller.limits().fetch_max_chars)
        self.controller._settings = {"team_max_fetch_pages": "0"}
        self.assertIsNone(self.controller.page_fetcher())

    def test_fetch_limits_are_in_the_settings_dialog(self) -> None:
        from app.ui.team_settings import _FIELDS
        self.assertIn("max_fetch_pages", [f[0] for f in _FIELDS])


class WindowsSafeNamesTests(unittest.TestCase):
    def test_generated_files_never_use_windows_device_names(self) -> None:
        from app.browser.downloads import DownloadManager
        with tempfile.TemporaryDirectory() as folder:
            manager = DownloadManager()
            item = manager.save_generated("con.py", "x = 1", folder, "team://test")
            self.assertEqual(item.file_name, "_con.py")
            item = manager.save_generated("src/NUL.txt", "x", folder, "team://test", subfolder="AI Team: run?")
            self.assertTrue(os.path.exists(os.path.join(item.directory, "_NUL.txt")))
            self.assertNotIn("?", item.directory)


if __name__ == "__main__":
    unittest.main()
