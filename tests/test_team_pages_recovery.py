"""Team: reading full pages from search results, explicit handoffs, actionable
reviewer feedback and recovery (rate limits, retry of one task, skip, extra
review round) - all against scripted fakes; no network.

Run with:  QT_QPA_PLATFORM=offscreen python tests/run.py tests.test_team_pages_recovery
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.agent.claude_client import ClaudeError  # noqa: E402
from app.team import agents  # noqa: E402
from app.team.engine import Capabilities, TeamEngine  # noqa: E402
from app.team.limits import TeamLimits  # noqa: E402
from app.team.model import Mission, MissionStatus, Source, SourceKind, TaskStatus  # noqa: E402
from app.team.webfetch import FetchError, FetchErrorKind, Page  # noqa: E402
from tests.test_team_engine import (  # noqa: E402
    APPROVE, NO_SANDBOX, RESEARCH_WRITE_REVIEW, FakeClient, plan_json, task,
)
from tests.test_team_websearch import FakeWeb  # noqa: E402


class FakeFetcher:
    def __init__(self, pages=None, fail=()):
        self.pages = pages or {}
        self.fail = dict(fail)
        self.fetched: list[str] = []

    def fetch(self, url):
        self.fetched.append(url)
        if url in self.fail:
            raise FetchError(FetchErrorKind.BLOCKED, self.fail[url])
        text = self.pages.get(url, "Full page text about the topic. " * 5)
        return Page(url, "Full page", text, False)


def run(client, *, fetcher=None, web=None, limits=None, mission=None, **kw):
    mission = mission or Mission(goal="Compare the widgets", web_search=True, sources=[
        Source("S1", SourceKind.TAB, "Widget A", "https://a.example/widget", "Widget A costs $10.")])
    caps = Capabilities(NO_SANDBOX, web_search=web if web is not None else FakeWeb(), fetcher=fetcher)
    engine = TeamEngine(mission, lambda: client, limits or TeamLimits(max_backoff_s=1.0), caps)
    engine.run(**kw)
    return engine, mission


def script(**over):
    base = {"plan": [RESEARCH_WRITE_REVIEW],
            "searchplan": [json.dumps({"queries": ["q1"]})],
            "researcher": ["A costs $10 [S1]. B has a 3 year warranty [S2].\n\n## Handoff\nDone; warranty from a full page."],
            "writer": ["Draft [S1] [S2]\n\n## Handoff\nPrices are verified; the warranty is not."],
            "reviewer": [APPROVE], "final": ["Result [S1] [S2]"]}
    base.update(over)
    return FakeClient(base)


class PageReadingTests(unittest.TestCase):
    def test_top_results_are_read_in_full_and_cited_as_pages(self) -> None:
        fetcher = FakeFetcher()
        client = script()
        _, mission = run(client, fetcher=fetcher)
        self.assertEqual(mission.status, MissionStatus.COMPLETED)
        web = [s for s in mission.sources if s.kind == SourceKind.WEB]
        self.assertTrue(web and all(s.depth == "page" and s.retrieved for s in web))
        self.assertIn('"origin": "web page (read in full)"', client.users("researcher")[0])
        final = mission.artifact(mission.final_artifact_id).content
        self.assertIn("## Web pages read", final)
        self.assertIn("retrieved ", final)
        self.assertIn("read in full", final)

    def test_a_blocked_page_keeps_its_snippet_and_says_why(self) -> None:
        bad = "https://reviews.example/b-warranty"
        fetcher = FakeFetcher(fail={bad: "That address is on a private network."})
        _, mission = run(script(), fetcher=fetcher)
        blocked = next(s for s in mission.sources if s.url == bad)
        self.assertEqual(blocked.depth, "snippet")
        self.assertIn("private network", blocked.note)
        self.assertTrue(any("Could not open reviews.example" in e.text for e in mission.events))
        final = mission.artifact(mission.final_artifact_id).content
        self.assertIn("snippets only", final)

    def test_page_count_is_capped_per_task(self) -> None:
        many = [__import__("app.team.websearch", fromlist=["x"]).WebResult(f"P{i}", f"https://p{i}.example/", "snippet text")
                for i in range(6)]
        fetcher = FakeFetcher()
        run(script(), fetcher=fetcher, web=FakeWeb(results=many), limits=TeamLimits(max_fetch_pages=2, max_backoff_s=1.0))
        self.assertEqual(len(fetcher.fetched), 2)

    def test_zero_pages_or_no_fetcher_means_snippets_only(self) -> None:
        _, mission = run(script(), fetcher=None)
        self.assertTrue(all(s.depth == "snippet" for s in mission.sources if s.kind == SourceKind.WEB))

    def test_a_retry_does_not_search_or_fetch_again(self) -> None:
        fetcher, web = FakeFetcher(), FakeWeb()
        client = script(writer=[ClaudeError("provider down", retryable=False), "Draft [S1]\n\n## Handoff\nok"])
        engine, mission = run(client, fetcher=fetcher, web=web, limits=TeamLimits(max_retries=0, max_backoff_s=1.0))
        self.assertEqual(mission.task("T2").status, TaskStatus.FAILED)
        first_fetches, first_queries = len(fetcher.fetched), len(web.queries)
        self.assertGreater(first_fetches, 0)
        engine.run()
        self.assertEqual(mission.status, MissionStatus.COMPLETED)
        self.assertEqual((len(fetcher.fetched), len(web.queries)), (first_fetches, first_queries))
        self.assertEqual(client.roles().count("researcher"), 1)      # finished work is not redone


class HandoffTests(unittest.TestCase):
    def test_handoff_notes_are_split_off_and_passed_downstream(self) -> None:
        client = script()
        _, mission = run(client, fetcher=FakeFetcher())
        researcher = mission.task("T1")
        self.assertIn("warranty from a full page", researcher.handoff)
        notes = mission.artifact(researcher.outputs[0]).content
        self.assertNotIn("## Handoff", notes)
        writer_prompt = client.users("writer")[0]
        self.assertIn("HANDOFF NOTES FROM TEAMMATES", writer_prompt)
        self.assertIn("warranty from a full page", writer_prompt)
        self.assertIn("Prices are verified", client.users("reviewer")[0])

    def test_missing_handoff_is_fine(self) -> None:
        self.assertEqual(agents.split_handoff("Just text"), ("Just text", ""))
        body, note = agents.split_handoff("Body\n\n## Handoff\n- done\n- unsure about X")
        self.assertEqual(body, "Body")
        self.assertIn("unsure about X", note)


class ReviewerFeedbackTests(unittest.TestCase):
    ISSUE = {"task": "T2", "severity": "blocking", "where": "Price section",
             "problem": "Price of B is missing", "evidence": "Source S2 says $14", "change": "Add B's price $14 [S2]"}
    MINOR = {"task": "T2", "severity": "minor", "problem": "Title is dull", "change": "Retitle"}

    def reviews(self):
        return [json.dumps({"verdict": "revise", "summary": "Fix the price.",
                            "criteria": [{"criterion": "Answers the mission", "met": False, "note": "no price"}],
                            "issues": [self.ISSUE, self.MINOR]}),
                json.dumps({"verdict": "approve", "summary": "Fixed.",
                            "criteria": [{"criterion": "Answers the mission", "met": True, "note": ""}],
                            "previous": [{"id": "B1", "status": "fixed"}], "issues": []})]

    def test_revision_carries_location_evidence_and_exact_change_but_not_minor_polish(self) -> None:
        client = script(reviewer=self.reviews())
        _, mission = run(client, fetcher=FakeFetcher())
        self.assertEqual(mission.status, MissionStatus.COMPLETED)
        writer_calls = client.users("writer")
        self.assertEqual(len(writer_calls), 2)
        revision = writer_calls[1]
        self.assertIn("B1 [Price section]", revision)
        self.assertIn("Source S2 says $14", revision)
        self.assertIn("REQUIRED: Add B's price $14 [S2]", revision)
        self.assertNotIn("REQUIRED: Retitle", revision)       # optional polish is not a requirement
        second_review = client.users("reviewer")[1]
        self.assertIn("PREVIOUS BLOCKING ISSUES", second_review)
        self.assertIn("B1", second_review)
        self.assertEqual([x.split(" ")[0] for x in mission.suggestions], [])       # cleared after approval
        self.assertTrue(all(c["met"] for c in mission.criteria_check))

    def test_minor_only_feedback_approves_and_is_listed_as_optional(self) -> None:
        review = json.dumps({"verdict": "revise", "summary": "Polish only.", "issues": [self.MINOR]})
        client = script(reviewer=[review])
        _, mission = run(client, fetcher=FakeFetcher())
        self.assertEqual(client.roles().count("writer"), 1)
        self.assertEqual(mission.status, MissionStatus.COMPLETED)
        final = mission.artifact(mission.final_artifact_id).content
        self.assertIn("## Optional improvements", final)
        self.assertIn("Title is dull", final)

    def test_open_blocking_issues_surface_in_the_result(self) -> None:
        review = json.dumps({"verdict": "revise", "summary": "Still wrong.", "issues": [self.ISSUE]})
        client = script(reviewer=[review])
        _, mission = run(client, fetcher=FakeFetcher(), limits=TeamLimits(max_revision_rounds=1, max_backoff_s=1.0))
        self.assertEqual(mission.status, MissionStatus.COMPLETED_WITH_ISSUES)
        self.assertIn("B1", mission.unresolved_issues[0])

    def test_one_more_round_can_be_requested_after_the_limit(self) -> None:
        bad = json.dumps({"verdict": "revise", "summary": "Wrong.", "issues": [self.ISSUE]})
        client = script(reviewer=[bad, bad, json.dumps({"verdict": "approve", "summary": "ok", "issues": []})])
        engine, mission = run(client, fetcher=FakeFetcher(), limits=TeamLimits(max_revision_rounds=1, max_backoff_s=1.0))
        self.assertEqual(mission.status, MissionStatus.COMPLETED_WITH_ISSUES)
        writers_before = client.roles().count("writer")
        engine.run(extra_round=True)
        self.assertEqual(mission.status, MissionStatus.COMPLETED)
        self.assertGreater(client.roles().count("writer"), writers_before)
        self.assertEqual(client.roles().count("researcher"), 1)


class RecoveryTests(unittest.TestCase):
    def test_rate_limited_task_waits_and_succeeds_without_failing_the_mission(self) -> None:
        limited = ClaudeError("Groq hit a temporary rate limit.", retryable=True, retry_after=0.01)
        client = script(writer=[limited, limited, limited, limited, "Draft [S1]\n\n## Handoff\nok"])
        limits = TeamLimits(max_retries=1, max_backoff_s=0.05, rate_limit_requeues=2, rate_limit_cooldown_s=3.0)
        started = time.monotonic()
        _, mission = run(client, fetcher=FakeFetcher(), limits=limits)
        self.assertEqual(mission.status, MissionStatus.COMPLETED)
        self.assertTrue(mission.throttled)
        self.assertGreaterEqual(time.monotonic() - started, 2.9)
        self.assertTrue(any("rate limit" in e.text and "waiting" in e.text for e in mission.events))
        self.assertEqual(client.roles().count("researcher"), 1)

    def test_retry_only_one_task_and_skip_another(self) -> None:
        boom = ClaudeError("provider down", retryable=False)
        plan = plan_json([task("T1", "researcher", sources=["S1"]), task("T2", "writer", ["T1"]),
                          task("T3", "writer", ["T1"], title="Appendix"), task("T4", "reviewer", ["T2", "T3"])])
        client = script(plan=[plan], writer=[boom, boom, "Draft [S1]\n\n## Handoff\nok"])
        limits = TeamLimits(max_retries=0, max_backoff_s=1.0, max_concurrency=1)
        engine, mission = run(client, fetcher=FakeFetcher(), limits=limits)
        self.assertEqual({mission.task("T2").status, mission.task("T3").status}, {TaskStatus.FAILED})
        engine.run(retry_only={"T2"}, skip={"T3"})
        self.assertEqual(mission.task("T3").status, TaskStatus.SKIPPED)
        self.assertEqual(mission.task("T2").status, TaskStatus.DONE)
        self.assertEqual(mission.status, MissionStatus.COMPLETED_WITH_ISSUES)    # a skipped task is never a clean success


if __name__ == "__main__":
    unittest.main()
