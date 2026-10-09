"""Results workspace rules (pure functions) and the panel's follow-up / history / export flows.

Run with:  QT_QPA_PLATFORM=offscreen python tests/run.py tests.test_team_results
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.team.model import (  # noqa: E402
    AgentId, Artifact, ArtifactKind, Mission, MissionStatus, Source, SourceKind, Task, TaskStatus,
)
from app.ui import team_results as tr  # noqa: E402
from tests.test_team_ui import TeamUITestCase, happy_client, pump, setUpModule as _ui_setup, wait_for  # noqa: E402


def setUpModule() -> None:
    _ui_setup()


def art(i, kind, title, content="x", **kw):
    return Artifact(f"A{i}", kind, title, content, kw.pop("agent", AgentId.WRITER), "T1", **kw)


class PureRules(unittest.TestCase):
    def mission(self):
        m = Mission(goal="Compare widgets", status=MissionStatus.COMPLETED)
        m.artifacts = [
            art(1, ArtifactKind.NOTES, "Notes", "n"),
            art(2, ArtifactKind.REPORT, "Draft", "old draft"),
            art(3, ArtifactKind.REPORT, "Draft", "new draft\nline", version=2, replaces="A2"),
            art(4, ArtifactKind.REVIEW, "Review", "r1", agent=AgentId.REVIEWER),
            art(5, ArtifactKind.REVIEW, "Review", "r2", agent=AgentId.REVIEWER, replaces="A4"),
            art(6, ArtifactKind.FINAL, "Final result", "final v1", agent=AgentId.COORDINATOR, meta={"cited": ["S1", "S2"]}),
            art(7, ArtifactKind.FINAL, "Final result", "final v2", agent=AgentId.COORDINATOR, version=2, replaces="A6",
                meta={"cited": ["S1"]}),
        ]
        m.final_artifact_id = "A7"
        return m

    def test_replaced_versions_never_appear_as_current_but_stay_in_history(self) -> None:
        m = self.mission()
        self.assertEqual(tr.replaced_ids(m), {"A2", "A4", "A6"})
        self.assertEqual([k for _, k in tr.view_items(m, "answer")], ["A7"])
        self.assertEqual(tr.answer_artifact(m)[0].id, "A7")
        history = tr.view_items(m, "history")
        self.assertEqual([k for _, k in history][:2], ["A7", "A6"])                 # newest first, nothing dropped
        self.assertEqual(len(history), 7)
        self.assertTrue(next(t for t, k in history if k == "A6").endswith("replaced"))
        self.assertTrue(next(t for t, k in tr.view_items(m, "review") if "A4" in t).endswith("replaced"))
        self.assertIn("latest", next(t for t, k in tr.view_items(m, "review") if k == "A5"))

    def test_diff_shows_what_changed_between_revisions(self) -> None:
        m = self.mission()
        text = tr.diff_markdown(m.artifact("A2"), m.artifact("A3"))
        self.assertIn("2 line(s) added, 1 removed", text)
        self.assertIn("-old draft", text)
        self.assertIn("+new draft", text)
        same = tr.diff_markdown(m.artifact("A6"), Artifact("A9", ArtifactKind.FINAL, "t", "final v1", "x", ""))
        self.assertIn("No text changes", same)

    def test_diff_html_wraps_and_marks_added_and_removed_lines(self) -> None:
        from app.ui import theme
        m = self.mission()
        page = tr.diff_html(m.artifact("A2"), m.artifact("A3"), theme.LIGHT)
        self.assertIn("2 line(s) added, 1 removed", page)
        self.assertIn("pre-wrap", page)
        self.assertIn("<s>old draft</s>", page)
        self.assertIn("new draft", page)
        self.assertIn(theme.LIGHT.danger_soft, page)
        self.assertIn("No text changes", tr.diff_html(m.artifact("A6"), Artifact("A9", ArtifactKind.FINAL, "t",
                                                                                "final v1", "x", ""), theme.DARK))
        self.assertIn("&lt;script&gt;", tr.diff_html(m.artifact("A2"), art(8, ArtifactKind.REPORT, "d", "<script>"),
                                                     theme.LIGHT))                      # content is escaped

    def test_banner_states_only_facts_in_the_mission(self) -> None:
        m = self.mission()
        banner = tr.answer_banner(m)
        self.assertIn("1 cited source", banner)
        self.assertIn("version 2", banner)
        self.assertIn("not reviewed", banner)                                         # no reviewer task, no criteria
        m.tasks = [Task("T1", "r", AgentId.REVIEWER, "i", status=TaskStatus.DONE)]
        self.assertIn("reviewed", tr.answer_banner(m))
        m.criteria_check = [{"criterion": "a", "met": True}, {"criterion": "b", "met": False}]
        m.unresolved_issues = ["B1 x"]
        banner = tr.answer_banner(m)
        self.assertIn("1 of 2 criteria met", banner)
        self.assertIn("1 open review issue", banner)
        m.final_artifact_id = ""
        self.assertIn("not the final answer", tr.answer_banner(m))                    # a draft is never dressed up

    def test_empty_states_explain_why(self) -> None:
        m = Mission(goal="g", status=MissionStatus.COMPLETED)
        m.tasks = [Task("T1", "r", AgentId.RESEARCHER, "i")]
        self.assertIn("did not involve code", tr.empty_text(m, "files"))
        self.assertIn("no code", tr.empty_text(m, "tests"))
        m.limitations = ["[capability] Tests were not run: no isolated execution environment is available (x)."]
        self.assertIn("Tests were not run", tr.empty_text(m, "tests"))

    def test_followup_thread_labels_where_each_answer_came_from(self) -> None:
        m = Mission(goal="g")
        m.followups = [
            {"id": 1, "mode": "answer", "question": "Why?", "answer": "Because [S1].", "basis": "Answered from existing.",
             "calls": 1, "ts": 0, "needs_research": False},
            {"id": 2, "mode": "research", "question": "More?", "answer": "NOT IN EVIDENCE: x", "basis": "New research: 1 page.",
             "calls": 2, "ts": 0, "needs_research": True}]
        text = tr.followups_markdown(m)
        self.assertIn("You · Ask", text)
        self.assertIn("You · New research", text)
        self.assertIn("*Answered from existing.* (1 model call(s))", text)
        self.assertIn("Research more", text)

    def test_export_names_are_safe(self) -> None:
        m = Mission(goal="Compare: the <widgets>/products?")
        name = tr.export_name(m, art(1, ArtifactKind.FINAL, "Final result"), "html")
        self.assertRegex(name, r"^[a-z0-9-]+-final-result\.html$")


class PanelFlows(TeamUITestCase):
    def finish(self, client=None):
        self.build(client or happy_client(
            followup_answer=["B lasts 5 years [S1]."], followup_rewrite=["**Buy A** [S1]."]))
        self.start_mission()
        self.assertTrue(wait_for(lambda: self.controller.snapshot().status == MissionStatus.COMPLETED))
        pump(10)

    def test_views_have_accessible_names_mnemonics_and_honest_empty_states(self) -> None:
        self.finish()
        for key, button in self.panel.view_buttons.items():
            self.assertTrue(button.accessibleName().endswith("view"))
            self.assertTrue(button.isCheckable())
        self.assertTrue(self.panel.view_buttons["answer"].isChecked())
        self.assertIn("&", self.panel.view_buttons["files"].text() + "&")     # mnemonic present in the source label
        self.panel._set_view("files")
        self.assertIn("did not involve code", self.panel.viewer.toPlainText())
        self.panel._set_view("tests")
        self.assertIn("no code", self.panel.viewer.toPlainText().lower())
        self.panel._set_view("review")
        self.assertIn("review", self.panel.viewer.toPlainText().lower())

    def test_follow_up_question_is_answered_and_counted(self) -> None:
        self.finish()
        before = self.controller.snapshot().model_calls
        self.panel._set_view("ask")
        self.assertTrue(self.panel.ask_box.isVisibleTo(self.panel))
        self.assertFalse(self.panel.ask_send.isEnabled())                        # nothing typed yet
        self.panel.ask_input.setPlainText("How long does B last?")
        pump()
        self.assertTrue(self.panel.ask_send.isEnabled())
        self.assertIn("1 model call", self.panel.ask_status.text())
        self.panel.ask_send.click()
        self.assertTrue(wait_for(lambda: bool(self.controller.snapshot().followups)))
        pump(10)
        mission = self.controller.snapshot()
        self.assertEqual(mission.model_calls, before + 1)
        text = self.panel.viewer.toPlainText()
        self.assertIn("How long does B last?", text)
        self.assertIn("existing results and sources", text)
        self.assertIn("B lasts 5 years", text)
        self.assertEqual(self.panel.ask_input.toPlainText(), "")

    def test_rewrite_replaces_the_answer_and_history_shows_the_change(self) -> None:
        self.finish()
        old = self.controller.snapshot().final_artifact_id
        self.panel._set_view("ask")
        self.panel.ask_mode.setCurrentIndex(1)
        self.panel.ask_input.setPlainText("Make it shorter")
        pump()
        self.panel.ask_send.click()
        self.assertTrue(wait_for(lambda: self.controller.snapshot().final_artifact_id != old))
        pump(10)
        self.panel._set_view("answer")
        self.assertIn("Buy A", self.panel.viewer.toPlainText())
        self.assertNotIn("A cheap", self.panel.viewer.toPlainText())
        self.panel._set_view("history")
        self.assertTrue(self.panel.diff_toggle.isVisibleTo(self.panel))
        self.assertIn("Changes from", self.panel.viewer.toPlainText())
        self.panel.diff_toggle.setChecked(False)
        self.assertNotIn("Changes from", self.panel.viewer.toPlainText())
        self.assertIn("replaced", self.panel.viewer_choice.itemText(1))

    def test_research_is_blocked_with_a_reason_when_web_search_is_not_set_up(self) -> None:
        self.finish()
        self.panel._set_view("ask")
        self.panel.ask_mode.setCurrentIndex(2)
        self.panel.ask_input.setPlainText("Anything new?")
        pump()
        self.assertFalse(self.panel.ask_send.isEnabled())
        self.assertIn("needs web search", self.panel.ask_status.text())

    def test_ask_is_unavailable_until_there_is_a_final_answer(self) -> None:
        self.build()
        mission = Mission(goal="g", status=MissionStatus.RUNNING, tasks=[Task("T1", "t", AgentId.RESEARCHER, "i")])
        self.controller._view = mission
        self.panel.refresh()
        self.panel._set_view("ask")
        self.panel.ask_input.setPlainText("hi")
        pump()
        self.assertFalse(self.panel.ask_send.isEnabled())
        self.assertIn("final answer", self.panel.ask_status.text())

    def test_export_html_writes_a_standalone_page(self) -> None:
        self.finish()
        from unittest import mock
        folder = tempfile.mkdtemp(prefix="pybrowser-export-")
        target = os.path.join(folder, "answer.html")
        with mock.patch("app.ui.team_panel.QFileDialog.getSaveFileName", return_value=(target, "")):
            self.panel._export_html()
        page = open(target, encoding="utf-8").read()
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn("Buy A", page)
        self.assertIn("prefers-color-scheme", page)


if __name__ == "__main__":
    unittest.main()
