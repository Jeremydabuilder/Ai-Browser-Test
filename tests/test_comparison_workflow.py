"""The product-comparison workflow, end to end, without a live model.

Reusable pieces are tested, not a hard-coded "comparison" mode: the user's
requirements survive planning, conflicts and unreadable sources stay visible in
the result, and a finished Team run can hand a bounded summary back to the
browsing Mission it came from. The model is scripted (tests/test_team_engine.py),
so nothing here says anything about real-model quality.

Run with:  QT_QPA_PLATFORM=offscreen python tests/run.py tests.test_comparison_workflow
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

import app.browser  # noqa: E402,F401

from app.team import handback  # noqa: E402
from app.team.engine import Capabilities, TeamEngine  # noqa: E402
from app.team.limits import TeamLimits  # noqa: E402
from app.team.model import (  # noqa: E402
    Mission, MissionStatus, Source, SourceKind, SourceStatus)
from tests.test_team_engine import (  # noqa: E402
    APPROVE, NO_SANDBOX, RESEARCH_WRITE_REVIEW, FakeClient, plan_json, task)

A = Source("S1", SourceKind.TAB, "Widget A", "https://a.example/widget", "Widget A costs $120 and ships to Canada.",
           retrieved="2026-10-24")
B = Source("S2", SourceKind.TAB, "Widget B", "https://b.example/widget", "Widget B costs $150. Shipping: US only.",
           retrieved="2026-10-24")
C = Source("S3", SourceKind.WEB, "Reseller page", "https://c.example/a", "Widget A costs $135 at this reseller.",
           depth="page", retrieved="2026-10-24")
BAD = Source("S4", SourceKind.TAB, "Widget C", "https://d.example/c", "", SourceStatus.INACCESSIBLE,
             "The page has no readable text (it may need sign-in).")
REQS = ["Under $130", "Ships to Canada"]


def run(client, sources, requirements=REQS, plan=RESEARCH_WRITE_REVIEW, **kw) -> Mission:
    mission = Mission(goal="Which widget should I buy?", sources=list(sources), requirements=list(requirements), **kw)
    TeamEngine(mission, lambda: client, TeamLimits(max_backoff_s=1.0), Capabilities(NO_SANDBOX)).run()
    return mission


def script(**over) -> FakeClient:
    base = {"plan": [RESEARCH_WRITE_REVIEW],
            "researcher": ["## Findings\n- Widget A costs $120 [S1].\n- Widget B costs $150 [S2].\n"
                           "## Gaps and uncertainty\n- Reseller [S3] says $135 for A, [S1] says $120.\n\n## Handoff\nok"],
            "writer": ["Widget A costs $120 and ships to Canada [S1].\n\n## Handoff\nok"],
            "reviewer": [json.dumps({"verdict": "approve", "summary": "ok", "issues": [], "criteria": [
                {"criterion": "Under $130", "met": True, "note": "A is $120 [S1]"},
                {"criterion": "Ships to Canada", "met": True, "note": "[S1]"}]})],
            "final": ["**Recommendation (team judgement):** Widget A.\n\nWidget A costs $120 [S1] but a reseller lists "
                      "$135 [S3]."]}
    base.update(over)
    return FakeClient(base)


class RequirementsSurvive(unittest.TestCase):
    def test_user_requirements_lead_the_criteria_verbatim_and_reach_every_stage(self):
        client = script()
        m = run(client, [A, B, C])
        self.assertEqual(m.status, MissionStatus.COMPLETED)
        self.assertEqual(m.success_criteria[:2], REQS)
        self.assertIn("Under $130", client.users("plan")[0])        # the planner saw them
        self.assertIn("Under $130", client.users("writer")[0])             # the writer's criteria list
        final = m.artifact(m.final_artifact_id).content
        self.assertIn("Your requirement - Under $130", final)              # the checklist labels them

    def test_a_plan_that_forgets_or_rewords_them_cannot_drop_them(self):
        plan = plan_json([task("T1", "researcher", sources=["S1"]), task("T2", "writer", ["T1"]),
                          task("T3", "reviewer", ["T2"])], criteria=("Cheap", "Under $130"))
        m = run(script(plan=[plan]), [A, B], plan=plan)
        self.assertEqual(m.success_criteria, ["Under $130", "Ships to Canada", "Cheap"])

    def test_a_run_without_requirements_is_unchanged(self):
        m = run(script(), [A, B], requirements=[])
        self.assertEqual(m.success_criteria, ["Answers the mission"])

    def test_requirements_survive_a_save_and_reload(self):
        m = run(script(), [A, B])
        again = Mission.from_dict(json.loads(json.dumps(m.to_dict())))
        self.assertEqual(again.requirements, REQS)


class EvidenceAndHonesty(unittest.TestCase):
    def test_conflicting_sources_and_unreadable_pages_remain_visible_in_the_result(self):
        m = run(script(), [A, B, C, BAD])
        final = m.artifact(m.final_artifact_id).content
        self.assertIn("$135", final)                                   # the dissent is in the answer...
        self.assertIn("## Could not be read", final)                   # ...and the failed page is reported
        self.assertIn("Widget C", final)
        self.assertIn("sign-in", final)
        self.assertIn("(page text retrieved 2026-10-24", final)        # retrieval information is kept
        self.assertIn("https://a.example/widget", final)

    def test_the_unreadable_page_is_listed_as_inaccessible_but_never_sent_as_content(self):
        client = script()
        run(client, [A, B, BAD])
        prompts = " ".join(u for _r, u in client.calls)
        self.assertNotIn('"id": "S4"', prompts)                       # no fenced content block for it
        self.assertIn("(attached tab, inaccessible) Widget C", prompts)  # the manifest is honest about it

    def test_a_figure_the_sources_do_not_contain_is_flagged_not_trusted(self):
        client = script(final=["Widget A costs $99 and ships to Canada with a long warranty included [S1]."])
        m = run(client, [A, B])
        final = m.artifact(m.final_artifact_id).content
        self.assertIn("## Citation check (automatic)", final)
        self.assertIn("figure(s) not in the source: 99", final)
        self.assertIn("not a fact check", final)

    def test_similar_claims_with_different_figures_are_listed_as_a_possible_conflict(self):
        client = script(final=["Widget A battery lasts 12 hours in testing [S1]. "
                               "Widget A battery lasts 18 hours in testing [S3]."])
        m = run(client, [A, B, C])
        self.assertIn("Possible conflict", m.artifact(m.final_artifact_id).content)


def finished(**kw) -> Mission:
    m = run(script(), [A, B, C])
    for k, v in kw.items():
        setattr(m, k, v)
    return m


class HandBack(unittest.TestCase):
    def test_summary_is_the_answer_plus_requirement_check_without_the_evidence_appendix(self):
        m = finished(id=7)
        text = handback.summary_for_mission(m)
        self.assertIn("Recommendation (team judgement)", text)
        self.assertIn("Met: Under $130", text)
        self.assertNotIn("## Sources", text)
        self.assertIn("AI Team run #7", text)
        self.assertIn("not proof", text)

    def test_summary_fits_a_mission_result_and_keeps_the_footer(self):
        m = finished(id=3)
        final = m.artifact(m.final_artifact_id)
        final.content = "Long answer. " * 2000 + "\n## Sources\n- [S1]"
        text = handback.summary_for_mission(m)
        self.assertLessEqual(len(text), handback.SUMMARY_LIMIT)
        self.assertTrue(text.endswith("is not proof."))

    def test_no_summary_until_there_is_a_real_final_result(self):
        for status in (MissionStatus.PLANNING, MissionStatus.RUNNING, MissionStatus.FAILED, MissionStatus.CANCELLED):
            m = finished()
            m.status = status
            self.assertIsNone(handback.summary_for_mission(m), status)
        m = finished()
        m.final_artifact_id = ""
        self.assertIsNone(handback.summary_for_mission(m))

    def test_open_issues_are_stated_in_the_summary(self):
        m = finished(id=2)
        m.status = MissionStatus.COMPLETED_WITH_ISSUES
        self.assertIn("open issues", handback.summary_for_mission(m))

    def test_handoff_from_a_mission_carries_goal_requirements_notes_and_pages(self):
        mission = SimpleNamespace(
            id=5, goal="Pick a widget", constraints=("Under $130",),
            findings=(SimpleNamespace(text="A is cheap", source_url="https://a.example"),),
            pages=(SimpleNamespace(url="https://a.example"), SimpleNamespace(url="https://a.example"),
                   SimpleNamespace(url="https://b.example")))
        h = handback.handoff_from_mission(mission)
        self.assertEqual((h.goal, h.requirements, h.linked_mission_id), ("Pick a widget", ["Under $130"], 5))
        self.assertEqual(h.urls, ["https://a.example", "https://b.example"])
        self.assertIn("not independently verified", h.notes)
        self.assertIn("A is cheap", h.notes)


if __name__ == "__main__":
    unittest.main()
