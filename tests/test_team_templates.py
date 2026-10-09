"""Mission templates: editable text, honest needs, enforced agent routing.

Run with:  QT_QPA_PLATFORM=offscreen python tests/run.py tests.test_team_templates
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.team import agents as agent_defs  # noqa: E402
from app.team import templates as tpl  # noqa: E402
from app.team.engine import Capabilities, TeamEngine  # noqa: E402
from app.team.limits import TeamLimits  # noqa: E402
from app.team.model import AgentId, Mission, MissionStatus, Source, SourceKind  # noqa: E402
from tests.test_team_engine import APPROVE, NO_SANDBOX, FakeClient, plan_json, task  # noqa: E402
from tests.test_team_ui import TeamUITestCase, happy_client, pump, setUpModule as _ui_setup, wait_for  # noqa: E402


def setUpModule() -> None:
    _ui_setup()


class Store(dict):
    def set(self, key, value):
        self[key] = value


class TemplateData(unittest.TestCase):
    def test_the_five_templates_exist_with_sensible_routing(self) -> None:
        self.assertEqual([t.label for t in tpl.TEMPLATES],
                         ["Compare tabs", "Research a topic", "Study guide", "Review code", "Draft a report"])
        study = tpl.BY_ID["study_guide"]
        self.assertEqual(study.agents, (AgentId.RESEARCHER, AgentId.WRITER))          # simple: no reviewer, no coder
        self.assertNotIn(AgentId.CODER, tpl.BY_ID["compare_tabs"].agents)
        self.assertIn(AgentId.CODER, tpl.BY_ID["review_code"].agents)

    def test_edits_are_stored_per_template_and_can_be_reset(self) -> None:
        store = Store()
        template = tpl.BY_ID["compare_tabs"]
        self.assertEqual(tpl.goal_for(store, template), template.goal)
        tpl.save_goal(store, template, "Compare these for a student budget.")
        self.assertEqual(tpl.goal_for(store, template), "Compare these for a student budget.")
        self.assertTrue(tpl.is_edited(store, template))
        self.assertEqual(tpl.goal_for(store, tpl.BY_ID["study_guide"]), tpl.BY_ID["study_guide"].goal)
        tpl.save_goal(store, template, template.goal)                                 # same as default = reset
        self.assertFalse(tpl.is_edited(store, template))
        self.assertEqual(tpl.goal_for(None, template), template.goal)

    def test_needs_report_what_is_missing_and_how_to_fix_it(self) -> None:
        template = tpl.BY_ID["compare_tabs"]
        states = {text: state for state, text, _ in tpl.check(template, tabs=1, material=1, web=False,
                                                              workspace=False, sandbox=False)}
        self.assertEqual(states["2 or more open tabs"], "missing")
        self.assertEqual(states["web search for outside facts"], "optional-missing")
        ok = tpl.check(template, tabs=2, material=2, web=True, workspace=False, sandbox=False)
        self.assertTrue(all(state == "ok" for state, _, _ in ok))
        code = tpl.check(tpl.BY_ID["review_code"], tabs=0, material=0, web=False, workspace=False, sandbox=False)
        self.assertEqual(code[0][0], "missing")
        self.assertIn("Workspace", code[0][2])
        self.assertEqual(tpl.check(tpl.BY_ID["review_code"], tabs=0, material=1, web=False, workspace=False,
                                   sandbox=False)[0][0], "ok")


class Routing(unittest.TestCase):
    def test_a_plan_using_a_disallowed_agent_is_rejected(self) -> None:
        plan = plan_json([task("T1", "researcher"), task("T2", "coder", ["T1"]), task("T3", "writer", ["T2"])])
        with self.assertRaises(agent_defs.PlanError) as ctx:
            agent_defs.parse_plan(plan, known_sources=set(), max_tasks=8, execution_available=False,
                                  allowed_agents=(AgentId.RESEARCHER, AgentId.WRITER))
        self.assertIn("may only use", str(ctx.exception))
        ok = agent_defs.parse_plan(plan_json([task("T1", "researcher"), task("T2", "writer", ["T1"])]),
                                   known_sources=set(), max_tasks=8, execution_available=False,
                                   allowed_agents=(AgentId.RESEARCHER, AgentId.WRITER))
        self.assertEqual([t.agent for t in ok.tasks], ["researcher", "writer"])          # and no reviewer is added

    def run_engine(self, plan_replies, allowed):
        client = FakeClient({"plan": plan_replies, "researcher": ["Notes [S1]"], "writer": ["Guide [S1]"],
                             "reviewer": [APPROVE], "final": ["Done [S1]"]})
        m = Mission(goal="Study these", allowed_agents=list(allowed), sources=[
            Source("S1", SourceKind.TAB, "Page", "https://a.example/", "Photosynthesis converts light.")])
        TeamEngine(m, lambda: client, TeamLimits(max_retries=0, max_backoff_s=1.0), Capabilities(NO_SANDBOX)).run()
        return m, client

    def test_the_engine_tells_the_coordinator_the_allowed_agents_and_runs_only_those(self) -> None:
        good = plan_json([task("T1", "researcher", sources=["S1"]), task("T2", "writer", ["T1"])])
        m, client = self.run_engine([good], (AgentId.RESEARCHER, AgentId.WRITER))
        self.assertIn("ALLOWED AGENTS", client.users("plan")[0])
        self.assertEqual(m.status, MissionStatus.COMPLETED)
        self.assertEqual({t.agent for t in m.tasks}, {"researcher", "writer"})
        self.assertEqual(client.roles().count("reviewer"), 0)

    def test_a_disallowed_plan_is_repaired_once_then_falls_back_within_the_allowed_agents(self) -> None:
        bad = plan_json([task("T1", "researcher"), task("T2", "coder", ["T1"]), task("T3", "writer", ["T2"])])
        m, client = self.run_engine([bad, bad], (AgentId.RESEARCHER, AgentId.WRITER))
        self.assertEqual(client.roles().count("plan"), 2)                                # one repair attempt
        self.assertEqual({t.agent for t in m.tasks}, {"researcher", "writer"})            # fallback was restricted
        self.assertTrue(any("fallback" in e.text.lower() for e in m.events))
        self.assertEqual(m.status, MissionStatus.COMPLETED)

    def test_no_template_means_the_coordinator_chooses_freely(self) -> None:
        good = plan_json([task("T1", "researcher", sources=["S1"]), task("T2", "writer", ["T1"]),
                          task("T3", "reviewer", ["T2"])])
        m, client = self.run_engine([good], ())
        self.assertNotIn("ALLOWED AGENTS", client.users("plan")[0])
        self.assertEqual(client.roles().count("reviewer"), 1)


class PanelTemplates(TeamUITestCase):
    def test_picking_a_template_fills_the_box_and_shows_needs_and_team(self) -> None:
        self.build()
        self.panel._pick_template("compare_tabs")
        pump()
        self.assertEqual(self.panel.goal.toPlainText(), tpl.BY_ID["compare_tabs"].goal)
        note = self.panel.template_note.text()
        self.assertIn("Researcher → Writer → Reviewer", note)
        self.assertIn("2 or more open tabs", note)
        self.assertIn("no other agents will be used", note)
        self.assertTrue(self.panel.template_buttons["compare_tabs"].isChecked())
        self.panel._pick_template("compare_tabs")                                         # again = drop it
        self.assertFalse(self.panel.template_note.isVisibleTo(self.panel))

    def test_users_own_wording_is_never_overwritten(self) -> None:
        self.build()
        self.panel.goal.setPlainText("Compare these two laptops for video editing")
        self.panel._pick_template("compare_tabs")
        self.assertEqual(self.panel.goal.toPlainText(), "Compare these two laptops for video editing")
        self.assertIn("Researcher", self.panel.template_note.text())                      # but routing applies

    def test_needs_update_as_sources_are_attached(self) -> None:
        self.build()
        self.panel._pick_template("study_guide")
        self.assertIn("✗", self.panel.template_note.text())
        from app.team.runner import paste_source
        self.panel._sources.append(paste_source(self.panel._sources, "Photosynthesis converts light into energy."))
        self.panel._refresh_included()
        self.assertNotIn("✗", self.panel.template_note.text())

    def test_the_mission_starts_with_the_templates_agents_only(self) -> None:
        client = happy_client(plan=[json.dumps({"summary": "s", "success_criteria": ["c"], "tasks": [
            task("T1", "researcher", sources=["S1"]), task("T2", "writer", ["T1"])]})])
        self.build(client)
        self.panel._pick_template("study_guide")
        self.start_mission(goal=self.panel.goal.toPlainText())
        self.assertTrue(wait_for(lambda: self.controller.snapshot().status == MissionStatus.COMPLETED))
        mission = self.controller.snapshot()
        self.assertEqual(mission.template_id, "study_guide")
        self.assertEqual(mission.allowed_agents, ["researcher", "writer"])
        self.assertIn("ALLOWED AGENTS", client.users("plan")[0])
        self.assertEqual(client.roles().count("reviewer"), 0)

    def test_edits_persist_in_settings(self) -> None:
        store = Store()
        self.build(settings=store)
        template = tpl.BY_ID["draft_report"]
        tpl.save_goal(store, template, "Draft a 1-page brief on: ")
        self.panel._pick_template("draft_report")
        self.assertEqual(self.panel.goal.toPlainText(), "Draft a 1-page brief on: ")
        self.assertIn("(edited)", self.panel.template_note.text())


class EverydayUsability(TeamUITestCase):
    def test_progress_text_comes_from_what_is_actually_running(self) -> None:
        import threading
        gate = threading.Event()
        client = happy_client()
        client.hold = {"writer": gate}
        self.build(client)
        try:
            self.start_mission()
            self.assertTrue(wait_for(lambda: "writer" in client.entered))
            pump(15)
            text = self.panel.run_status.text()
            self.assertRegex(text, r"Now: Writer on T2 since \d\d:\d\d:\d\d")
            self.assertNotIn("%", text)                                     # no invented percentages
            self.assertEqual(self.panel.progress.value(), 1)                # exactly the finished-task count
            self.assertEqual(self.panel.progress.maximum(), 3)
        finally:
            gate.set()
        self.assertTrue(wait_for(lambda: self.controller.snapshot().status == MissionStatus.COMPLETED))
        pump(10)
        self.assertNotIn("Now:", self.panel.run_status.text())

    def test_unfinished_tasks_describe_what_they_will_do_and_finished_ones_what_they_handed_over(self) -> None:
        self.build()
        self.start_mission()
        self.assertTrue(wait_for(lambda: self.controller.snapshot().status == MissionStatus.COMPLETED))
        pump(10)
        self.panel.run_tabs.setCurrentIndex(0)
        cards = " ".join(self.panel._agents_box.itemAt(i).widget().text()
                         for i in range(self.panel._agents_box.count() - 1))
        self.assertIn("task(s) finished", cards)

    def test_ctrl_enter_starts_a_mission_from_the_keyboard(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        self.build()
        self.panel.goal.setPlainText("Compare the products")
        self.panel.goal.setFocus()
        pump()
        QTest.keyClick(self.panel.goal, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(wait_for(lambda: self.controller.snapshot() is not None))

    def test_activity_has_an_honest_empty_state(self) -> None:
        self.build()
        mission = Mission(goal="g", status=MissionStatus.PLANNING)
        self.controller._view = mission
        self.panel.refresh()
        self.assertIn("Nothing has happened yet", self.panel.activity.placeholderText())


if __name__ == "__main__":
    unittest.main()
