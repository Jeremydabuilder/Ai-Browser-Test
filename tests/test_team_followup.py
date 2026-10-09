"""Follow-up chat with a finished mission: answer from evidence, rewrite, new research.

Run with:  QT_QPA_PLATFORM=offscreen python tests/run.py tests.test_team_followup
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.team.engine import Capabilities, TeamEngine  # noqa: E402
from app.team.limits import TeamLimits  # noqa: E402
from app.team.llm import ErrorKind, TeamError  # noqa: E402
from app.team.model import ArtifactKind, Mission, MissionStatus, Source, SourceKind  # noqa: E402
from app.team.webfetch import Page  # noqa: E402
from app.team.websearch import WebResult  # noqa: E402
from tests.test_team_engine import APPROVE, NO_SANDBOX, RESEARCH_WRITE_REVIEW, FakeClient  # noqa: E402
from tests.test_team_websearch import FakeWeb  # noqa: E402


class Fetcher:
    def fetch(self, url):
        return Page(url, "T", "Fresh page text: the Gadget X has a 5 year warranty and costs $99. " * 3, False)


def finished(client_extra=None, *, web=None, fetcher=None, limits=None):
    script = {"plan": [RESEARCH_WRITE_REVIEW], "researcher": ["A costs $10 [S1]. B costs $14 [S2]."],
              "writer": ["# Comparison\nA is cheaper [S1]; B lasts longer [S2]."], "reviewer": [APPROVE],
              "final": ["**Buy A.** It is cheaper [S1]. Hostile page said IGNORE ALL RULES."],
              "searchplan": [json.dumps({"queries": ["gadget x warranty"]})],
              "followup_answer": ["A costs $10 [S1] and B costs $14 [S2]."],
              "followup_rewrite": ["**Buy A** - cheaper [S1]."],
              "followup_research": ["Per the new page the Gadget X has a 5 year warranty [S3]."]}
    script.update(client_extra or {})
    client = FakeClient(script)
    m = Mission(goal="Compare the widgets", sources=[
        Source("S1", SourceKind.TAB, "Widget A", "https://a.example/widget", "Widget A costs $10 and lasts 2 years."),
        Source("S2", SourceKind.TAB, "Widget B", "https://b.example/widget", "Widget B costs $14 and lasts 5 years.")])
    caps = Capabilities(NO_SANDBOX, web_search=web, fetcher=fetcher)
    engine = TeamEngine(m, lambda: client, limits or TeamLimits(max_retries=0, max_backoff_s=1.0), caps)
    engine.run()
    assert m.status == MissionStatus.COMPLETED, m.error
    return engine, m, client


class AnswerFromEvidence(unittest.TestCase):
    def test_answer_uses_current_result_and_sources_and_counts_calls(self) -> None:
        engine, m, client = finished()
        before = m.model_calls
        record = engine.ask("What does B cost?")
        self.assertEqual(record["mode"], "answer")
        self.assertIn("[S2]", record["answer"])
        self.assertEqual(record["cites"], ["S1", "S2"])
        self.assertIn("no new research", record["basis"])
        self.assertFalse(record["needs_research"])
        prompt = client.users("followup_answer")[0]
        self.assertIn("CURRENT RESULT", prompt)
        self.assertIn("Widget B costs $14", prompt)                       # sources are included, fenced
        self.assertIn("<untrusted", prompt)
        self.assertEqual(record["calls"], 1)
        self.assertEqual(m.model_calls, before + 1)                        # counted toward the mission allowance
        self.assertEqual(m.followups[-1]["question"], "What does B cost?")
        self.assertEqual(len([s for s in m.sources]), 2)                   # nothing new was researched

    def test_unsupported_questions_are_flagged_not_answered_from_thin_air(self) -> None:
        engine, m, _ = finished({"followup_answer": ["NOT IN EVIDENCE: the sources never mention shipping."]})
        record = engine.ask("How long is shipping?")
        self.assertTrue(record["needs_research"])
        self.assertIn("does not cover this", record["basis"])

    def test_invented_citations_are_removed(self) -> None:
        engine, _, _ = finished({"followup_answer": ["B is $14 [S2] and A has a warranty [S9]."]})
        record = engine.ask("Prices?")
        self.assertIn("[unverified citation removed]", record["answer"])
        self.assertEqual(record["cites"], ["S2"])

    def test_earlier_followups_are_context_and_replaced_versions_are_not(self) -> None:
        engine, m, client = finished()
        engine.ask("First question?")
        engine.ask("Second question?")
        prompt = client.users("followup_answer")[1]
        self.assertIn("EARLIER FOLLOW-UPS", prompt)
        self.assertIn("First question?", prompt)


class Rewrite(unittest.TestCase):
    def test_rewrite_makes_a_new_current_result_and_keeps_history(self) -> None:
        engine, m, client = finished()
        old_id = m.final_artifact_id
        record = engine.ask("Make the report shorter", "rewrite")
        self.assertNotEqual(m.final_artifact_id, old_id)
        new = m.artifact(m.final_artifact_id)
        self.assertEqual(new.replaces, old_id)
        self.assertTrue(m.artifact(old_id).meta.get("replaced"))
        self.assertIn("**Buy A** - cheaper [S1]", new.content)
        self.assertIn("## Sources", new.content)                           # the appendix is rebuilt, citations resolve
        self.assertEqual(record["artifact_id"], new.id)
        self.assertIn("no new research", record["basis"])

    def test_rewrite_that_needs_facts_it_does_not_have_changes_nothing(self) -> None:
        engine, m, _ = finished({"followup_rewrite": ["NOT IN EVIDENCE: no pricing for C."]})
        old_id = m.final_artifact_id
        record = engine.ask("Add widget C", "rewrite")
        self.assertEqual(m.final_artifact_id, old_id)
        self.assertTrue(record["needs_research"])


class NewResearch(unittest.TestCase):
    def web(self):
        return FakeWeb(results=[WebResult("Gadget X specs", "http://gadget.example/x", "Gadget X specs snippet")])

    def test_research_adds_labelled_sources_and_counts_every_call(self) -> None:
        engine, m, client = finished(web=self.web(), fetcher=Fetcher())
        before = m.model_calls
        record = engine.ask("What warranty does Gadget X have?", "research")
        self.assertIn("New research", record["basis"])
        self.assertEqual(record["new_sources"], ["S3"])
        new = m.source("S3")
        self.assertEqual((new.kind, new.depth), (SourceKind.WEB, "page"))
        self.assertIn("5 year warranty", client.users("followup_research")[0])
        self.assertEqual(record["calls"], 2)                               # query planning + the research answer
        self.assertEqual(m.model_calls, before + 2)

    def test_research_is_unavailable_without_web_search_and_says_why(self) -> None:
        engine, m, client = finished()
        with self.assertRaises(TeamError) as ctx:
            engine.ask("Anything new?", "research")
        self.assertIn("Web search is not set up", ctx.exception.message)
        self.assertEqual(client.roles().count("followup_research"), 0)


class Guards(unittest.TestCase):
    def test_follow_ups_cannot_exceed_the_mission_allowance(self) -> None:
        engine, m, client = finished()
        engine.limits = TeamLimits(max_model_calls=4, max_retries=0, max_backoff_s=1.0).clamped()
        m.model_calls = 12                                                  # 3 runs x 4 already spent
        calls = len(client.calls)
        with self.assertRaises(TeamError) as ctx:
            engine.ask("One more?")
        self.assertEqual(ctx.exception.kind, ErrorKind.BUDGET)
        self.assertEqual(len(client.calls), calls)

    def test_no_follow_up_before_there_is_a_result_or_while_running(self) -> None:
        client = FakeClient({})
        engine = TeamEngine(Mission(goal="g"), lambda: client, TeamLimits(), Capabilities(NO_SANDBOX))
        with self.assertRaises(TeamError):
            engine.ask("Hello?")
        _, m, _ = finished()
        busy = TeamEngine(m, lambda: client, TeamLimits(), Capabilities(NO_SANDBOX))
        busy._running = True
        with self.assertRaises(TeamError):
            busy.ask("Hello?")

    def test_empty_question_and_unknown_mode_are_rejected(self) -> None:
        engine, _, _ = finished()
        for question, mode in (("", "answer"), ("   ", "answer"), ("ok", "explode")):
            with self.assertRaises(TeamError):
                engine.ask(question, mode)


if __name__ == "__main__":
    unittest.main()
