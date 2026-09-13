"""app/mcp_server/tools.py's dispatcher, exercised against fakes so these
tests do not need a real BrowserController/QtWebEngine - see
test_mcp_server_integration.py for the same tools against the real thing.

Run with:
    python -m unittest tests.test_mcp_server_tools -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.mcp_server.tools import McpToolContext, McpToolError, dispatch  # noqa: E402
from app.mcp_server.types import PairedClient  # noqa: E402


def _client(*capabilities: str) -> PairedClient:
    return PairedClient(id="c1", display_name="Test", token_hash="h",
                       capabilities=tuple(capabilities))


class _FakeFuture:
    """Duck-types BrowserFuture.then() well enough for the dispatcher -
    resolves synchronously, since these tests never touch Qt."""

    def __init__(self, result) -> None:
        self._result = result

    def then(self, callback):
        callback(self._result)
        return self


class _Result:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def to_dict(self) -> dict:
        return self._payload


def _ok(**data) -> _Result:
    return _Result({"ok": True, "data": data, "effects": {}, "page": {"url": data.get("url", "")}})


def _fail(code: str, message: str) -> _Result:
    return _Result({"ok": False, "error": {"code": code, "message": message}})


class FakeBrowser:
    def __init__(self) -> None:
        self.tabs = [{"tab_id": 1, "url": "https://a.example", "title": "A", "active": True,
                     "loading": False}]
        self.navigate_calls: list[tuple[str, int | None]] = []
        self.open_tab_calls: list[str | None] = []
        self.requires_confirmation = False
        self.selected_text = "some selected text"

    def list_tabs(self):
        return self.tabs

    def get_selected_text(self, tab_id=None):
        return self.selected_text

    def describe_action(self, action, ref=None, text="", url="", tab_id=None):
        return {"level": "elevated" if self.requires_confirmation else "normal",
               "reasons": [], "requires_confirmation": self.requires_confirmation}

    def get_page_text(self, tab_id=None, max_chars=20000):
        return _FakeFuture(_ok(text="hello from the page", truncated=False))

    def open_tab(self, url=None):
        self.open_tab_calls.append(url)
        return _FakeFuture(_ok(url=url or ""))

    def navigate(self, url, tab_id=None):
        self.navigate_calls.append((url, tab_id))
        return _FakeFuture(_ok(url=url))


class FakeMission:
    def __init__(self, id=1, title="T", goal="G", status="active", progress="",
                result="", created_at="now", updated_at="now"):
        self.id = id
        self.title = title
        self.goal = goal
        self.status = status
        self.progress = progress
        self.result = result
        self.created_at = created_at
        self.updated_at = updated_at


class FakeFinding:
    def __init__(self, ref=1, text="found it", source_url="https://x", source_title="X",
                created_at="now"):
        self.ref = ref
        self.text = text
        self.source_url = source_url
        self.source_title = source_title
        self.created_at = created_at


class FakePage:
    def __init__(self, url="https://x", display_title="X", outcome="useful",
                first_seen="now", last_seen="now"):
        self.url = url
        self.display_title = display_title
        self.outcome = outcome
        self.first_seen = first_seen
        self.last_seen = last_seen


class FakeMissionStore:
    def __init__(self) -> None:
        self._missions = {1: FakeMission()}

    def recent(self, limit):
        return list(self._missions.values())[:limit]

    def get(self, mission_id, with_pages=False):
        return self._missions.get(mission_id)

    def findings(self, mission_id):
        return [FakeFinding()]

    def pages(self, mission_id):
        return [FakePage()]


class FakeMissions:
    def __init__(self) -> None:
        self.store = FakeMissionStore()
        self.started_with: tuple[str, str] | None = None

    def start(self, goal, title=""):
        self.started_with = (goal, title)
        mission = FakeMission(id=2, goal=goal, title=title)
        self.store._missions[2] = mission
        return mission


class FakeGraphNode:
    def __init__(self, id=1, node_type="research", role="researcher", title="Research",
                state="pending", dependencies=(), result_summary="", error=""):
        self.id = id
        self.node_type = node_type
        self.role = role
        self.title = title
        self.state = state
        self.dependencies = dependencies
        self.result_summary = result_summary
        self.error = error


class FakeGraphStore:
    def nodes_for_mission(self, mission_id):
        return [FakeGraphNode()]


def _context(browser=None, missions=None, graph_store=None) -> McpToolContext:
    return McpToolContext(
        browser=browser, missions=missions, graph_store=graph_store,
        call_sync=lambda fn: fn(),
        call_future=lambda fn: _run_future(fn),
        confirm=lambda prompt: False)


def _run_future(fn):
    box = {}
    fn().then(lambda r: box.update(value=r))
    return box.get("value")


class BrowserToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.browser = FakeBrowser()
        self.context = _context(browser=self.browser)
        self.client = _client("read_pages", "read_tabs", "navigate", "open_tabs")

    def test_current_page_returns_the_active_tab(self) -> None:
        result = dispatch(self.context, self.client, "browser.current_page", {})
        self.assertTrue(result.ok)
        self.assertEqual(result.data["url"], "https://a.example")

    def test_read_page_wraps_the_text_as_untrusted_content(self) -> None:
        result = dispatch(self.context, self.client, "browser.read_page", {})
        self.assertTrue(result.ok)
        self.assertIn("<untrusted_web_page_content>", result.data["text"])
        self.assertIn("hello from the page", result.data["text"])

    def test_list_tabs_returns_every_open_tab(self) -> None:
        result = dispatch(self.context, self.client, "browser.list_tabs", {})
        self.assertEqual(len(result.data["tabs"]), 1)

    def test_search_matches_by_title_or_url(self) -> None:
        result = dispatch(self.context, self.client, "browser.search", {"query": "a.example"})
        self.assertEqual(len(result.data["tabs"]), 1)
        result_none = dispatch(self.context, self.client, "browser.search",
                               {"query": "no-such-thing"})
        self.assertEqual(len(result_none.data["tabs"]), 0)

    def test_search_requires_a_query(self) -> None:
        with self.assertRaises(McpToolError):
            dispatch(self.context, self.client, "browser.search", {"query": ""})

    def test_get_selected_text_is_wrapped_as_untrusted_too(self) -> None:
        result = dispatch(self.context, self.client, "browser.get_selected_text", {})
        self.assertIn("some selected text", result.data["text"])
        self.assertIn("<untrusted_web_page_content>", result.data["text"])

    def test_navigate_without_confirmation_needed_just_navigates(self) -> None:
        result = dispatch(self.context, self.client, "browser.navigate",
                         {"url": "https://example.com"})
        self.assertTrue(result.ok)
        self.assertEqual(self.browser.navigate_calls, [("https://example.com", None)])

    def test_navigate_requiring_confirmation_and_denied_never_navigates(self) -> None:
        """The core no-bypass-approval guarantee: an external call that maps
        to a sensitive action goes through the SAME safety classification
        (describe_action) the interactive agent uses, and a denial actually
        stops the call - see app/mcp_server/tools.py's _navigate."""
        self.browser.requires_confirmation = True
        result = dispatch(self.context, self.client, "browser.navigate",
                         {"url": "https://evil.example"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "DENIED")
        self.assertEqual(self.browser.navigate_calls, [])

    def test_navigate_requiring_confirmation_and_approved_does_navigate(self) -> None:
        self.browser.requires_confirmation = True
        context = McpToolContext(
            browser=self.browser, missions=None, graph_store=None,
            call_sync=lambda fn: fn(), call_future=lambda fn: _run_future(fn),
            confirm=lambda prompt: True)
        result = dispatch(context, self.client, "browser.navigate", {"url": "https://ok.example"})
        self.assertTrue(result.ok)
        self.assertEqual(self.browser.navigate_calls, [("https://ok.example", None)])

    def test_open_tab_also_goes_through_the_same_navigate_classification(self) -> None:
        """browser_open_tab and browser_navigate must face the same check -
        routing only one through it would leave the other as a bypass."""
        self.browser.requires_confirmation = True
        result = dispatch(self.context, self.client, "browser.open_tab",
                         {"url": "https://evil.example"})
        self.assertFalse(result.ok)
        self.assertEqual(self.browser.open_tab_calls, [])

    def test_navigate_requires_a_url(self) -> None:
        with self.assertRaises(McpToolError):
            dispatch(self.context, self.client, "browser.navigate", {})


class PermissionEnforcementTests(unittest.TestCase):
    def test_a_client_without_the_navigate_capability_is_refused_before_touching_the_browser(
        self,
    ) -> None:
        browser = FakeBrowser()
        context = _context(browser=browser)
        client = _client("read_pages")  # no "navigate"
        with self.assertRaises(McpToolError) as ctx:
            dispatch(context, client, "browser.navigate", {"url": "https://example.com"})
        self.assertEqual(ctx.exception.code, "INSUFFICIENT_PERMISSION")
        self.assertEqual(browser.navigate_calls, [])

    def test_an_unknown_tool_is_refused(self) -> None:
        context = _context(browser=FakeBrowser())
        client = _client(*[c.value for c in __import__(
            "app.mcp_server.types", fromlist=["Capability"]).Capability])
        with self.assertRaises(McpToolError) as ctx:
            dispatch(context, client, "browser.eval_js", {})
        self.assertEqual(ctx.exception.code, "UNKNOWN_TOOL")

    def test_malformed_arguments_are_refused(self) -> None:
        context = _context(browser=FakeBrowser())
        client = _client("read_tabs")
        with self.assertRaises(McpToolError):
            dispatch(context, client, "browser.list_tabs", "not a dict")  # type: ignore[arg-type]


class MissionToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.missions = FakeMissions()
        self.graph_store = FakeGraphStore()
        self.context = _context(missions=self.missions, graph_store=self.graph_store)
        self.client = _client("read_missions", "create_mission")

    def test_mission_list_returns_safe_summaries_only(self) -> None:
        result = dispatch(self.context, self.client, "mission.list", {})
        self.assertTrue(result.ok)
        summary = result.data["missions"][0]
        self.assertEqual(set(summary), {"id", "title", "goal", "status", "progress",
                                        "result", "created_at", "updated_at"})

    def test_mission_get_requires_a_mission_id(self) -> None:
        with self.assertRaises(McpToolError):
            dispatch(self.context, self.client, "mission.get", {})

    def test_mission_get_findings(self) -> None:
        result = dispatch(self.context, self.client, "mission.get_findings", {"mission_id": 1})
        self.assertEqual(result.data["findings"][0]["text"], "found it")

    def test_mission_get_sources(self) -> None:
        result = dispatch(self.context, self.client, "mission.get_sources", {"mission_id": 1})
        self.assertEqual(result.data["sources"][0]["url"], "https://x")

    def test_mission_get_plan_never_includes_private_reasoning_fields(self) -> None:
        result = dispatch(self.context, self.client, "mission.get_plan", {"mission_id": 1})
        node = result.data["nodes"][0]
        self.assertEqual(set(node), {"id", "type", "role", "title", "state",
                                     "dependencies", "result_summary", "error"})

    def test_mission_create_starts_a_new_mission(self) -> None:
        result = dispatch(self.context, self.client, "mission.create", {"goal": "Find a laptop"})
        self.assertTrue(result.ok)
        self.assertEqual(self.missions.started_with, ("Find a laptop", ""))

    def test_mission_create_requires_a_goal(self) -> None:
        with self.assertRaises(McpToolError):
            dispatch(self.context, self.client, "mission.create", {})

    def test_mission_tools_fail_closed_when_missions_are_unavailable(self) -> None:
        context = _context(missions=None, graph_store=None)
        result = dispatch(context, self.client, "mission.list", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "MISSIONS_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
