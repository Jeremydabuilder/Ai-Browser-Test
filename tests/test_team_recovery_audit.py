"""Recovery audit: Retry / Resume / Skip / cancel / Revise again keep valid
work, never hide skipped work behind a success status, and cannot spend past
the limits.

Run with:  QT_QPA_PLATFORM=offscreen python tests/run.py tests.test_team_recovery_audit
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.agent.claude_client import ClaudeError  # noqa: E402
from app.team.engine import Capabilities, TeamEngine  # noqa: E402
from app.team.limits import TeamLimits  # noqa: E402
from app.team.llm import ErrorKind  # noqa: E402
from app.team.model import Mission, MissionStatus, Source, SourceKind, TaskStatus  # noqa: E402
from tests.test_team_engine import (  # noqa: E402
    APPROVE, NO_SANDBOX, RESEARCH_WRITE_REVIEW, FakeClient, plan_json, task,
)

BOOM = ClaudeError("provider down", retryable=False)
ISSUE = {"task": "T2", "severity": "blocking", "where": "Intro", "problem": "Claim lacks support",
         "evidence": "no [S#]", "change": "Cite S1"}
BAD = json.dumps({"verdict": "revise", "summary": "Fix it.", "issues": [ISSUE]})


def mission():
    return Mission(goal="Compare the widgets", sources=[
        Source("S1", SourceKind.TAB, "Widget A", "https://a.example/widget", "Widget A costs $10.")])


def start(client, limits=None, m=None):
    m = m or mission()
    engine = TeamEngine(m, lambda: client, limits or TeamLimits(max_retries=0, max_backoff_s=1.0),
                        Capabilities(NO_SANDBOX))
    engine.run()
    return engine, m


def script(**over):
    base = {"plan": [RESEARCH_WRITE_REVIEW], "researcher": ["A is $10 [S1]"], "writer": ["Draft [S1]"],
            "reviewer": [APPROVE], "final": ["Result [S1]"]}
    base.update(over)
    return FakeClient(base)


class SkipIsNeverASilentSuccess(unittest.TestCase):
    def test_skipping_the_writer_completes_with_issues_and_says_so(self) -> None:
        client = script(writer=[BOOM])
        engine, m = start(client)
        self.assertEqual(m.task("T2").status, TaskStatus.FAILED)
        engine.run(skip={"T2"})
        self.assertEqual(m.task("T2").status, TaskStatus.SKIPPED)
        self.assertEqual(m.status, MissionStatus.COMPLETED_WITH_ISSUES)
        final = m.artifact(m.final_artifact_id).content
        self.assertIn("You skipped T2", final)
        self.assertIn("does not include its work", final)
        self.assertIn("TASKS THE USER CHOSE TO SKIP", client.users("final")[-1])
        self.assertIn("skipped", client.users("reviewer")[-1])      # the reviewer was told

    def test_skipping_the_reviewer_says_the_result_was_not_reviewed(self) -> None:
        client = script(reviewer=[BOOM])
        engine, m = start(client)
        engine.run(skip={"T3"})
        self.assertEqual(m.status, MissionStatus.COMPLETED_WITH_ISSUES)
        self.assertIn("it was not reviewed", m.artifact(m.final_artifact_id).content)

    def test_skipping_every_producer_cannot_succeed(self) -> None:
        client = script(researcher=[BOOM])
        plan = plan_json([task("T1", "researcher", sources=["S1"]), task("T2", "reviewer", ["T1"])])
        client = script(plan=[plan], researcher=[BOOM])
        engine, m = start(client)
        engine.run(skip={"T1"})
        self.assertEqual(m.status, MissionStatus.FAILED)
        self.assertEqual(m.final_artifact_id, "")


class SpendingLimits(unittest.TestCase):
    def test_a_mission_cannot_spend_past_its_lifetime_cap_by_retrying(self) -> None:
        m = mission()
        limits = TeamLimits(max_model_calls=4, max_retries=0, max_backoff_s=1.0)
        m.model_calls = 4 * 3                       # three runs' worth already spent
        client = script()
        _, m = start(client, limits, m)
        self.assertEqual(client.calls, [])
        self.assertEqual(m.status, MissionStatus.FAILED)
        self.assertEqual(m.error_kind, ErrorKind.BUDGET)
        self.assertIn("across its runs", m.error)

    def test_each_run_has_its_own_budget_but_calls_accumulate(self) -> None:
        limits = TeamLimits(max_model_calls=6, max_retries=0, max_backoff_s=1.0)
        client = script(writer=[BOOM, "Draft [S1]"])
        engine, m = start(client, limits)
        first = m.model_calls
        engine.run()
        self.assertEqual(m.status, MissionStatus.COMPLETED)
        self.assertGreater(m.model_calls, first)

    def test_auto_retries_and_extra_rounds_are_bounded(self) -> None:
        limited = ClaudeError("rate", retryable=True, retry_after=0.01)
        limits = TeamLimits(max_model_calls=8, max_retries=0, max_backoff_s=0.05, rate_limit_requeues=50,
                            rate_limit_cooldown_s=3.0)
        client = script(writer=[limited])
        _, m = start(client, limits)
        self.assertLessEqual(client.roles().count("writer"), limits.rate_limit_requeues + 1)
        self.assertLessEqual(len(client.calls), limits.max_model_calls)


class CancellationAndResume(unittest.TestCase):
    def test_cancelled_in_flight_work_is_discarded_and_finished_work_is_kept(self) -> None:
        gate = threading.Event()
        client = script()
        client.hold = {"writer": gate}
        m = mission()
        engine = TeamEngine(m, lambda: client, TeamLimits(max_retries=0, max_backoff_s=1.0), Capabilities(NO_SANDBOX))
        thread = threading.Thread(target=engine.run)
        thread.start()
        self.assertTrue(client.entered.setdefault("writer", threading.Event()).wait(10))
        engine.cancel()
        gate.set()
        thread.join(15)
        self.assertEqual(m.status, MissionStatus.CANCELLED)
        self.assertEqual(m.task("T1").status, TaskStatus.DONE)
        self.assertEqual(m.task("T2").status, TaskStatus.CANCELLED)
        self.assertEqual(m.task("T2").outputs, [])                    # nothing half-finished is kept
        client.hold = {}
        researchers = client.roles().count("researcher")
        engine.run()
        self.assertEqual(m.status, MissionStatus.COMPLETED)
        self.assertEqual(client.roles().count("researcher"), researchers)

    def test_retrying_a_task_reruns_what_it_blocked_with_the_new_output(self) -> None:
        client = script(writer=[BOOM, "Fresh draft [S1]"])
        engine, m = start(client)
        self.assertEqual(m.task("T3").status, TaskStatus.BLOCKED)
        engine.run(retry_only={"T2"})
        self.assertEqual(m.status, MissionStatus.COMPLETED)
        self.assertIn("Fresh draft", client.users("reviewer")[-1])

    def test_revise_again_does_not_replay_a_stale_review_while_a_newer_one_is_pending(self) -> None:
        client = script(reviewer=[BAD, BOOM, APPROVE])
        engine, m = start(client, TeamLimits(max_retries=0, max_revision_rounds=1, max_backoff_s=1.0))
        self.assertEqual(m.task("T5").status, TaskStatus.FAILED)       # the re-review failed
        writers = client.roles().count("writer")
        engine.run(extra_round=True)
        self.assertEqual(m.status, MissionStatus.COMPLETED)
        self.assertEqual(client.roles().count("writer"), writers)     # no second revision on a stale verdict


class StaleDependentsAreInvalidated(unittest.TestCase):
    """When upstream work changes, anything built on the old version is re-done, never combined with the new."""

    def test_revised_research_makes_the_old_draft_and_review_stale(self) -> None:
        plan = plan_json([task("T1", "researcher", sources=["S1"]), task("T2", "writer", ["T1"]),
                          task("T3", "reviewer", ["T2"])])
        ask_for_research = json.dumps({"verdict": "revise", "summary": "Research is thin.", "issues": [
            {"task": "T1", "severity": "blocking", "where": "Findings", "problem": "Price is unsourced",
             "evidence": "no [S#]", "change": "Add the price from S1"}]})
        client = script(plan=[plan], researcher=["OLD-NOTES A is cheap [S1]", "NEW-NOTES A costs $10 [S1]"],
                        writer=["OLD-DRAFT built on old notes", "NEW-DRAFT built on new notes"],
                        reviewer=[ask_for_research, APPROVE])
        _, m = start(client)
        self.assertEqual(m.status, MissionStatus.COMPLETED)
        self.assertEqual(client.roles().count("researcher"), 2)
        self.assertEqual(client.roles().count("writer"), 2)            # the draft was redone on the new notes
        writer_again = client.users("writer")[1]
        self.assertIn("NEW-NOTES", writer_again)
        self.assertNotIn("OLD-NOTES", writer_again)
        second_review = client.users("reviewer")[1]
        self.assertIn("NEW-DRAFT", second_review)
        self.assertNotIn("OLD-DRAFT", second_review)
        self.assertNotIn("OLD-NOTES", second_review)
        final_prompt = client.users("final")[-1]
        self.assertIn("NEW-DRAFT", final_prompt)
        self.assertNotIn("OLD-DRAFT", final_prompt)
        old = [a for a in m.artifacts if "OLD-DRAFT" in a.content]
        self.assertTrue(old and all(a.meta.get("replaced") for a in old))      # kept for history, marked replaced

    def test_retrying_a_failed_producer_reruns_a_review_that_ran_without_it(self) -> None:
        plan = plan_json([task("T1", "researcher", sources=["S1"]), task("T2", "writer", ["T1"]),
                          task("T3", "writer", ["T1"], title="Appendix"), task("T4", "reviewer", ["T2", "T3"])])
        client = script(plan=[plan], writer=["MAIN-DRAFT", BOOM, "APPENDIX-DRAFT"])
        limits = TeamLimits(max_retries=0, max_backoff_s=1.0, max_concurrency=1)
        engine, m = start(client, limits)
        self.assertEqual(m.task("T3").status, TaskStatus.FAILED)
        self.assertEqual(m.task("T4").status, TaskStatus.DONE)         # reviewed without the appendix
        self.assertNotIn("APPENDIX", client.users("reviewer")[0])
        engine.run(retry_only={"T3"})
        self.assertEqual(client.roles().count("reviewer"), 2)          # the stale review was redone
        self.assertIn("APPENDIX-DRAFT", client.users("reviewer")[1])
        self.assertEqual(client.roles().count("researcher"), 1)
        self.assertEqual(m.status, MissionStatus.COMPLETED)

    def test_unchanged_upstream_leaves_finished_work_alone(self) -> None:
        client = script()
        engine, m = start(client)
        calls = len(client.calls)
        engine.run()
        self.assertEqual(len(client.calls), calls + 1)                 # only the final assembly
        self.assertEqual(client.roles().count("reviewer"), 1)


if __name__ == "__main__":
    unittest.main()
