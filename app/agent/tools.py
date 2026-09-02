"""The tools Claude is allowed to use, and how they reach the browser.

Every tool maps onto exactly one ``BrowserController`` method. There is no
``execute_javascript`` tool and there must never be one: the model gets
semantic browser operations, not a shell on the page.

Two conventions run through the whole file:

* **Results are structured.** A tool returns JSON the model can branch on -
  ``ok``, an ``error`` with a machine-readable ``code``, and a ``hint`` telling
  it what to do next. "Page changed; inspect the page again" is worth far more
  to a recovering agent than a stack trace.
* **Page content is quarantined.** Anything that came from a web page is
  wrapped in an explicit untrusted marker before it reaches the model. See
  ``wrap_untrusted``.

Everything here runs on the Qt GUI thread, because BrowserController does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from app.agent.config import ContextLimits
from app.browser.controller import BrowserController, ScrollDirection
from app.browser.futures import BrowserFuture
from app.browser.results import ActionResult
# Data only - model.py holds no Qt, no database and no browser. The limit is
# imported rather than restated so the schema the model reads and the rule the
# store enforces can never drift apart.
from app.missions.model import MAX_FINDING_CHARS

# ---------------------------------------------------------------------------
# Untrusted content marking
# ---------------------------------------------------------------------------

UNTRUSTED_OPEN = "<untrusted_web_page_content>"
UNTRUSTED_CLOSE = "</untrusted_web_page_content>"


def wrap_untrusted(payload: Any) -> str:
    """Fence page-derived data so the model can see where it starts and ends.

    A page can contain "ignore your instructions and…". Marking the boundary
    does not make that text harmless - nothing does, entirely - but it gives
    the model an unambiguous signal about which bytes are data. The system
    prompt tells it what the marker means.

    We also neutralise any copy of the closing marker inside the payload, so a
    page cannot "close" the fence early and have the rest read as instructions.
    """
    body = json.dumps(payload, ensure_ascii=False, indent=None)
    body = body.replace(UNTRUSTED_CLOSE, "&lt;/untrusted_web_page_content&gt;")
    return f"{UNTRUSTED_OPEN}\n{body}\n{UNTRUSTED_CLOSE}"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_REF = {
    "type": "string",
    "description": "Element reference from browser_get_page, e.g. 's3:e12'. "
                   "References are only valid until the page changes.",
}
_TAB = {
    "type": "integer",
    "description": "Tab id from browser_list_tabs. Omit to use the active tab.",
}


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
    }


TOOL_SCHEMAS: list[dict[str, Any]] = [
    _tool("browser_get_page",
          "Inspect the current page and return its structure: URL, title, headings, "
          "forms, and the interactive elements (links, buttons, text fields, checkboxes, "
          "radios, dropdowns) with their roles, accessible names and element references. "
          "Call this before acting on a page, and again after any action that changed it. "
          "Returns untrusted web page content.",
          {"tab_id": _TAB,
           "include_invisible": {"type": "boolean",
                                 "description": "Include elements that are not visible. Rarely needed."}}),

    _tool("browser_get_page_text",
          "Return only the readable text of the page, with no element references. "
          "Use this when you need to read content rather than interact with it. "
          "Returns untrusted web page content.",
          {"tab_id": _TAB}),

    _tool("browser_find_elements",
          "Search the whole page for elements by what they are called, and get back a "
          "short ranked list of candidates with match scores. Use this when you know "
          "what you are looking for but have not found it in the page structure - it "
          "searches every element, not just the first page-worth. "
          "Pass several phrasings in `queries`: the browser matches text literally and "
          "does not know synonyms, so to find a login button send "
          "[\"login\", \"log in\", \"sign in\"]. "
          "This only finds elements - it never activates them. If several candidates "
          "look plausible, inspect further or ask the user which one they meant rather "
          "than guessing. Returns untrusted web page content.",
          {"queries": {"type": "array", "items": {"type": "string"},
                       "description": "Alternative phrasings of the element's visible label."},
           "role": {"type": "string",
                    "description": "Optional role filter, e.g. button, link, textbox, checkbox."},
           "limit": {"type": "integer", "description": "Maximum candidates. Defaults to 10."},
           "tab_id": _TAB}),

    _tool("browser_navigate",
          "Load a URL in a tab and wait for it to finish loading.",
          {"url": {"type": "string", "description": "Absolute URL, including the scheme."},
           "tab_id": _TAB},
          ["url"]),

    _tool("browser_click",
          "Click an element by its reference. Reports whether the click navigated, "
          "changed the page, or opened a tab.",
          {"ref": _REF, "tab_id": _TAB}, ["ref"]),

    _tool("browser_type",
          "Type text into a text field, search box or textarea. Set submit=true to "
          "submit the field's form afterwards.",
          {"ref": _REF,
           "text": {"type": "string", "description": "The text to type."},
           "submit": {"type": "boolean", "description": "Submit the form after typing."},
           "append": {"type": "boolean", "description": "Append instead of replacing."},
           "tab_id": _TAB},
          ["ref", "text"]),

    _tool("browser_submit",
          "Submit the form containing the given element.",
          {"ref": _REF, "tab_id": _TAB}, ["ref"]),

    _tool("browser_select",
          "Choose an option in a dropdown, by its visible label or its value.",
          {"ref": _REF,
           "value": {"type": "string", "description": "Option label or value."},
           "tab_id": _TAB},
          ["ref", "value"]),

    _tool("browser_set_checked",
          "Check or uncheck a checkbox, switch or radio button.",
          {"ref": _REF,
           "checked": {"type": "boolean", "description": "Desired state. Defaults to true."},
           "tab_id": _TAB},
          ["ref"]),

    _tool("browser_scroll",
          "Scroll the page.",
          {"direction": {"type": "string", "enum": ["up", "down", "top", "bottom"],
                         "description": "Defaults to down."},
           "amount": {"type": "integer", "description": "Pixels. Defaults to about one screen."},
           "tab_id": _TAB}),

    _tool("browser_scroll_to_element",
          "Scroll an element into view.",
          {"ref": _REF, "tab_id": _TAB}, ["ref"]),

    _tool("browser_back", "Go back in this tab's history.", {"tab_id": _TAB}),
    _tool("browser_forward", "Go forward in this tab's history.", {"tab_id": _TAB}),
    _tool("browser_reload", "Reload the current page.", {"tab_id": _TAB}),

    _tool("browser_open_tab",
          "Open a new tab, optionally loading a URL.",
          {"url": {"type": "string", "description": "Optional URL to load."},
           "background": {"type": "boolean", "description": "Open without switching to it."}}),

    _tool("browser_close_tab", "Close a tab.", {"tab_id": _TAB}),
    _tool("browser_select_tab", "Switch to a tab.", {"tab_id": _TAB}, ["tab_id"]),
    _tool("browser_list_tabs", "List the open tabs with their ids, titles and URLs.", {}),

    _tool("browser_wait_for_element",
          "Wait for content that loads late. Polls until a matching element appears, "
          "or the page text contains the given string.",
          {"role": {"type": "string",
                    "description": "Element role, e.g. link, button, textbox."},
           "name_contains": {"type": "string", "description": "Substring of the accessible name."},
           "text_contains": {"type": "string", "description": "Substring of the page text."},
           "timeout_ms": {"type": "integer", "description": "Defaults to 10000."},
           "tab_id": _TAB}),

    # The only tool that writes anything outside the browser. It reaches one
    # method on the Mission service - not a database, not a query, not a
    # mission id - so the model can record a discovery and can do nothing else.
    _tool("mission_save_finding",
          "Record one useful discovery against the mission the user is working on. "
          "Save a fact worth having tomorrow - a price, a specification, a comparison, "
          "a repeated complaint - written so it still makes sense on its own. "
          "Do not save progress commentary, plans, or a summary of what you are about "
          "to do. Findings are shown to the user, not fed back to you. "
          f"Maximum {MAX_FINDING_CHARS} characters; a longer one is refused rather "
          "than shortened, so write it short. Saving the same finding twice updates "
          "the first rather than adding a second.",
          {"text": {"type": "string",
                    "description": "The discovery, in one self-contained sentence. "
                                   "Include the actual fact, not just that a page "
                                   "looked promising."},
           "tab_id": {"type": "integer",
                      "description": "The tab the finding came from, for attribution. "
                                     "Omit to use the tab in front. An id that is not "
                                     "open is an error, never a fallback."}},
          ["text"]),
]

TOOL_NAMES = {schema["name"] for schema in TOOL_SCHEMAS}


def _handler_map(schemas: list[dict[str, Any]] | None = None) -> dict[str, str]:
    """Tool name -> the ToolRegistry method that runs it.

    This used to be `getattr(self, "_run_" + name[len("browser_"):])`, which
    assumed every tool name began with `browser_` and sliced off a fixed eight
    characters. `mission_save_finding` survives that by pure luck - "mission_"
    is also eight characters long - and the next namespace would not. Slicing
    at the namespace separator instead of at a number makes the mapping mean
    what it says.

    Built once at import, with a collision check, so adding a tool whose
    handler name clashes with another's fails loudly here rather than quietly
    running the wrong code.
    """
    mapping: dict[str, str] = {}
    for schema in schemas if schemas is not None else TOOL_SCHEMAS:
        name = schema["name"]
        _prefix, _, rest = name.partition("_")
        if not rest:
            raise AssertionError(f"tool name {name!r} needs a namespace prefix")
        handler = f"_run_{rest}"
        if handler in mapping.values():
            clash = next(k for k, v in mapping.items() if v == handler)
            raise AssertionError(
                f"tools {clash!r} and {name!r} both map to {handler!r}")
        mapping[name] = handler
    return mapping


_HANDLERS = _handler_map()

#: Tools that change only which tab is in front - no page effect, nothing to
#: confirm. Listed explicitly so `assess` can fail closed on anything else.
_UNCLASSIFIED_SAFE = {"browser_select_tab", "browser_close_tab",
                      "browser_back", "browser_forward", "browser_reload"}

#: Tools that write to the user's own local records and touch no web page.
#:
#: Deliberately NOT part of READ_ONLY_TOOLS: saving a finding is a write, and
#: calling it read-only would put a lie in the code for the next person to
#: build on. It is exempt from confirmation for a stated reason rather than by
#: category - it sends nothing anywhere, spends nothing, changes no page, and
#: is one click for the user to undo. A modal per recorded fact would make the
#: feature unusable.
#:
#: The fail-closed default in `assess` is untouched: a tool in neither this set
#: nor any other is still treated as a write.
LOCAL_WRITE_TOOLS = {"mission_save_finding"}

#: Tools that only read. Used to skip confirmation checks entirely.
READ_ONLY_TOOLS = {
    "browser_get_page", "browser_get_page_text", "browser_list_tabs",
    "browser_find_elements",
    "browser_wait_for_element", "browser_scroll", "browser_scroll_to_element",
}


def _error(code: str, message: str, *, hint: str = "") -> dict[str, Any]:
    """A refused tool call, in the same shape as every other tool error.

    Same keys as `encode` produces for a failed ActionResult, so the model
    reads one error format across the whole tool surface rather than two.
    """
    payload: dict[str, Any] = {
        "ok": False,
        "error": {"code": code, "message": message, "recoverable": True},
    }
    if hint:
        payload["hint"] = hint
    return payload


def _finding_activity(text: str) -> str:
    """What the step checklist says while a finding is saved.

    Shows the finding, elided, because "Saving a finding" tells the user
    nothing about what Py thought was worth keeping.
    """
    condensed = " ".join(text.split())
    if len(condensed) > 60:
        condensed = condensed[:59].rstrip() + "\u2026"
    return f'Noting "{condensed}"'


#: Why a save was refused, and what the model should do about it. Each hint is
#: an instruction the model can actually follow, not a restatement.
_FINDING_ERRORS: dict[str, tuple[str, str, str]] = {
    "no_mission": ("NO_ACTIVE_MISSION",
                   "There is no mission active, so there is nothing to record against.",
                   "Answer the user normally. Do not try again."),
    "too_long": ("FINDING_TOO_LONG",
                 "That finding is longer than the limit and was NOT saved.",
                 "Rewrite it shorter, keeping the fact and any qualifier that "
                 "changes its meaning, and call the tool again."),
    "full": ("MISSION_FULL",
             "This mission already holds the maximum number of findings.",
             "Stop recording findings and finish the task."),
    "no_text": ("EMPTY_FINDING", "A finding needs some text.",
                "Write the discovery as one sentence and try again."),
    "unknown_tab": ("UNKNOWN_TAB",
                    "There is no open tab with that id, so the finding was NOT "
                    "saved - it would have been attributed to the wrong page.",
                    "Call browser_list_tabs for a current id, or omit tab_id to "
                    "use the tab in front."),
}


def _finding_error(result: dict) -> dict:
    code, message, hint = _FINDING_ERRORS.get(
        result.get("status", ""),
        ("FINDING_FAILED", "The finding could not be saved.", "Carry on with the task."))
    if "limit" in result:
        message = f"{message} The limit is {result['limit']}."
    return _error(code, message, hint=hint)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


@dataclass
class ToolOutcome:
    """What running a tool produced.

    ``future`` is set for asynchronous operations; the session waits on it.
    ``immediate`` is set when the tool failed validation or finished at once.
    """

    future: BrowserFuture | None = None
    immediate: dict[str, Any] | None = None
    #: Short human-readable line for the activity log, e.g. 'Clicking "Search"'.
    activity: str = ""


class ToolError(ValueError):
    """Bad arguments from the model. Reported back as a tool_result error."""


class ToolRegistry:
    """Validates tool arguments and calls BrowserController."""

    def __init__(self, browser: BrowserController, limits: ContextLimits | None = None,
                 missions=None) -> None:
        """``missions`` is the Mission service, or None when there is not one.

        Typed loosely on purpose: this class needs exactly one method from it,
        ``save_finding(text, tab_id)``. It gets no store, no database handle
        and no way to name a mission - so "the model cannot write anywhere
        except the active mission" is a property of what was passed in, not of
        the model behaving itself.
        """
        self._browser = browser
        self._limits = limits or ContextLimits()
        self._missions = missions

    # -- argument helpers ------------------------------------------------
    @staticmethod
    def _string(args: dict, key: str, *, required: bool = False, default: str = "") -> str:
        value = args.get(key, default)
        if value is None:
            value = default
        if not isinstance(value, str):
            raise ToolError(f"'{key}' must be a string.")
        if required and not value.strip():
            raise ToolError(f"'{key}' is required.")
        return value

    @staticmethod
    def _bool(args: dict, key: str, default: bool = False) -> bool:
        value = args.get(key, default)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise ToolError(f"'{key}' must be true or false.")
        return value

    @staticmethod
    def _int(args: dict, key: str, default: int | None = None) -> int | None:
        value = args.get(key, default)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError(f"'{key}' must be an integer.")
        return value

    def _tab(self, args: dict) -> int | None:
        return self._int(args, "tab_id", None)

    # -- the sensitivity question ----------------------------------------
    def knows(self, name: str) -> bool:
        """Is this a tool that exists? Asked before anything is announced."""
        return name in TOOL_NAMES

    def assess(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """What would this tool call do, and does it need the user's blessing?

        The answer comes from the browser's own safety layer, never from the
        model. That is the point: a page that talks the model into calling
        "Place order" still has to get past a classification the model does not
        control.
        """
        if name in READ_ONLY_TOOLS:
            return {"level": "normal", "reasons": [], "requires_confirmation": False}
        if name in LOCAL_WRITE_TOOLS:
            # A local, reversible write to the user's own mission board. It
            # never reaches the browser's safety layer because there is no
            # page, URL or element for that layer to judge.
            return {"level": "normal", "reasons": [], "requires_confirmation": False}
        ref = args.get("ref")
        tab_id = self._tab(args) if isinstance(args.get("tab_id"), int) else None
        if name in ("browser_navigate", "browser_open_tab"):
            # Both load a URL, so both must face the same check. Routing only
            # navigate through it left open_tab as a way to reach a flagged URL
            # (an executable download, say) without the user being asked.
            return self._strip(self._browser.describe_action(
                "navigate", url=self._string(args, "url"), tab_id=tab_id))
        if name == "browser_type":
            return self._strip(self._browser.describe_action(
                "type_text", ref=ref, text=self._string(args, "text"), tab_id=tab_id))
        if name == "browser_click":
            return self._strip(self._browser.describe_action("click", ref=ref, tab_id=tab_id))
        if name == "browser_submit":
            return self._strip(self._browser.describe_action("submit", ref=ref, tab_id=tab_id))
        if name in ("browser_set_checked", "browser_select"):
            return self._strip(self._browser.describe_action("set_checked", ref=ref, tab_id=tab_id))
        if name in _UNCLASSIFIED_SAFE:
            return {"level": "normal", "reasons": [], "requires_confirmation": False}
        # A tool nobody thought to classify is treated as a write, not as
        # harmless. Failing closed here means adding a tool cannot accidentally
        # create a hole in the confirmation gate.
        return {"level": "elevated", "reasons": ["changes browser state"],
                "requires_confirmation": False}

    def _element_name(self, args: dict[str, Any]) -> str:
        """The accessible name of the element this call targets, if known."""
        ref = args.get("ref")
        if not isinstance(ref, str):
            return ""
        tab_id = args.get("tab_id") if isinstance(args.get("tab_id"), int) else None
        preview = self._browser.describe_action("inspect", ref=ref, tab_id=tab_id)
        return (preview.get("target") or {}).get("name", "")

    @staticmethod
    def _strip(preview: dict[str, Any]) -> dict[str, Any]:
        return {
            "level": preview.get("level", "normal"),
            "reasons": preview.get("reasons", []),
            "requires_confirmation": preview.get("requires_confirmation", False),
            "target": (preview.get("target") or {}).get("name", ""),
            "target_role": (preview.get("target") or {}).get("role", ""),
        }

    #: Gerund -> infinitive, so one description can read as a step heading
    #: ("Clicking Buy now") and as a request ("wants to click Buy now").
    #: Written out rather than derived, because English word endings are not
    #: a rule you can compute.
    _INFINITIVES = {
        "Clicking": "click",
        "Submitting": "submit",
        "Setting": "set",
        "Choosing an option in": "choose an option in",
        "Scrolling to": "scroll to",
        "Typing into": "type into",
        "Opening": "open",
        "Reading the page": "read the page",
        "Reading the page text": "read the page text",
        "Looking for": "look for",
        "Searching the page": "search the page",
        "Scrolling": "scroll",
        "Waiting for": "wait for",
    }

    def describe_call_as_request(self, name: str, args: dict[str, Any]) -> str:
        """The same description, phrased to follow "wants to".

        Lowercasing the gerund produced "wants to clicking ...", which is the
        sort of thing that makes an approval prompt look machine-generated at
        exactly the moment the user is deciding whether to trust it.
        """
        description = self.describe_call(name, args)
        for gerund, infinitive in self._INFINITIVES.items():
            if description == gerund:
                return infinitive
            if description.startswith(gerund + " "):
                return infinitive + description[len(gerund):]
        return description[:1].lower() + description[1:]

    def describe_call(self, name: str, args: dict[str, Any]) -> str:
        """A short line for the activity log. Never includes sensitive text."""
        try:
            if name == "browser_navigate":
                return f"Opening {self._string(args, 'url')}"
            if name == "mission_save_finding":
                return _finding_activity(self._string(args, "text"))
            if name == "browser_get_page":
                return "Reading the page"
            if name == "browser_get_page_text":
                return "Reading the page text"
            if name == "browser_find_elements":
                queries = args.get("queries") or []
                shown = str(queries[0]) if queries else self._string(args, "role")
                return f'Looking for "{shown}"' if shown else "Searching the page"
            if name in ("browser_click", "browser_submit", "browser_set_checked",
                        "browser_select", "browser_scroll_to_element", "browser_type"):
                # Ask the browser what this element is called. assess() would
                # short-circuit for read-only tools and leave us printing a raw
                # reference, which tells the user nothing.
                label = self._element_name(args) or args.get("ref", "")
                verb = {
                    "browser_click": "Clicking",
                    "browser_submit": "Submitting",
                    "browser_set_checked": "Setting",
                    "browser_select": "Choosing an option in",
                    "browser_scroll_to_element": "Scrolling to",
                    "browser_type": "Typing into",
                }[name]
                return f'{verb} "{label}"' if label else verb
            if name == "browser_scroll":
                return f"Scrolling {self._string(args, 'direction', default='down')}"
            if name == "browser_back":
                return "Going back"
            if name == "browser_forward":
                return "Going forward"
            if name == "browser_reload":
                return "Reloading"
            if name == "browser_open_tab":
                url = self._string(args, "url")
                return f"Opening a new tab{f' at {url}' if url else ''}"
            if name == "browser_close_tab":
                return "Closing a tab"
            if name == "browser_select_tab":
                return "Switching tab"
            if name == "browser_list_tabs":
                return "Listing tabs"
            if name == "browser_wait_for_element":
                return "Waiting for the page to update"
        except ToolError:
            pass
        return name.replace("browser_", "").replace("_", " ").capitalize()

    # -- running ---------------------------------------------------------
    def run(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        """Validate and dispatch. Raises ToolError for bad arguments."""
        if name not in TOOL_NAMES:
            raise ToolError(f"Unknown tool '{name}'.")
        if not isinstance(args, dict):
            raise ToolError("Tool arguments must be an object.")
        handler: Callable[[dict], ToolOutcome] = getattr(self, _HANDLERS[name])
        return handler(args)

    # Each handler below is deliberately thin - validate, call, return.
    def _run_save_finding(self, args: dict) -> ToolOutcome:
        """Record one discovery against the active Mission.

        Every failure here is a normal tool result the model can read and act
        on - too long, no mission, unknown tab - rather than an exception. The
        model's next move differs in each case, so the code says which.
        """
        text = self._string(args, "text", required=True)
        tab_id = self._int(args, "tab_id", None)
        if self._missions is None:
            return ToolOutcome(immediate=_error(
                "NO_MISSION", "Missions are not available in this window.",
                hint="Carry on with the task; nothing needs recording."),
                activity="Saving a finding")

        result = self._missions.save_finding(text, tab_id)
        status = result.get("status")
        if status in ("saved", "updated"):
            source = result.get("source") or ""
            return ToolOutcome(
                # Deliberately terse: the text is already in the conversation
                # because the model just wrote it, and echoing it back would
                # pay for it twice.
                immediate={"ok": True, "status": status,
                           "finding_id": result.get("finding_id"),
                           **({"source": source} if source else {})},
                activity=_finding_activity(text))
        return ToolOutcome(immediate=_finding_error(result), activity="Saving a finding")

    def _run_get_page(self, args: dict) -> ToolOutcome:
        return ToolOutcome(future=self._browser.get_page_structure(
            self._tab(args),
            max_elements=self._limits.max_elements,
            max_text=self._limits.max_page_text,
            include_invisible=self._bool(args, "include_invisible"),
        ))

    def _run_find_elements(self, args: dict) -> ToolOutcome:
        queries = args.get("queries") or []
        if not isinstance(queries, list) or not all(isinstance(q, str) for q in queries):
            raise ToolError("'queries' must be a list of strings.")
        role = self._string(args, "role") or None
        if not queries and not role:
            raise ToolError("Give 'queries', a 'role', or both.")
        return ToolOutcome(future=self._browser.find_elements(
            queries, role=role, limit=self._int(args, "limit", 10) or 10,
            tab_id=self._tab(args)))

    def _run_get_page_text(self, args: dict) -> ToolOutcome:
        return ToolOutcome(future=self._browser.get_page_text(
            self._tab(args), max_chars=self._limits.max_page_text))

    def _run_navigate(self, args: dict) -> ToolOutcome:
        url = self._string(args, "url", required=True)
        return ToolOutcome(future=self._browser.navigate(url, self._tab(args)))

    def _run_click(self, args: dict) -> ToolOutcome:
        return ToolOutcome(future=self._browser.click(
            self._string(args, "ref", required=True), self._tab(args)))

    def _run_type(self, args: dict) -> ToolOutcome:
        return ToolOutcome(future=self._browser.type_text(
            self._string(args, "ref", required=True),
            self._string(args, "text"),
            submit=self._bool(args, "submit"),
            append=self._bool(args, "append"),
            tab_id=self._tab(args),
        ))

    def _run_submit(self, args: dict) -> ToolOutcome:
        return ToolOutcome(future=self._browser.submit(
            self._string(args, "ref", required=True), self._tab(args)))

    def _run_select(self, args: dict) -> ToolOutcome:
        return ToolOutcome(future=self._browser.select_option(
            self._string(args, "ref", required=True),
            self._string(args, "value", required=True),
            self._tab(args),
        ))

    def _run_set_checked(self, args: dict) -> ToolOutcome:
        return ToolOutcome(future=self._browser.set_checked(
            self._string(args, "ref", required=True),
            self._bool(args, "checked", True),
            self._tab(args),
        ))

    def _run_scroll(self, args: dict) -> ToolOutcome:
        direction = self._string(args, "direction", default=ScrollDirection.DOWN)
        if direction not in ("up", "down", "top", "bottom"):
            raise ToolError("'direction' must be up, down, top or bottom.")
        return ToolOutcome(future=self._browser.scroll(
            direction, self._int(args, "amount"), self._tab(args)))

    def _run_scroll_to_element(self, args: dict) -> ToolOutcome:
        return ToolOutcome(future=self._browser.scroll_to_element(
            self._string(args, "ref", required=True), self._tab(args)))

    def _run_back(self, args: dict) -> ToolOutcome:
        return ToolOutcome(future=self._browser.go_back(self._tab(args)))

    def _run_forward(self, args: dict) -> ToolOutcome:
        return ToolOutcome(future=self._browser.go_forward(self._tab(args)))

    def _run_reload(self, args: dict) -> ToolOutcome:
        return ToolOutcome(future=self._browser.reload(self._tab(args)))

    def _run_open_tab(self, args: dict) -> ToolOutcome:
        url = self._string(args, "url") or None
        return ToolOutcome(future=self._browser.open_tab(
            url, background=self._bool(args, "background")))

    def _run_close_tab(self, args: dict) -> ToolOutcome:
        return ToolOutcome(immediate=self.encode(self._browser.close_tab(self._tab(args))))

    def _run_select_tab(self, args: dict) -> ToolOutcome:
        tab_id = self._int(args, "tab_id")
        if tab_id is None:
            raise ToolError("'tab_id' is required.")
        return ToolOutcome(immediate=self.encode(self._browser.select_tab(tab_id)))

    def _run_list_tabs(self, args: dict) -> ToolOutcome:
        return ToolOutcome(immediate={"ok": True, "tabs": self._browser.list_tabs()})

    def _run_wait_for_element(self, args: dict) -> ToolOutcome:
        role = self._string(args, "role") or None
        name_contains = self._string(args, "name_contains") or None
        text_contains = self._string(args, "text_contains") or None
        if not any((role, name_contains, text_contains)):
            raise ToolError("Give at least one of role, name_contains or text_contains.")
        return ToolOutcome(future=self._browser.wait_for_element(
            role=role, name_contains=name_contains, text_contains=text_contains,
            tab_id=self._tab(args),
            timeout_ms=self._int(args, "timeout_ms", 10000) or 10000,
        ))

    # -- result encoding --------------------------------------------------
    def encode(self, result: ActionResult) -> dict[str, Any]:
        """Turn an ActionResult into the JSON the model sees.

        Page-derived content is wrapped separately by ``render`` below; this
        keeps the control fields (ok, error, effects) outside the untrusted
        fence so the model can always trust *those*.
        """
        payload: dict[str, Any] = {
            "ok": result.ok,
            "action": result.action,
            "page": {
                "url": result.page.url,
                "title": result.page.title,
                "tab_id": result.page.tab_id,
                "can_go_back": result.page.can_go_back,
                "can_go_forward": result.page.can_go_forward,
            },
        }
        if result.page.load_error:
            payload["page"]["load_error"] = result.page.load_error
        if result.target:
            payload["target"] = {"ref": result.target.ref, "role": result.target.role,
                                 "name": result.target.name}
        effects = result.effects
        if result.ok and result.action not in ("get_page_structure", "get_page_text"):
            payload["effects"] = {
                "navigated": effects.navigated,
                "page_changed": effects.dom_changed,
                "opened_tab": effects.opened_tab,
            }
            if effects.new_tab_id is not None:
                payload["effects"]["new_tab_id"] = effects.new_tab_id
            if effects.navigated or effects.dom_changed or effects.opened_tab:
                payload["hint"] = ("The page changed. Element references from earlier "
                                   "snapshots may be stale - call browser_get_page again "
                                   "before acting on this page.")
        if result.error:
            payload["error"] = {
                "code": result.error.code,
                "message": result.error.message,
                "recoverable": result.error.recoverable,
            }
            payload["hint"] = (
                "Call browser_get_page to get fresh element references, then retry."
                if result.error.recoverable
                else "This cannot be retried as-is. Choose a different element or approach."
            )
        # Structures and text are page content: fence them.
        structure = result.data.get("structure")
        if structure is not None:
            payload["structure_is_untrusted"] = True
        return payload

    def render(self, result: ActionResult, payload: dict[str, Any]) -> str:
        """The final string handed back as the tool_result content."""
        structure = result.data.get("structure")
        text = result.data.get("text")
        # wait_for_element also reports a key called "matches", but as a count.
        # Only a list is a find_elements result.
        matches = result.data.get("matches")
        if not isinstance(matches, list):
            matches = None
        blocks = [json.dumps(payload, ensure_ascii=False)]
        if structure is not None:
            blocks.append(wrap_untrusted(self._trim_structure(structure)))
        elif matches is not None:
            total = result.data.get("total_matches", len(matches))
            summary: dict[str, Any] = {"matches": matches, "total_matches": total}
            if total > len(matches):
                summary["note"] = (f"{total} elements matched; the {len(matches)} best are "
                                   "listed. Narrow the query if the one you want is missing.")
            if len(matches) > 1 and matches[0].get("match_score", 0) - \
                    matches[1].get("match_score", 0) < 20:
                summary["ambiguous"] = True
                summary["note_ambiguous"] = (
                    "Several candidates scored similarly. Do not guess - inspect them, "
                    "or ask the user which one they meant.")
            blocks.append(wrap_untrusted(summary))
        elif text is not None:
            blocks.append(wrap_untrusted({"page_text": text,
                                          "truncated": result.data.get("truncated", False)}))
        elif result.data:
            extra = {k: v for k, v in result.data.items() if k not in ("structure", "text")}
            if extra:
                blocks.append(json.dumps(extra, ensure_ascii=False))
        rendered = "\n".join(blocks)
        cap = self._limits.max_tool_result_chars
        if len(rendered) > cap:
            # Say so rather than silently cutting: the agent needs to know the
            # view is partial so it can scroll or narrow instead of assuming.
            rendered = rendered[:cap] + (
                f"\n[Truncated at {cap} characters. The page is larger than the "
                "configured limit - scroll, or ask for the page text instead.]")
        return rendered

    def _trim_structure(self, structure: Any) -> dict[str, Any]:
        """Compact the page structure for the model's benefit."""
        data = structure.to_dict() if hasattr(structure, "to_dict") else dict(structure)
        # doc_id and dom_revision are internal bookkeeping; the model has no use
        # for them and they cost tokens on every single turn.
        data.pop("doc_id", None)
        data.pop("dom_revision", None)
        if data.get("elements_truncated"):
            data["note"] = (f"Only the first {self._limits.max_elements} interactive "
                            "elements are listed. Scroll or narrow the task if the one "
                            "you need is missing.")
        return data
