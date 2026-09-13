"""The 13 tools an external MCP client may call, and the dispatcher that
adapts each one onto the EXISTING BrowserController / MissionService /
MissionGraphStore - never a second automation layer.

Deliberately conservative for this first pass (see the Phase 11 spec):
no arbitrary JavaScript, no unrestricted click/type, no form submission,
no downloads, no file access, no code execution. Everything here is
either a read, or one of the two narrowly-scoped actions (open_tab,
navigate) that already goes through BrowserController.describe_action -
the exact same safety classification the interactive agent gets - plus
mission.create, a local, reversible write with no browser side effect.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.agent.tools import UNTRUSTED_CLOSE, UNTRUSTED_OPEN
from app.mcp_server.permissions import is_permitted

MAX_READ_CHARS = 20000


@dataclass
class ToolResult:
    ok: bool
    data: dict[str, Any] | None = None
    error_code: str = ""
    error_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        if self.ok:
            return {"ok": True, **(self.data or {})}
        return {"ok": False, "error": {"code": self.error_code, "message": self.error_message}}


class McpToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


#: JSON Schema for every tool this server exposes - what tools/list returns.
#: Never grows arbitrary JS / unrestricted click-type / submit / downloads /
#: file access / code execution: that is the point of this first pass.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"name": "browser.current_page", "description": "The active tab's URL, title and id.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "browser.read_page",
     "description": "The visible text of a tab (untrusted page content).",
     "inputSchema": {"type": "object", "properties": {
         "tab_id": {"type": "integer"}, "max_chars": {"type": "integer"}}}},
    {"name": "browser.list_tabs", "description": "Every open tab, with ids, titles and URLs.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "browser.search",
     "description": "Search open tabs by a title/URL substring.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}},
                     "required": ["query"]}},
    {"name": "browser.get_selected_text", "description": "The current text selection in a tab.",
     "inputSchema": {"type": "object", "properties": {"tab_id": {"type": "integer"}}}},
    {"name": "mission.list", "description": "Recent Missions, most recently touched first.",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "mission.get", "description": "One Mission's goal, status and progress.",
     "inputSchema": {"type": "object", "properties": {"mission_id": {"type": "integer"}},
                     "required": ["mission_id"]}},
    {"name": "mission.get_findings", "description": "A Mission's recorded findings.",
     "inputSchema": {"type": "object", "properties": {"mission_id": {"type": "integer"}},
                     "required": ["mission_id"]}},
    {"name": "mission.get_sources", "description": "The pages that contributed to a Mission.",
     "inputSchema": {"type": "object", "properties": {"mission_id": {"type": "integer"}},
                     "required": ["mission_id"]}},
    {"name": "mission.get_plan",
     "description": "A Mission's execution graph: nodes, state, dependencies.",
     "inputSchema": {"type": "object", "properties": {"mission_id": {"type": "integer"}},
                     "required": ["mission_id"]}},
    {"name": "browser.open_tab", "description": "Open a new tab, optionally at a URL.",
     "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}}},
    {"name": "browser.navigate", "description": "Load a URL in a tab.",
     "inputSchema": {"type": "object", "properties": {
         "url": {"type": "string"}, "tab_id": {"type": "integer"}},
         "required": ["url"]}},
    {"name": "mission.create", "description": "Start a new Mission with a goal.",
     "inputSchema": {"type": "object", "properties": {
         "goal": {"type": "string"}, "title": {"type": "string"}},
         "required": ["goal"]}},
]

TOOL_NAMES: frozenset[str] = frozenset(t["name"] for t in TOOL_SCHEMAS)


class McpToolContext:
    """Everything a tool call needs, gathered in one place so tools.py never
    imports app.ui or app.missions.coordinator - it only ever adapts to
    BrowserController / MissionService / MissionGraphStore, per the
    "do not build a second browser automation layer" rule.

    ``call_sync``/``call_future``/``confirm`` are the GUI-thread bridge
    (see app/mcp_server/server.py's GuiBridge) - every BrowserController
    call must run on the GUI thread even though tool dispatch itself runs
    on an HTTP handler thread.
    """

    def __init__(
        self, *, browser, missions, graph_store,
        call_sync: Callable[[Callable[[], Any]], Any],
        call_future: Callable[[Callable[[], Any]], Any],
        confirm: Callable[[str], bool],
    ) -> None:
        self.browser = browser
        self.missions = missions
        self.graph_store = graph_store
        self.call_sync = call_sync
        self.call_future = call_future
        self.confirm = confirm


def dispatch(context: McpToolContext, client, tool: str, args: dict[str, Any]) -> ToolResult:
    """Enforce capability scoping, then run the tool. Fails closed: an
    unknown tool or a missing capability never reaches a handler."""
    if tool not in TOOL_NAMES:
        raise McpToolError("UNKNOWN_TOOL", f"'{tool}' is not a tool this server exposes.")
    if not isinstance(args, dict):
        raise McpToolError("INVALID_PARAMS", "Tool arguments must be an object.")
    if not is_permitted(client, tool):
        raise McpToolError(
            "INSUFFICIENT_PERMISSION",
            f"This client does not have the capability required for '{tool}'.")
    handler = _HANDLERS[tool]
    return handler(context, args)


# -- browser tools ------------------------------------------------------------

def _current_page(context: McpToolContext, args: dict[str, Any]) -> ToolResult:
    tabs = context.call_sync(context.browser.list_tabs)
    active = next((t for t in tabs if t.get("active")), None)
    if active is None:
        return ToolResult(ok=False, error_code="NO_ACTIVE_TAB",
                          error_message="There is no active tab.")
    return ToolResult(ok=True, data={
        "tab_id": active["tab_id"], "url": active["url"], "title": active["title"]})


def _read_page(context: McpToolContext, args: dict[str, Any]) -> ToolResult:
    tab_id = args.get("tab_id") if isinstance(args.get("tab_id"), int) else None
    max_chars = args.get("max_chars") if isinstance(args.get("max_chars"), int) else MAX_READ_CHARS
    max_chars = max(1, min(max_chars, MAX_READ_CHARS))
    result = context.call_future(lambda: context.browser.get_page_text(tab_id, max_chars=max_chars))
    if result is None:
        return ToolResult(ok=False, error_code="TIMEOUT", error_message="Reading the page timed out.")
    payload = result.to_dict()
    if not payload.get("ok"):
        error = payload.get("error") or {}
        return ToolResult(ok=False, error_code=error.get("code", "FAILED"),
                          error_message=error.get("message", "Could not read the page."))
    text = (payload.get("data") or {}).get("text", "")
    # Untrusted-content semantics survive the trip to an external client too -
    # this text came from a page, not from the user or PyBrowser itself.
    return ToolResult(ok=True, data={
        "text": f"{UNTRUSTED_OPEN}\n{text}\n{UNTRUSTED_CLOSE}",
        "truncated": bool((payload.get("data") or {}).get("truncated"))})


def _list_tabs(context: McpToolContext, args: dict[str, Any]) -> ToolResult:
    tabs = context.call_sync(context.browser.list_tabs)
    return ToolResult(ok=True, data={"tabs": tabs})


def _search(context: McpToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query", "")).strip().lower()
    if not query:
        raise McpToolError("INVALID_PARAMS", "'query' is required.")
    tabs = context.call_sync(context.browser.list_tabs)
    matches = [t for t in tabs
              if query in t.get("title", "").lower() or query in t.get("url", "").lower()]
    return ToolResult(ok=True, data={"tabs": matches})


def _get_selected_text(context: McpToolContext, args: dict[str, Any]) -> ToolResult:
    tab_id = args.get("tab_id") if isinstance(args.get("tab_id"), int) else None
    text = context.call_sync(lambda: context.browser.get_selected_text(tab_id)) or ""
    # Page-derived content, same untrusted-content semantics as read_page.
    return ToolResult(ok=True, data={"text": f"{UNTRUSTED_OPEN}\n{text}\n{UNTRUSTED_CLOSE}"})


def _open_tab(context: McpToolContext, args: dict[str, Any]) -> ToolResult:
    url = args.get("url")
    url = str(url) if url else None
    if url:
        assessment = context.call_sync(
            lambda: context.browser.describe_action("navigate", url=url))
        if assessment.get("requires_confirmation"):
            prompt = (f"An external AI client wants to open a new tab at:\n\n{url}\n\n"
                     "Allow this?")
            if not context.confirm(prompt):
                return ToolResult(ok=False, error_code="DENIED",
                                  error_message="The user did not approve this action.")
    result = context.call_future(lambda: context.browser.open_tab(url))
    payload = result.to_dict()
    if not payload.get("ok"):
        error = payload.get("error") or {}
        return ToolResult(ok=False, error_code=error.get("code", "FAILED"),
                          error_message=error.get("message", "Could not open the tab."))
    new_tab_id = (payload.get("effects") or {}).get("new_tab_id")
    return ToolResult(ok=True, data={"tab_id": new_tab_id})


def _navigate(context: McpToolContext, args: dict[str, Any]) -> ToolResult:
    url = args.get("url")
    if not url:
        raise McpToolError("INVALID_PARAMS", "'url' is required.")
    url = str(url)
    tab_id = args.get("tab_id") if isinstance(args.get("tab_id"), int) else None
    assessment = context.call_sync(
        lambda: context.browser.describe_action("navigate", url=url, tab_id=tab_id))
    if assessment.get("requires_confirmation"):
        prompt = f"An external AI client wants to navigate to:\n\n{url}\n\nAllow this?"
        if not context.confirm(prompt):
            return ToolResult(ok=False, error_code="DENIED",
                              error_message="The user did not approve this action.")
    result = context.call_future(lambda: context.browser.navigate(url, tab_id))
    payload = result.to_dict()
    if not payload.get("ok"):
        error = payload.get("error") or {}
        return ToolResult(ok=False, error_code=error.get("code", "FAILED"),
                          error_message=error.get("message", "Could not navigate."))
    return ToolResult(ok=True, data={"url": (payload.get("page") or {}).get("url", url)})


# -- mission tools ------------------------------------------------------------

def _mission_list(context: McpToolContext, args: dict[str, Any]) -> ToolResult:
    if context.missions is None:
        return ToolResult(ok=False, error_code="MISSIONS_UNAVAILABLE",
                          error_message="Missions are not available in this window.")
    limit = args.get("limit") if isinstance(args.get("limit"), int) else 20
    missions = context.missions.store.recent(max(1, min(limit, 100)))
    return ToolResult(ok=True, data={"missions": [_mission_summary(m) for m in missions]})


def _mission_get(context: McpToolContext, args: dict[str, Any]) -> ToolResult:
    if context.missions is None:
        return ToolResult(ok=False, error_code="MISSIONS_UNAVAILABLE",
                          error_message="Missions are not available in this window.")
    mission_id = args.get("mission_id")
    if not isinstance(mission_id, int):
        raise McpToolError("INVALID_PARAMS", "'mission_id' is required.")
    mission = context.missions.store.get(mission_id, with_pages=False)
    if mission is None:
        return ToolResult(ok=False, error_code="NOT_FOUND", error_message="No such Mission.")
    return ToolResult(ok=True, data={"mission": _mission_summary(mission)})


def _mission_get_findings(context: McpToolContext, args: dict[str, Any]) -> ToolResult:
    if context.missions is None:
        return ToolResult(ok=False, error_code="MISSIONS_UNAVAILABLE",
                          error_message="Missions are not available in this window.")
    mission_id = args.get("mission_id")
    if not isinstance(mission_id, int):
        raise McpToolError("INVALID_PARAMS", "'mission_id' is required.")
    findings = context.missions.store.findings(mission_id)
    return ToolResult(ok=True, data={"findings": [
        {"ref": f.ref, "text": f.text, "source_url": f.source_url,
         "source_title": f.source_title, "created_at": f.created_at}
        for f in findings]})


def _mission_get_sources(context: McpToolContext, args: dict[str, Any]) -> ToolResult:
    if context.missions is None:
        return ToolResult(ok=False, error_code="MISSIONS_UNAVAILABLE",
                          error_message="Missions are not available in this window.")
    mission_id = args.get("mission_id")
    if not isinstance(mission_id, int):
        raise McpToolError("INVALID_PARAMS", "'mission_id' is required.")
    pages = context.missions.store.pages(mission_id)
    return ToolResult(ok=True, data={"sources": [
        {"url": p.url, "title": p.display_title, "outcome": p.outcome,
         "first_seen": p.first_seen, "last_seen": p.last_seen}
        for p in pages]})


def _mission_get_plan(context: McpToolContext, args: dict[str, Any]) -> ToolResult:
    if context.graph_store is None:
        return ToolResult(ok=False, error_code="MISSIONS_UNAVAILABLE",
                          error_message="Mission plans are not available in this window.")
    mission_id = args.get("mission_id")
    if not isinstance(mission_id, int):
        raise McpToolError("INVALID_PARAMS", "'mission_id' is required.")
    nodes = context.graph_store.nodes_for_mission(mission_id)
    # Safe summaries only - never the model's private reasoning, only what
    # the node itself records: role, state, dependencies, a short result.
    return ToolResult(ok=True, data={"nodes": [
        {"id": n.id, "type": n.node_type, "role": n.role, "title": n.title,
         "state": n.state, "dependencies": list(n.dependencies),
         "result_summary": n.result_summary, "error": n.error}
        for n in nodes]})


def _mission_create(context: McpToolContext, args: dict[str, Any]) -> ToolResult:
    if context.missions is None:
        return ToolResult(ok=False, error_code="MISSIONS_UNAVAILABLE",
                          error_message="Missions are not available in this window.")
    goal = args.get("goal")
    if not goal or not str(goal).strip():
        raise McpToolError("INVALID_PARAMS", "'goal' is required.")
    title = str(args.get("title") or "")
    mission = context.call_sync(lambda: context.missions.start(str(goal), title))
    if mission is None:
        return ToolResult(ok=False, error_code="FAILED", error_message="Could not start the Mission.")
    return ToolResult(ok=True, data={"mission": _mission_summary(mission)})


def _mission_summary(mission) -> dict[str, Any]:
    return {
        "id": mission.id, "title": mission.title, "goal": mission.goal,
        "status": mission.status, "progress": mission.progress,
        "result": mission.result, "created_at": mission.created_at,
        "updated_at": mission.updated_at,
    }


_HANDLERS: dict[str, Callable[[McpToolContext, dict[str, Any]], ToolResult]] = {
    "browser.current_page": _current_page,
    "browser.read_page": _read_page,
    "browser.list_tabs": _list_tabs,
    "browser.search": _search,
    "browser.get_selected_text": _get_selected_text,
    "browser.open_tab": _open_tab,
    "browser.navigate": _navigate,
    "mission.list": _mission_list,
    "mission.get": _mission_get,
    "mission.get_findings": _mission_get_findings,
    "mission.get_sources": _mission_get_sources,
    "mission.get_plan": _mission_get_plan,
    "mission.create": _mission_create,
}
