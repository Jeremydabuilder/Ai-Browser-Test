"""Web search for the Researcher: provider request shapes, error handling,
query redaction, key handling, and how results enter a mission (as web
sources, kept apart from anything the user attached).

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_team_websearch -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx2 as httpx  # noqa: E402
import keyring  # noqa: E402
from keyring.backend import KeyringBackend  # noqa: E402

from app.team import websearch  # noqa: E402
from app.team.engine import Capabilities, TeamEngine  # noqa: E402
from app.team.limits import TeamLimits  # noqa: E402
from app.team.model import Mission, MissionStatus, Source, SourceKind  # noqa: E402
from app.team import sandbox as sandbox_mod  # noqa: E402
from tests.test_team_engine import APPROVE, FakeClient, RESEARCH_WRITE_REVIEW  # noqa: E402

TAVILY_KEY = "tvly-unit-test-key-0123456789"
BRAVE_KEY = "BSAunittestkey0123456789abcdef"


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        self.data.pop((service, username), None)


def tavily(handler) -> websearch.WebSearch:
    status = websearch.SearchStatus("tavily", "Tavily", True, "test", "environment", TAVILY_KEY)
    return websearch.WebSearch(status, transport=httpx.MockTransport(handler), sleep=lambda s: None)


def brave(handler) -> websearch.WebSearch:
    status = websearch.SearchStatus("brave", "Brave Search", True, "test", "environment", BRAVE_KEY)
    return websearch.WebSearch(status, transport=httpx.MockTransport(handler), sleep=lambda s: None)


class ProviderRequestTests(unittest.TestCase):
    def test_tavily_request_shape_and_result_parsing(self) -> None:
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"results": [
                {"title": "Widget B review", "url": "https://reviews.example/b", "content": "Lasts 5 years.  Strong."},
                {"title": "dup", "url": "https://reviews.example/b", "content": "same url"},
                {"title": "bad", "url": "javascript:alert(1)", "content": "x"},
                {"title": "ftp", "url": "ftp://x/y", "content": "x"},
                {"title": "", "url": "https://untitled.example/", "content": "No title here"},
            ]})

        results = tavily(handler).search("widget b lifespan", 5)
        request = seen[0]
        self.assertEqual((request.method, str(request.url)), ("POST", "https://api.tavily.com/search"))
        self.assertEqual(request.headers["authorization"], f"Bearer {TAVILY_KEY}")
        body = json.loads(request.content)
        self.assertEqual((body["query"], body["max_results"]), ("widget b lifespan", 5))
        self.assertNotIn(TAVILY_KEY, request.content.decode())
        self.assertEqual([r.url for r in results], ["https://reviews.example/b", "https://untitled.example/"])
        self.assertEqual(results[0].snippet, "Lasts 5 years. Strong.")
        self.assertEqual(results[1].title, "https://untitled.example/")      # untitled hits fall back to the URL

    def test_brave_request_shape_and_html_is_stripped(self) -> None:
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"web": {"results": [
                {"title": "Brave <b>result</b>", "url": "https://a.example/x", "description": "A <strong>bold</strong> claim",
                 "extra_snippets": ["more &amp; more"]}]}})

        results = brave(handler).search("hello world")
        request = seen[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.url.host, "api.search.brave.com")
        self.assertEqual(request.url.params["q"], "hello world")
        self.assertEqual(request.headers["x-subscription-token"], BRAVE_KEY)
        self.assertNotIn(BRAVE_KEY, str(request.url))                         # never in the URL
        self.assertEqual(results[0].title, "Brave result")
        self.assertEqual(results[0].snippet, "A bold claim more & more")

    def test_only_a_short_redacted_query_leaves_the_machine(self) -> None:
        bodies = []

        def handler(request):
            bodies.append(request.content.decode())
            return httpx.Response(200, json={"results": []})

        tavily(handler).search("compare widgets my password is hunter2 and key sk-abcdefghijklmnopqrstuvwxyz0123456789 " + "x" * 400)
        sent = json.loads(bodies[0])["query"]
        self.assertNotIn("sk-abcdefghij", sent)
        self.assertLessEqual(len(sent), websearch.MAX_QUERY)
        self.assertEqual(set(json.loads(bodies[0])), {"query", "max_results", "search_depth", "include_answer",
                                                      "include_raw_content"})   # nothing but the query + options


class ErrorHandlingTests(unittest.TestCase):
    def kind(self, status: int, **kwargs) -> str:
        client = tavily(lambda request: httpx.Response(status, json={"detail": "x"}, **kwargs))
        with self.assertRaises(websearch.SearchError) as caught:
            client.search("q")
        self.assertNotIn(TAVILY_KEY, caught.exception.message)
        return caught.exception.kind

    def test_status_codes_map_to_actionable_kinds(self) -> None:
        self.assertEqual(self.kind(401), websearch.SearchErrorKind.AUTH)
        self.assertEqual(self.kind(403), websearch.SearchErrorKind.AUTH)
        self.assertEqual(self.kind(432), websearch.SearchErrorKind.QUOTA)
        self.assertEqual(self.kind(500), websearch.SearchErrorKind.PROVIDER)

    def test_a_429_is_retried_once_then_reported(self) -> None:
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(429, headers={"retry-after": "1"}, json={})

        with self.assertRaises(websearch.SearchError) as caught:
            tavily(handler).search("q")
        self.assertEqual(len(calls), 2)
        self.assertEqual(caught.exception.kind, websearch.SearchErrorKind.RATE_LIMIT)

        ok = []

        def recovering(request):
            ok.append(1)
            if len(ok) == 1:
                return httpx.Response(429, headers={"retry-after": "0"}, json={})
            return httpx.Response(200, json={"results": [{"title": "t", "url": "https://x.example/", "content": "c"}]})

        self.assertEqual(len(tavily(recovering).search("q")), 1)

    def test_network_failures_and_garbage_are_reported_not_raised_raw(self) -> None:
        def boom(request):
            raise httpx.ConnectError("no route")
        with self.assertRaises(websearch.SearchError) as caught:
            tavily(boom).search("q")
        self.assertEqual(caught.exception.kind, websearch.SearchErrorKind.NETWORK)
        with self.assertRaises(websearch.SearchError):
            tavily(lambda request: httpx.Response(200, content=b"<html>")).search("q")

    def test_no_key_means_no_client(self) -> None:
        with self.assertRaises(websearch.SearchError):
            websearch.WebSearch(websearch.SearchStatus("tavily", "Tavily", False, "no key"))


class KeyHandlingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._previous = keyring.get_keyring()
        self.memory = MemoryKeyring()
        keyring.set_keyring(self.memory)
        self.addCleanup(keyring.set_keyring, self._previous)
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("PYBROWSER_SEARCH_PROVIDER", "PYBROWSER_DISABLE_KEYRING", "TAVILY_API_KEY",
                     "BRAVE_SEARCH_API_KEY"):
            os.environ.pop(name, None)

    class Settings:
        def __init__(self, **values):
            self.values = values

        def get(self, key, default=""):
            return self.values.get(key, default)

    def test_search_is_off_until_a_provider_is_chosen_and_says_how_to_turn_it_on(self) -> None:
        status = websearch.resolve_search(self.Settings())
        self.assertFalse(status.available)
        self.assertIn("Tavily or Brave", status.detail)

    def test_a_chosen_provider_without_a_key_names_the_variable_and_where_to_get_one(self) -> None:
        status = websearch.resolve_search(self.Settings(team_search_provider="brave"))
        self.assertFalse(status.available)
        self.assertIn("BRAVE_SEARCH_API_KEY", status.detail)
        self.assertIn("Brave", status.detail)

    def test_keys_come_from_the_keyring_first_then_the_environment_and_never_show_in_repr(self) -> None:
        settings = self.Settings(team_search_provider="tavily")
        os.environ["TAVILY_API_KEY"] = "from-env-key-123456"
        status = websearch.resolve_search(settings)
        self.assertEqual((status.available, status.origin), (True, "environment"))
        websearch.save_search_key("tavily", TAVILY_KEY)               # what the settings dialog does
        status = websearch.resolve_search(settings)
        self.assertEqual((status.origin, status.secret), ("keyring", TAVILY_KEY))
        self.assertNotIn(TAVILY_KEY, repr(status))
        self.assertNotIn(TAVILY_KEY, status.detail)
        self.assertIn(("PyBrowser", "tavily-api-key"), self.memory.data)  # the OS keyring, not the settings table
        websearch.clear_search_key("tavily")
        self.assertEqual(websearch.resolve_search(settings).origin, "environment")

    def test_the_environment_can_pick_the_provider(self) -> None:
        os.environ.update({"PYBROWSER_SEARCH_PROVIDER": "brave", "BRAVE_SEARCH_API_KEY": BRAVE_KEY})
        status = websearch.resolve_search(self.Settings())
        self.assertEqual((status.provider, status.available), ("brave", True))


class FakeWeb:
    """Stands in for a WebSearch client inside the engine."""

    label = "Tavily"

    def __init__(self, results=None, error=None):
        self.queries: list[str] = []
        self.results = results if results is not None else [
            websearch.WebResult("Widget B warranty - Review Site", "https://reviews.example/b-warranty",
                                "Widget B ships with a three year warranty."),
            websearch.WebResult("Widget A teardown", "https://teardown.example/a", "Widget A uses cheaper bearings.")]
        self.error = error

    def search(self, query, max_results=6):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.results


NO_SANDBOX = sandbox_mod.SandboxStatus(False, "test")


def run_mission(client, web, *, web_on=True, sources=True, goal="Compare the widgets"):
    srcs = [Source("S1", SourceKind.TAB, "Widget A", "https://a.example/widget", "Widget A costs $10."),
            Source("S2", SourceKind.PASTE, "My notes", "", "I prefer durable widgets.")] if sources else []
    mission = Mission(goal=goal, sources=srcs, web_search=web_on)
    caps = Capabilities(NO_SANDBOX, web_search=web)
    engine = TeamEngine(mission, lambda: client, TeamLimits(max_backoff_s=1.0), caps)
    engine.run()
    return mission


def script(**overrides):
    base = {"plan": [RESEARCH_WRITE_REVIEW],
            "searchplan": [json.dumps({"queries": ["widget b warranty length", "widget a bearings quality"]})],
            "researcher": ["Per the attached page A costs $10 [S1]. Per web search B has a 3 year warranty [S3]."],
            "writer": ["Draft [S1] [S3]"], "reviewer": [APPROVE], "final": ["Buy B. [S1] [S3]"]}
    base.update(overrides)
    return FakeClient(base)


class EngineIntegrationTests(unittest.TestCase):
    def test_web_results_become_distinct_sources_with_urls_and_are_labelled_in_the_prompt(self) -> None:
        client, web = script(), FakeWeb()
        mission = run_mission(client, web)
        self.assertEqual(mission.status, MissionStatus.COMPLETED)
        self.assertEqual(web.queries, ["widget b warranty length", "widget a bearings quality"])
        web_sources = [s for s in mission.sources if s.kind == SourceKind.WEB]
        self.assertEqual([s.url for s in web_sources], ["https://reviews.example/b-warranty", "https://teardown.example/a"])
        self.assertEqual([s.id for s in web_sources], ["S3", "S4"])          # numbered after the attached ones
        prompt = client.users("researcher")[0]
        self.assertIn('"origin": "web search result"', prompt)
        self.assertIn('"origin": "attached tab"', prompt)
        self.assertIn('"origin": "attached text"', prompt)
        self.assertIn("three year warranty", prompt)
        self.assertTrue(any(e.kind == "tool" and "Web search (Tavily)" in e.text for e in mission.events))

    def test_the_final_result_separates_attached_sources_from_web_sources_and_states_the_limit(self) -> None:
        mission = run_mission(script(), FakeWeb())
        final = mission.artifact(mission.final_artifact_id).content
        attached = final.index("## Sources\n_attached by you_")
        web = final.index("## Web search results")
        self.assertLess(attached, web)
        self.assertIn("[S1] Widget A - https://a.example/widget", final[attached:web])
        self.assertIn("[S3] Widget B warranty - Review Site - https://reviews.example/b-warranty", final[web:])
        self.assertIn("search snippets (title, URL and excerpt); the pages themselves were not opened", final)
        self.assertNotIn("Web search is unavailable", final)

    def test_query_planning_sees_the_mission_wording_but_never_page_contents(self) -> None:
        client = script()
        run_mission(client, FakeWeb())
        planning = client.users("searchplan")[0]
        self.assertIn("Compare the widgets", planning)
        self.assertNotIn("Widget A costs $10", planning)
        self.assertNotIn("I prefer durable widgets", planning)

    def test_a_bad_query_plan_falls_back_to_the_task_title(self) -> None:
        client, web = script(searchplan=["not json"]), FakeWeb()
        run_mission(client, web)
        self.assertEqual(len(web.queries), 1)
        self.assertIn("Read the pages", web.queries[0])

    def test_when_the_user_turned_it_off_nothing_is_sent_anywhere(self) -> None:
        client, web = script(), FakeWeb()
        mission = run_mission(client, web, web_on=False)
        self.assertEqual(web.queries, [])
        self.assertNotIn("searchplan", client.roles())
        self.assertFalse([s for s in mission.sources if s.kind == SourceKind.WEB])
        self.assertIn("turned off for this mission", mission.artifact(mission.final_artifact_id).content)

    def test_a_failing_search_degrades_gracefully_and_says_so(self) -> None:
        error = websearch.SearchError(websearch.SearchErrorKind.RATE_LIMIT, "Tavily is rate-limiting searches.")
        client, web = script(), FakeWeb(error=error)
        mission = run_mission(client, web)
        self.assertEqual(mission.status, MissionStatus.COMPLETED)         # attached sources still carry the research
        self.assertEqual(len(web.queries), 2)                             # a rate limit does not stop later queries
        final = mission.artifact(mission.final_artifact_id).content
        self.assertIn("Web search failed (Tavily is rate-limiting searches.)", final)
        self.assertTrue(any(e.kind == "warning" and "Web search failed" in e.text for e in mission.events))

    def test_a_rejected_key_stops_further_queries(self) -> None:
        error = websearch.SearchError(websearch.SearchErrorKind.AUTH, "Tavily rejected the API key.")
        web = FakeWeb(error=error)
        run_mission(script(), web)
        self.assertEqual(len(web.queries), 1)

    def test_without_any_provider_the_limitation_is_stated(self) -> None:
        mission = run_mission(script(), None)
        final = mission.artifact(mission.final_artifact_id).content
        self.assertIn("Web search is unavailable", final)

    def test_the_planner_is_told_what_search_exists(self) -> None:
        client = script()
        run_mission(client, FakeWeb())
        self.assertIn("web search: Tavily is available", client.users("plan")[0])
        client = script()
        run_mission(client, None)
        self.assertIn("web search: unavailable", client.users("plan")[0])


if __name__ == "__main__":
    unittest.main()
