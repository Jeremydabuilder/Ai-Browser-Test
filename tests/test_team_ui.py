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
from PySide6.QtWidgets import QApplication, QScrollBar, QWidget  # noqa: E402

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
    def build(self, client=None, browser=None) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.store = TeamStore(self.db)
        self.client = client or happy_client()
        self.controller = TeamController(self.store, None, None, client_factory=lambda: self.client)
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
            self.assertIn("not ready", text)
            self.assertIn("GROQ_API_KEY", text)
            self.assertTrue(self.panel.configure_button.isVisibleTo(self.panel))
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
        self.assertIn("Web search: unavailable", self.panel.env.text())    # limitations are shown up front
        self.assertIn("Execution:", self.panel.env.text())


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
        self.assertIn("Search: local knowledge only", self.panel.env.text())
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
            self.assertTrue(self.panel.save_files_button.isVisibleTo(self.panel))
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
        self.assertEqual(self.panel.run_goal.text(), "<b>bold goal</b>")
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


if __name__ == "__main__":
    unittest.main()
