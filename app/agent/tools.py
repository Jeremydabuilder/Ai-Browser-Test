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

from app.agent.config import Autonomy, ContextLimits
from app.security import firewall, injection
from app.security.log import EventType as SecurityEventType
from app.security.log import security_log
from app.security.provenance import Provenance
from app.browser import visual as visual_module
from app.browser.controller import BrowserController, ScrollDirection
from app.browser.futures import BrowserFuture, resolved
from app.browser.results import ActionError, ActionResult
from app.browser.visual import VisualBudget
# Data only - model.py holds no Qt, no database and no browser. The limit is
# imported rather than restated so the schema the model reads and the rule the
# store enforces can never drift apart.
from app.missions.model import (
    MAX_ALTERNATIVES,
    MAX_ANSWER_CHARS,
    MAX_ASSUMPTION_CHARS,
    MAX_ASSUMPTIONS,
    MAX_CHALLENGE_SUMMARY,
    MAX_DECISION_CHARS,
    MAX_EVIDENCE,
    MAX_FINDING_CHARS,
    MAX_CONSTRAINT_CHARS,
    MAX_CONSTRAINTS,
    MAX_FOLLOW_UP_CHARS,
    MAX_FOLLOW_UPS,
    MAX_GHOST_RUN_EFFECT_CHARS,
    MAX_GHOST_RUN_EFFECTS,
    MAX_GHOST_RUN_OPTION_CHARS,
    MAX_POINT_CHARS,
    MAX_POINTS,
    MAX_PROGRESS_CHARS,
    MAX_QUESTION_CHARS,
    MAX_RATIONALE_CHARS,
    MAX_RESULT_CHARS,
    Confidence,
    EffectKind,
    PointKind,
    Verdict,
)

# ---------------------------------------------------------------------------
# Untrusted content marking
# ---------------------------------------------------------------------------

UNTRUSTED_OPEN = "<untrusted_web_page_content>"
UNTRUSTED_CLOSE = "</untrusted_web_page_content>"


def wrap_untrusted(payload: Any, *, provenance: str = Provenance.WEBPAGE, source: str = "") -> str:
    """Fence page-derived data so the model can see where it starts and ends -
    the ONE chokepoint every non-authoritative source (a webpage, a PDF, a
    local file, a knowledge-retrieval result) already passes through before
    reaching the model, which is why Phase 15's firewall lives here rather
    than being reimplemented per feature (app.mcp.adapter.wrap_untrusted is
    the other half of that same design, for MCP results specifically - it
    calls the same app.security.firewall functions this does).

    A page can contain "ignore your instructions and…". Marking the boundary
    does not make that text harmless - nothing does, entirely - but it gives
    the model an unambiguous signal about which bytes are data. The system
    prompt tells it what the marker means. ``provenance`` records WHICH kind
    of untrusted source this is (see app.security.provenance) so the model
    can be told, not just that this is untrusted, but what it is untrusted
    *as* - a WEBPAGE is not the same thing as an MCP_RESULT or a
    KNOWLEDGE_RETRIEVAL, even though none of them carry authority.

    Likely secrets (API keys, passwords, tokens, payment data) are redacted
    from the payload before it is fenced - see app.security.firewall.redact -
    unless the user has turned that protection off in Settings. Detecting
    injection-style phrasing here only logs a security event (see
    app.security.log); it can never be the thing that decides safety, since
    the actual authority boundary is that this fence exists at all, not what
    is inside it.

    We also neutralise any copy of the closing marker inside the payload, so a
    page cannot "close" the fence early and have the rest read as instructions.
    """
    body = json.dumps(payload, ensure_ascii=False, indent=None)
    if firewall.is_enabled():
        redacted_body, findings = firewall.redact(body, only_high_risk=True)
        if findings:
            security_log.record(
                SecurityEventType.SECRET_REDACTED, firewall.summarize(findings), source=source)
            body = redacted_body
    if injection.is_enabled():
        reasons = injection.detect(body)
        if reasons:
            security_log.record(
                SecurityEventType.INJECTION_DETECTED, "; ".join(reasons[:3]), source=source)
    body = body.replace(UNTRUSTED_CLOSE, "&lt;/untrusted_web_page_content&gt;")
    open_tag = f'<untrusted_content provenance="{provenance}">' if provenance != Provenance.WEBPAGE \
        else UNTRUSTED_OPEN
    close_tag = "</untrusted_content>" if provenance != Provenance.WEBPAGE else UNTRUSTED_CLOSE
    return f"{open_tag}\n{body}\n{close_tag}"


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

    _tool("browser_get_pdf_text",
          "Read the PDF a tab is showing: its extracted text, labelled by page number, "
          "plus page count and whether it looks like a scanned (image-only) PDF with no "
          "extractable text. Use this instead of browser_get_page_text for any tab whose "
          "URL ends in .pdf - the PDF viewer has no page text to read directly. "
          "Returns untrusted document content.",
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

    _tool("mission_note_source",
          "Record that you reviewed a source while researching, whether or not it "
          "was useful. Call this for a page you read and decided did not help, so "
          "the user can see it was actually checked rather than just skipped over. "
          "A page a finding was already saved from does not need this - it is "
          "already counted as reviewed and useful.",
          {"useful": {"type": "boolean",
                     "description": "False for a page that did not help - wrong "
                                    "topic, no real information, already covered "
                                    "by a better source."},
           "tab_id": {"type": "integer",
                      "description": "The tab this source is in. Omit to use the "
                                     "tab in front. An id that is not open is an "
                                     "error, never a fallback."}},
          ["useful"]),

    _tool("mission_save_question",
          "Raise an open question against the mission - something research has "
          "surfaced that is not yet settled: sources disagree, a fact could not be "
          "confirmed, or answering the user's goal properly depends on something "
          "still unknown. This is not a to-do list and not a place to think out "
          "loud - only for a genuine uncertainty worth the user seeing. Resolve it "
          "later with mission_resolve_question once a source answers it. "
          f"Maximum {MAX_QUESTION_CHARS} characters. Raising the same question "
          "again is a harmless no-op, not a duplicate.",
          {"text": {"type": "string",
                    "description": "The open question, as a self-contained sentence "
                                   "the user can read on its own."}},
          ["text"]),

    _tool("mission_resolve_question",
          "Answer a question previously raised with mission_save_question, once a "
          "source settles it. Matched by wording, not by an id - close paraphrasing "
          "is fine, but answering a question that does not match any open one is an "
          f"error, not a guess. Maximum {MAX_ANSWER_CHARS} characters for the answer.",
          {"question": {"type": "string",
                        "description": "The open question being answered, close to "
                                       "how it was originally raised."},
           "answer": {"type": "string",
                     "description": "What settled it, in one self-contained sentence."}},
          ["question", "answer"]),

    _tool("mission_save_decision",
          "Record what the user has decided on the active mission, and why. "
          "Use it when the research has reached a conclusion - a choice made, an "
          "option ruled out, a recommendation the user accepted. Write the "
          "rationale for the user to read months later, in their terms, not as "
          "an account of how you worked it out. "
          "Saving a decision RECORDS it; it does not carry it out and it is not "
          "permission to do anything. Any real action still needs the user to ask "
          "and still faces the browser's own confirmation. "
          "Saving again replaces the current decision. "
          f"Limits: decision {MAX_DECISION_CHARS} characters, rationale "
          f"{MAX_RATIONALE_CHARS}; longer is refused rather than shortened.",
          {"decision": {"type": "string",
                        "description": "What was decided, in a few words."},
           "rationale": {"type": "string",
                         "description": "Why, in terms the user will recognise. "
                                        "The reasons, not the reasoning."},
           "evidence": {"type": "array",
                        "items": {"type": "string"},
                        "description": "References of findings on this mission that "
                                       "support the decision, as they appear in your "
                                       f"notes: \"F1\", \"F4\". At most {MAX_EVIDENCE}. "
                                       "A reference that does not exist on this "
                                       "mission is an error, not an omission."},
           "assumptions": {"type": "array",
                           "items": {"type": "string"},
                           "description": "What this decision takes for granted, "
                                          "stated plainly for the user - the things "
                                          "that would change the answer if they turned "
                                          f"out to be false. At most {MAX_ASSUMPTIONS}, "
                                          f"{MAX_ASSUMPTION_CHARS} characters each."},
           "alternatives": {"type": "array",
                            "description": "What was considered and not chosen. "
                                           f"At most {MAX_ALTERNATIVES}.",
                            "items": {"type": "object",
                                      "properties": {
                                          "name": {"type": "string"},
                                          "reason": {"type": "string",
                                                     "description": "Why it was not chosen."}},
                                      "required": ["name", "reason"],
                                      "additionalProperties": False}}},
          ["decision", "rationale"]),

    _tool("mission_save_challenge",
          "Record the result of challenging a claim the user selected. Use this "
          "ONLY after the user has asked for a claim to be challenged - it applies "
          "to whatever they picked, and there is no way to point it at something "
          "else. "
          "Challenging means trying to prove the claim wrong: look for evidence "
          "pointing the other way, the primary source behind a statistic, context "
          "the claim leaves out, whether it has gone out of date, and who benefits "
          "from it being believed. Report what you found even when it is nothing - "
          "a claim that survives a real attempt to break it is a useful result. "
          "This records your findings beside the original claim; it never edits or "
          "replaces it.",
          {"verdict": {"type": "string",
                       "enum": list(Verdict.ALL),
                       "description": "upheld - nothing found against it. weakened - "
                                      "it still stands but less firmly. contradicted - "
                                      "evidence points the other way. unresolved - "
                                      "you could not settle it."},
           "summary": {"type": "string",
                       "description": "What you found, in a few sentences for the "
                                      f"user to read. At most {MAX_CHALLENGE_SUMMARY} "
                                      "characters."},
           "points": {"type": "array",
                      "description": f"The specific problems found. At most {MAX_POINTS}.",
                      "items": {"type": "object",
                                "properties": {
                                    "kind": {"type": "string",
                                             "enum": list(PointKind.ALL),
                                             "description": "conflict - evidence the "
                                                            "other way. context - "
                                                            "something important left "
                                                            "out. outdated - true once, "
                                                            "not now. bias - who "
                                                            "benefits. unresolved - a "
                                                            "question you could not "
                                                            "answer."},
                                    "text": {"type": "string",
                                             "description": "The problem, in one "
                                                            f"sentence. At most "
                                                            f"{MAX_POINT_CHARS} characters."},
                                    "tab_id": {"type": "integer",
                                               "description": "The tab this came from, "
                                                              "for attribution. Omit for "
                                                              "the tab in front. An id "
                                                              "that is not open is an "
                                                              "error."}},
                                "required": ["kind", "text"],
                                "additionalProperties": False}}},
          ["verdict", "summary"]),

    _tool("mission_save_ghost_run",
          "Predict what would happen if one option were chosen - BEFORE it is "
          "chosen. This never performs the option; it only writes down what you "
          "expect, so options can be compared before anything is done. Use it "
          "when the user is weighing choices and wants to see likely "
          "consequences first. Several predictions can exist side by side for "
          "the same mission.",
          {"option": {"type": "string",
                      "description": "The option being predicted, in a few words. "
                                     f"At most {MAX_GHOST_RUN_OPTION_CHARS} characters."},
           "confidence": {"type": "string",
                          "enum": list(Confidence.ALL),
                          "description": "How sure you are about this prediction."},
           "effects": {"type": "array",
                       "description": "The predicted consequences. At most "
                                      f"{MAX_GHOST_RUN_EFFECTS}.",
                       "items": {"type": "object",
                                 "properties": {
                                     "kind": {"type": "string",
                                              "enum": list(EffectKind.ALL),
                                              "description": "benefit - a plus for this "
                                                             "option. risk - a caution "
                                                             "against it. neutral - "
                                                             "worth noting either way."},
                                     "text": {"type": "string",
                                              "description": "The predicted effect, in "
                                                             "one sentence. At most "
                                                             f"{MAX_GHOST_RUN_EFFECT_CHARS} "
                                                             "characters."}},
                                 "required": ["kind", "text"],
                                 "additionalProperties": False}}},
          ["option", "confidence"]),

    _tool("mission_set_progress",
          "Update the mission's current-stage label, shown to the user while "
          "you work - e.g. \"Comparing 3 options\", \"Waiting for approval\", "
          "\"Done\". Call this as the task moves between stages, not on every "
          "tool call - a label is a headline, not a log. Deliberately not a "
          f"percentage: there is no denominator for an open-ended web task. "
          f"At most {MAX_PROGRESS_CHARS} characters; longer is truncated.",
          {"label": {"type": "string", "description": "The current stage, in a few words."}},
          ["label"]),

    _tool("mission_save_result",
          "Record the mission's outcome once real work is done - the answer to "
          "the goal, written for the user to read: a comparison, a ranked list, "
          "a summary, whatever the task called for. Distinct from a decision: a "
          "pure research or comparison task has a result without ever choosing "
          "anything. Saving again replaces the previous result. "
          f"At most {MAX_RESULT_CHARS} characters; longer is refused rather than "
          "shortened, so write it structured and to the point rather than "
          "padded.",
          {"text": {"type": "string",
                    "description": "The outcome, in the user's terms. Use plain "
                                   "structure (a short table, a list) where that "
                                   "reads better than a paragraph."},
           "follow_ups": {"type": "array",
                          "items": {"type": "string"},
                          "description": "Plain suggestions for what to do next - "
                                         "\"track prices for another week\" - never "
                                         "anything that acts on its own. At most "
                                         f"{MAX_FOLLOW_UPS}, {MAX_FOLLOW_UP_CHARS} "
                                         "characters each."}},
          ["text"]),

    _tool("mission_save_constraints",
          "Record the hard requirements the user's goal itself named - a price "
          "limit, a location, a must-have feature - so they stay visible "
          "alongside the goal instead of buried in it. Call this once, early, "
          "only when the goal actually names specific requirements - do not "
          "invent constraints it did not state. Replaces the previous list "
          "wholesale; call again with the full corrected list to change one. "
          f"At most {MAX_CONSTRAINTS}, {MAX_CONSTRAINT_CHARS} characters each.",
          {"constraints": {"type": "array",
                           "items": {"type": "string"},
                           "description": "Each requirement as a short, self-"
                                          "contained phrase - \"under $120\", "
                                          "\"hard-court durability\" - not a "
                                          "sentence explaining it."}},
          ["constraints"]),

    # Phase 13 - local, on-device only. Lets Mission research check what
    # is already known (past Missions/findings/highlights/PDFs/files)
    # before deciding whether fresh web research is even needed.
    _tool("knowledge_search",
          "Search the user's own local knowledge index - previously visited pages, "
          "past Missions and their findings, saved highlights, PDFs and files the "
          "user has added - for anything relevant to a query. Nothing here comes "
          "from the web right now; use this BEFORE browsing when a query might "
          "already be answered by something the user has read or found before. "
          "Returns nothing if semantic history is disabled or nothing matches - "
          "that is not an error, it just means proceed with fresh research.",
          {"query": {"type": "string", "description": "What to look for, in plain words."},
           "limit": {"type": "integer",
                     "description": "Maximum results to return. Defaults to 5."}},
          ["query"]),

    # Phase 19 - the research/knowledge graph. Read-only: these tools never
    # write a node or edge, only look at what already exists. Always
    # available regardless of the Semantic History toggle (see
    # app/knowledge_graph/service.py's module docstring).
    _tool("knowledge_graph_search",
          "Search the research graph for Missions, findings, sources (pages/PDFs/files), "
          "highlights, topics, and claims by title/name. Use this to find a starting node "
          "before asking about its sources, related items, or provenance.",
          {"query": {"type": "string", "description": "What to look for, in plain words."},
           "node_type": {"type": "string",
                        "description": "Optional: restrict to one kind - mission, finding, "
                                       "webpage, pdf, file, highlight, topic, or claim."}},
          ["query"]),
    _tool("knowledge_graph_sources",
          "Given a graph node id (from knowledge_graph_search), return what supports or "
          "contradicts it: a claim's supporting sources and any contradicting claims (with "
          "whether that is a genuine contradiction, superseded information, or a differing "
          "opinion), or a source's related sources and the findings/Missions that used it.",
          {"node_id": {"type": "string", "description": "A graph node id, e.g. 'claim:abc123' "
                                                        "or 'webpage:abc123'."}},
          ["node_id"]),
    _tool("knowledge_graph_related",
          "Given a graph node id, return its immediate neighborhood in the research graph - "
          "connected Missions, findings, sources, topics, and claims, with the relationship "
          "each one has to it. Capped to a small neighborhood, never the whole graph.",
          {"node_id": {"type": "string", "description": "A graph node id."}},
          ["node_id"]),
    _tool("knowledge_graph_provenance",
          "Given a graph node id (typically a claim or finding), trace where it originally "
          "came from - the chain back to its original evidence.",
          {"node_id": {"type": "string", "description": "A graph node id."}},
          ["node_id"]),

    # Phase 14 - visual computer-use FALLBACK. Only offered when a
    # vision-capable provider is configured (see ToolRegistry.schemas) -
    # never a default path. Use the structured tools above (browser_get_page,
    # browser_click, browser_type by ref) first; reach for these only when
    # structured element lookup has failed, the page is canvas/custom-
    # rendered, or you have already decided structured tools cannot reach
    # the control you need. Every coordinate is relative to the current
    # page's own viewport only - there is no way to reach anything outside
    # it (no Settings, no OS dialogs, no other application).
    _tool("browser_visual_observe",
          "See the current page as a screenshot, when structured tools cannot "
          "reliably describe what is on it. Returns the image plus viewport "
          "dimensions, the current URL and title, and a timestamp. Use this "
          "FIRST in visual mode, before choosing coordinates to act on - never "
          "guess coordinates from a structured snapshot instead. Screenshots "
          "are not kept after this turn; call this again if you need a fresh "
          "look after something changes.",
          {"tab_id": _TAB}),

    _tool("browser_visual_click",
          "Click whatever is at this point in the CURRENT SCREENSHOT from "
          "browser_visual_observe's viewport. Only use this after "
          "browser_visual_observe, and only when you are reasonably confident "
          "what is at that point - if you are not sure, ask the user rather "
          "than guessing. If this looks like it would submit a form, send a "
          "message, place an order, pay for something, delete something, "
          "upload a file, change an account setting, or otherwise do something "
          "consequential, the SAME approval prompt a structured click already "
          "shows the user appears automatically before anything happens - you "
          "do not need to, and cannot, pre-approve this yourself. Just call the "
          "tool; if it needs approval, wait for the result.",
          {"x": {"type": "integer", "description": "X coordinate in the viewport, "
                                                   "from the last observation."},
           "y": {"type": "integer", "description": "Y coordinate in the viewport, "
                                                   "from the last observation."},
           "tab_id": _TAB},
          ["x", "y"]),

    _tool("browser_visual_focus",
          "Focus (without clicking) whatever is at this viewport point - use "
          "before browser_visual_type when you need to place the cursor in a "
          "field a structured reference could not describe.",
          {"x": {"type": "integer", "description": "X coordinate in the viewport."},
           "y": {"type": "integer", "description": "Y coordinate in the viewport."},
           "tab_id": _TAB},
          ["x", "y"]),

    _tool("browser_visual_type",
          "Type into whatever element currently has focus - call "
          "browser_visual_focus or browser_visual_click on a text field first. "
          "A field that looks like a password or payment field triggers the "
          "same automatic approval prompt browser_visual_click does; there is "
          "nothing you need to set to pre-approve it.",
          {"text": {"type": "string", "description": "Text to type."},
           "tab_id": _TAB},
          ["text"]),

    _tool("browser_visual_scroll",
          "Scroll the page while in visual mode. Same effect as browser_scroll "
          "- call browser_visual_observe again afterwards to see the result.",
          {"direction": {"type": "string", "enum": ["up", "down", "top", "bottom"],
                         "description": "Defaults to down."},
           "amount": {"type": "integer", "description": "Pixels. Defaults to about one screen."},
           "tab_id": _TAB}),
]

#: Visual-fallback tools (Phase 14). Exposed only when the active provider
#: is vision-capable (see ToolRegistry.schemas) - an agent talking to a
#: text-only model is never offered a tool whose whole point is reading a
#: screenshot it cannot process.
VISUAL_TOOL_NAMES = frozenset({
    "browser_visual_observe", "browser_visual_click", "browser_visual_focus",
    "browser_visual_type", "browser_visual_scroll",
})

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
                      "browser_back", "browser_forward", "browser_reload",
                      # Moving focus without clicking or typing does nothing a
                      # user would need to approve - same reasoning as the
                      # structured API, which has no confirmable "focus" tool
                      # at all.
                      "browser_visual_focus"}

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
LOCAL_WRITE_TOOLS = {"mission_save_finding", "mission_save_decision",
                     "mission_save_challenge", "mission_save_ghost_run",
                     "mission_set_progress", "mission_save_result",
                     "mission_save_question", "mission_resolve_question",
                     "mission_note_source", "mission_save_constraints",
                     "knowledge_search"}

#: Tools whose sensitivity cannot be known until the coordinate is
#: resolved on the page (Phase 14). assess() cannot classify these ahead
#: of time - only run() can, once it has resolved what is actually at
#: that point - so they are exempted from assess()'s classification here
#: and instead fail closed inside their own handler (see
#: _visual_write_action): a sensitive target is refused with
#: CONFIRMATION_REQUIRED unless the model passes confirmed=true, which it
#: may only do after the user has actually agreed.
VISUAL_WRITE_TOOLS = {"browser_visual_click", "browser_visual_type"}

#: Tools that only read. Used to skip confirmation checks entirely.
READ_ONLY_TOOLS = {
    "browser_get_page", "browser_get_page_text", "browser_get_pdf_text", "browser_list_tabs",
    "browser_find_elements", "browser_visual_observe", "browser_visual_scroll",
    "browser_wait_for_element", "browser_scroll", "browser_scroll_to_element",
}

#: Tools that go looking for a page rather than read one already open - used
#: only to pick Py's mascot state (searching vs. reading), not for safety.
#: Deliberately just navigation: opening a tab is closer to "starting
#: something" than "looking for it", so it stays classed as working.
SEARCH_TOOLS = {"browser_navigate"}


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


def _visual_fingerprint(element: dict[str, Any] | None) -> str | None:
    """A stable-enough identity for a visually-resolved element, so a
    later re-resolution of the same coordinate can be compared against it
    (see ToolRegistry.assess_async and AgentSession.resolve_confirmation).
    Mirrors page_script.js's own fingerprint() - tag, role, and accessible
    name - deliberately excluding position: the coordinate is already
    fixed by the tool call, so this only needs to say "is it still the
    same control", not "is it in the same place"."""
    if not element:
        return None
    return "|".join((
        str(element.get("tag", "")),
        str(element.get("role", "")),
        str(element.get("name", ""))[:80],
    ))


def _finding_activity(text: str) -> str:
    """What the step checklist says while a finding is saved.

    Shows the finding, elided, because "Saving a finding" tells the user
    nothing about what Py thought was worth keeping.
    """
    condensed = " ".join(text.split())
    if len(condensed) > 60:
        condensed = condensed[:59].rstrip() + "\u2026"
    return f'Noting "{condensed}"'


def _alternatives_of(raw) -> list[tuple[str, str]]:
    """Validate the alternatives argument into (name, reason) pairs."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ToolError("'alternatives' must be a list.")
    pairs: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ToolError("Each alternative must be an object with 'name' and 'reason'.")
        name, reason = item.get("name"), item.get("reason")
        if not isinstance(name, str) or not isinstance(reason, str):
            raise ToolError("An alternative's 'name' and 'reason' must be strings.")
        if name.strip():
            pairs.append((name, reason))
    return pairs


def _decision_activity(decision: str) -> str:
    condensed = " ".join(decision.split())
    if len(condensed) > 48:
        condensed = condensed[:47].rstrip() + "\u2026"
    return f'Recording the decision: "{condensed}"'


#: Why a decision was refused, and what to do about it.
_DECISION_ERRORS: dict[str, tuple[str, str, str]] = {
    "no_mission": ("NO_ACTIVE_MISSION",
                   "There is no mission active, so there is nothing to decide on.",
                   "Answer the user directly. Do not try again."),
    "too_long": ("DECISION_TOO_LONG",
                 "The decision or its rationale is over the limit and was NOT saved.",
                 "Shorten it, keeping the reasons that actually decided it, and "
                 "call the tool again."),
    "no_text": ("EMPTY_DECISION", "A decision needs both a decision and a rationale.",
                "Write what was chosen and why, then try again."),
    "unknown_evidence": ("UNKNOWN_EVIDENCE",
                         "One of those references does not name a finding on this "
                         "mission, so nothing was saved - the decision would have "
                         "cited evidence that is not there.",
                         "Use the references shown beside this mission's notes, like "
                         "\"F1\", or omit evidence."),
}


def _decision_error(result: dict) -> dict:
    code, message, hint = _DECISION_ERRORS.get(
        result.get("status", ""),
        ("DECISION_FAILED", "The decision could not be saved.", "Carry on."))
    unknown = result.get("unknown")
    if unknown:
        message = f"{message} Not found: {', '.join(unknown)}."
    limits = result.get("limits")
    if limits:
        message = (f"{message} Limits: decision {limits['decision']} characters, "
                   f"rationale {limits['rationale']}.")
    return _error(code, message, hint=hint)


#: Why a challenge was refused, and what to do about it.
_CHALLENGE_ERRORS: dict[str, tuple[str, str, str]] = {
    "no_mission": ("NO_ACTIVE_MISSION",
                   "There is no mission active, so there is nothing to challenge.",
                   "Tell the user what you found instead."),
    "nothing_pending": ("NO_CHALLENGE_REQUESTED",
                        "The user has not asked for anything to be challenged, so "
                        "there is nothing this would apply to.",
                        "Do not call this tool unless the user asked for a claim to "
                        "be challenged. Answer them directly instead."),
    "bad_verdict": ("BAD_VERDICT",
                    "That is not one of the verdicts.",
                    "Use upheld, weakened, contradicted or unresolved."),
    "bad_kind": ("BAD_POINT_KIND",
                 "One of those point kinds is not recognised, so nothing was saved.",
                 "Use conflict, context, outdated, bias or unresolved."),
    "unknown_target": ("UNKNOWN_TARGET",
                       "The claim being challenged could not be identified.",
                       "Ask the user which claim they meant."),
    "too_long": ("CHALLENGE_TOO_LONG",
                 "The summary or one of the points is over the limit and nothing "
                 "was saved.",
                 "Shorten it, keeping what actually undermines the claim, and try "
                 "again."),
    "no_text": ("EMPTY_CHALLENGE", "A challenge needs a summary.",
                "Say what you found, then try again."),
}


def _challenge_error(result: dict) -> dict:
    code, message, hint = _CHALLENGE_ERRORS.get(
        result.get("status", ""),
        ("CHALLENGE_FAILED", "The challenge could not be saved.", "Carry on."))
    if "limit" in result:
        message = f"{message} The limit is {result['limit']} characters."
    return _error(code, message, hint=hint)


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


def _question_activity(text: str) -> str:
    condensed = " ".join(text.split())
    if len(condensed) > 60:
        condensed = condensed[:59].rstrip() + "…"
    return f'Flagging an open question: "{condensed}"'


def _answer_activity(text: str) -> str:
    condensed = " ".join(text.split())
    if len(condensed) > 60:
        condensed = condensed[:59].rstrip() + "…"
    return f'Resolving a question: "{condensed}"'


#: Why raising a question was refused, and what to do about it.
_QUESTION_ERRORS: dict[str, tuple[str, str, str]] = {
    "no_mission": ("NO_ACTIVE_MISSION",
                   "There is no mission active, so there is nothing to flag.",
                   "Answer the user directly. Do not try again."),
    "too_long": ("QUESTION_TOO_LONG",
                 "The question is over the limit and was NOT saved.",
                 "Shorten it to the actual uncertainty and call the tool again."),
    "full": ("TOO_MANY_OPEN_QUESTIONS",
             "This mission already has as many open questions as it can hold.",
             "Resolve one first, or decide the remaining uncertainty is not "
             "worth tracking and carry on."),
    "no_text": ("EMPTY_QUESTION", "A question needs some text.",
                "Write the uncertainty as one sentence and try again."),
}


def _question_error(result: dict) -> dict:
    code, message, hint = _QUESTION_ERRORS.get(
        result.get("status", ""),
        ("QUESTION_FAILED", "The question could not be saved.", "Carry on with the task."))
    if "limit" in result:
        message = f"{message} The limit is {result['limit']}."
    return _error(code, message, hint=hint)


#: Why resolving a question was refused, and what to do about it.
_RESOLVE_ERRORS: dict[str, tuple[str, str, str]] = {
    "no_mission": ("NO_ACTIVE_MISSION",
                   "There is no mission active, so there is nothing to resolve.",
                   "Answer the user directly. Do not try again."),
    "not_found": ("QUESTION_NOT_FOUND",
                  "No open question matches that wording, so nothing was resolved.",
                  "Check the mission's open questions and use their exact wording, "
                  "or raise this as a new question instead."),
    "too_long": ("ANSWER_TOO_LONG",
                 "The answer is over the limit and was NOT saved.",
                 "Shorten it to what actually settled it and call the tool again."),
    "no_text": ("EMPTY_ANSWER", "An answer needs some text.",
                "Write what settled it as one sentence and try again."),
}


def _resolve_error(result: dict) -> dict:
    code, message, hint = _RESOLVE_ERRORS.get(
        result.get("status", ""),
        ("RESOLVE_FAILED", "The question could not be resolved.", "Carry on with the task."))
    if "limit" in result:
        message = f"{message} The limit is {result['limit']}."
    return _error(code, message, hint=hint)


#: Why a ghost run was refused, and what to do about it.
_GHOST_RUN_ERRORS: dict[str, tuple[str, str, str]] = {
    "no_mission": ("NO_ACTIVE_MISSION",
                   "There is no mission active, so there is nothing to predict for.",
                   "Answer the user directly. Do not try again."),
    "too_long": ("GHOST_RUN_TOO_LONG",
                 "The option or one of the effects is over the limit and nothing "
                 "was saved.",
                 "Shorten it, keeping the part that actually matters, and call "
                 "the tool again."),
    "no_text": ("EMPTY_GHOST_RUN", "A prediction needs an option to predict for.",
                "Name the option in a few words and try again."),
    "bad_confidence": ("BAD_CONFIDENCE",
                       "That is not one of the confidence levels.",
                       "Use low, medium or high."),
    "bad_kind": ("BAD_EFFECT_KIND",
                 "One of those effect kinds is not recognised, so nothing was "
                 "saved.",
                 "Use benefit, risk or neutral."),
}


def _ghost_run_error(result: dict) -> dict:
    code, message, hint = _GHOST_RUN_ERRORS.get(
        result.get("status", ""),
        ("GHOST_RUN_FAILED", "The prediction could not be saved.", "Carry on."))
    return _error(code, message, hint=hint)


def _ghost_run_activity(option: str) -> str:
    condensed = " ".join(option.split())
    if len(condensed) > 48:
        condensed = condensed[:47].rstrip() + "…"
    return f'Predicting: "{condensed}"'


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
    #: Set instead of ``future`` for MCP tool calls. Resolves to a plain
    #: ``{"ok": bool, "text": str}`` dict (see app.mcp.adapter.render_tool_result)
    #: rather than a browser ActionResult - MCP calls have no page/effects to
    #: encode, so they get their own completion path in AgentSession._execute
    #: instead of being forced through encode()/render().
    mcp_future: BrowserFuture | None = None
    #: Set instead of ``future``/``immediate`` for browser_visual_observe
    #: only (Phase 14 hardening): screenshot capture now needs a page round
    #: trip of its own (scanning for sensitive fields to redact - see
    #: app.browser.visual.observe), so it can no longer resolve
    #: synchronously the way it first did. Resolves to a plain
    #: ``{"ok": bool, ...}`` dict, optionally carrying a reserved
    #: ``"__image__": {"mime_type":..., "data": <base64>}`` key - the same
    #: shape ``mcp_future`` already uses for a non-ActionResult result,
    #: not a fourth new pattern.
    visual_observe_future: BrowserFuture | None = None
    #: Short human-readable line for the activity log, e.g. 'Clicking "Search"'.
    activity: str = ""
    #: Phase 14 - set only by an ``immediate`` visual result carrying an
    #: image. ``{"mime_type":..., "data": <base64>}``, the same shape
    #: AgentSession.send()'s own ``image`` parameter already uses. Carried
    #: separately from ``immediate`` because an ActionResult/its JSON
    #: encoding must stay a plain, loggable dict - the actual image bytes
    #: ride as a real image content block in the tool_result (see
    #: AgentSession._record_result), never inlined as base64 text into
    #: that JSON.
    image: dict[str, str] | None = None


class ToolError(ValueError):
    """Bad arguments from the model. Reported back as a tool_result error."""


class ToolRegistry:
    """Validates tool arguments and calls BrowserController."""

    def __init__(self, browser: BrowserController, limits: ContextLimits | None = None,
                 missions=None, *, autonomy: str = Autonomy.STANDARD, mcp=None,
                 allowed_tools: frozenset[str] | None = None, knowledge=None,
                 vision_capable: bool = False, graph=None) -> None:
        """``missions`` is the Mission service, or None when there is not one.

        Typed loosely on purpose: this class needs exactly one method from it,
        ``save_finding(text, tab_id)``. It gets no store, no database handle
        and no way to name a mission - so "the model cannot write anywhere
        except the active mission" is a property of what was passed in, not of
        the model behaving itself.

        ``autonomy`` is the user's chosen policy tier (see app.agent.config.
        Autonomy) - applied on top of the browser's own sensitivity judgement
        in assess(), never inside it. An unrecognised value is treated as
        STANDARD rather than raising, the same "a bad preference must not
        crash the agent" rule every other stored setting follows.

        ``mcp`` is an ``McpConnectionManager`` (app.mcp.connection_manager),
        or None when MCP is unavailable/unconfigured. Like ``missions``, this
        registry asks it for exactly what it needs (``schemas()``,
        ``knows()``, ``describe_call()``, ``run_tool()``) and holds nothing
        else - app.mcp itself never imports anything from app.agent.

        ``allowed_tools`` is a Skill's tool allowlist (app.agent.skills), or
        None for the ordinary unrestricted conversation every prior phase
        already built. Entries are exactly the names schemas() and run()
        already use - a builtin tool's plain name, or an MCP tool's full
        namespaced name ("mcp.<server_id>.<tool_name>"), so scoping to one
        specific tool on one specific server needs no new naming scheme.
        This is enforced in exactly two places: schemas() (the model is
        never even offered a disallowed tool) and knows() (a hallucinated or
        provider-quirk call to one is refused with the same clean
        UNKNOWN_TOOL result AgentSession already gives a truly nonexistent
        tool - not a new error shape, not a special case in session.py).
        """
        self._browser = browser
        self._limits = limits or ContextLimits()
        self._missions = missions
        self._mcp = mcp
        self._allowed_tools = allowed_tools
        #: Phase 13 KnowledgeIndex, or None where semantic history is
        #: unavailable/disabled - knowledge_search then simply reports no
        #: results (see _run_search) rather than failing.
        self._knowledge = knowledge
        #: Phase 19 KnowledgeGraphService, or None where the graph is
        #: unavailable (e.g. a bare test) - the graph_* tools then simply
        #: report nothing found, the same "not an error" shape
        #: knowledge_search already uses for a disabled/absent index.
        self._graph = graph
        #: Phase 14 - whether the configured provider can process images
        #: (see app.agent.config.provider_supports_images). Gates whether
        #: the visual-fallback tools are even offered: a text-only
        #: provider is never handed a tool whose entire point is reading a
        #: screenshot it cannot see. Never overridden per-call - the model
        #: cannot ask its way into visual tools a text-only provider can't
        #: use.
        self._vision_capable = vision_capable
        #: Phase 14 per-task budget (see app.browser.visual.VisualBudget) -
        #: bounds visual actions/screenshots/scrolls so a stuck agent
        #: cannot turn "occasional fallback" into a runaway click loop.
        #: Reset by whoever starts a new task (AgentSession.send).
        self.visual_budget = VisualBudget()
        self._autonomy = autonomy if autonomy in (
            Autonomy.READ_ONLY, Autonomy.ASK_ALWAYS, Autonomy.STANDARD) else Autonomy.STANDARD

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
        """Is this a tool that exists, and (when a Skill is running) one it
        actually allows? Asked before anything is announced - a disallowed
        tool is refused exactly like a nonexistent one, the same
        UNKNOWN_TOOL result and the same "never even gets a step announced"
        property AgentSession already gives a hallucinated tool name. There
        is deliberately no separate "not permitted by this Skill" error
        shape: from the model's perspective a tool outside its Skill's
        allowlist simply does not exist right now.
        """
        if self._allowed_tools is not None and name not in self._allowed_tools:
            return False
        if name in VISUAL_TOOL_NAMES and not self._vision_capable:
            # Same rule schemas() applies - a visual tool is never even
            # offered to a text-only provider, so it must not be "known"
            # either (a model cannot be talked into calling a tool it was
            # never told about, but run() also refuses it directly - see
            # run() below - as defense in depth).
            return False
        if name in TOOL_NAMES:
            return True
        return bool(self._mcp is not None and name.startswith("mcp.") and self._mcp.knows(name))

    def active_mission_id(self) -> int | None:
        """The active Mission's id, or None - MCP permission decisions
        scoped to "this Mission" are checked against this. Reads ``.active``
        off whatever was passed as ``missions`` rather than importing
        MissionService: this class already only ever asks that object for
        the handful of things it needs (see __init__), and this is one
        more of them, not a new dependency."""
        mission = getattr(self._missions, "active", None) if self._missions is not None else None
        return getattr(mission, "id", None) if mission is not None else None

    def active_mission_title(self) -> str:
        """The active Mission's title, or "" - a display-only snapshot for
        the MCP audit log, so a logged call still names the Mission it
        happened under even after that Mission is later renamed."""
        mission = getattr(self._missions, "active", None) if self._missions is not None else None
        return getattr(mission, "title", "") or "" if mission is not None else ""

    def record_mcp_denied(self, name: str) -> None:
        """Log a live decline (the user answered an approval prompt "no")
        to the MCP audit trail - the one denial path assess() itself never
        sees, since it only ever evaluates a *remembered* Deny."""
        if self._mcp is None or not name.startswith("mcp."):
            return
        self._mcp.record_declined_call(name, mission_id=self.active_mission_id(),
                                       mission_title=self.active_mission_title())

    def mcp_editable_field(self, name: str, args: dict[str, Any]) -> tuple[str, str]:
        """Which argument of an mcp.* call, if any, is worth letting the
        user hand-edit before approving - see
        McpConnectionManager.editable_field for the actual rule."""
        if self._mcp is None or not name.startswith("mcp."):
            return "", ""
        return self._mcp.editable_field(name, args)

    def remember_mcp_permission(self, name: str, permission: str, scope: str) -> None:
        """Persist an Allow/Deny decision for an mcp.* tool, scoped to
        "this Mission" or "always" - called from AgentSession.
        resolve_confirmation after the user answers an approval prompt with
        a "remember" scope selected. A no-op for anything else, including
        Scope.ONCE, which McpPermissionStore never persists in the first
        place."""
        if self._mcp is None or not name.startswith("mcp."):
            return
        self._mcp.remember_permission_for(name, permission, scope,
                                          mission_id=self.active_mission_id())

    def mcp_current_fingerprint(self, name: str) -> str:
        """Passthrough for the Phase 16 Automation Recorder/runner - see
        McpConnectionManager.current_fingerprint."""
        if self._mcp is None or not name.startswith("mcp."):
            return ""
        return self._mcp.current_fingerprint(name)

    def element_for_ref(self, ref: str, tab_id: int | None = None) -> dict[str, Any] | None:
        """The full element descriptor a ``ref`` currently points at, or None.

        Used only by the Phase 16 Automation Recorder to build a
        SemanticTarget at record time (app/automation/recorder.py) - the
        same lookup describe_call()/_element_name() already do to print a
        human-readable step description, exposed here as data rather than a
        formatted string."""
        preview = self._browser.describe_action("inspect", ref=ref, tab_id=tab_id)
        return preview.get("target")

    def assess(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """What would this tool call do, and does it need the user's blessing?

        The level comes from the browser's own safety layer, never from the
        model - a page that talks the model into calling "Place order" still
        has to get past a classification the model does not control. Whether
        that level actually asks is decided afterwards, by _apply_autonomy:
        the user's own chosen policy, not the model's and not the page's.

        Synchronous, and cannot classify a VISUAL_WRITE_TOOLS call for real
        (see needs_async_assessment/assess_async below) - AgentSession never
        calls this for one of those names. A direct caller that does gets
        the same non-blocking "elevated, not yet resolved" placeholder
        _raw_assess always returned before assess_async existed; it is
        never itself treated as clearance to act (see ToolOutcome.run()'s
        own defense-in-depth, which does not exist for this - see
        _run_visual_click/_run_visual_type, which never trust anything
        this method says).
        """
        return self._apply_autonomy(self._raw_assess(name, args))

    def needs_async_assessment(self, name: str) -> bool:
        """True for a tool whose sensitivity cannot be judged synchronously
        - AgentSession._next_tool must call assess_async() instead of
        assess() for these, exactly the same way it already treats an MCP
        tool name differently from a browser one, never a third code path
        bolted on beside the real one."""
        return name in VISUAL_WRITE_TOOLS

    def assess_async(self, name: str, args: dict[str, Any]) -> BrowserFuture:
        """The real classification for a visual write tool (browser_visual_
        click/browser_visual_type) - resolves the coordinate/focused field
        on the page and runs it through the exact same safety.classify_click
        /classify_type a structured ref-based action already uses (via
        app.browser.visual.classify_visual_target), then applies the same
        autonomy policy assess() does. Returns a BrowserFuture rather than a
        plain dict because, unlike a structured ref (already cached from a
        prior get_page_structure() snapshot - see BrowserController.
        _known_element), a coordinate has never been resolved before, so
        resolving it needs one page round trip.

        The resolved dict also carries "visual_target_fingerprint" - a
        stable string describing what was actually found (see
        _visual_fingerprint) - so a caller (AgentSession.resolve_confirmation)
        can re-resolve the same coordinate right before acting and refuse if
        the page has changed what is there since the user approved this,
        rather than trusting that nothing moved in between.
        """
        tab_id = self._tab(args)
        if name == "browser_visual_click":
            x = self._int(args, "x")
            y = self._int(args, "y")
            if x is None or y is None:
                raise ToolError("'x' and 'y' are required.")
            action, text = "click", ""
            inspect_future = self._browser.visual_inspect(x, y, tab_id)
        else:
            text = self._string(args, "text", required=True)
            action = "type"
            inspect_future = self._browser.visual_inspect_active(tab_id)

        result_future = BrowserFuture(f"assess_{name}")

        def on_inspected(inspect_result: Any) -> None:
            if inspect_result is None or not inspect_result.ok:
                # Nothing resolvable yet (no element at that point, nothing
                # focused) - never a reason to require confirmation; the
                # real tool call will report this same failure honestly
                # when it runs, and there is nothing to protect the user
                # from acting on if there is no target at all.
                result_future.set_result(self._apply_autonomy({
                    "level": "normal", "reasons": [], "requires_confirmation": False,
                    "visual_target_fingerprint": None,
                }))
                return
            element = inspect_result.data.get("element")
            assessment = visual_module.classify_visual_target(element, action=action, text=text)
            result = assessment.to_dict()
            result["visual_target_fingerprint"] = _visual_fingerprint(element)
            # A human-readable label for the ConfirmationRequest built from
            # this, so a visual approval reads "click 'Buy now'" the same
            # way a structured one does - never a raw, meaningless
            # coordinate pair (see AgentSession._continue_after_assessment).
            result["visual_target_name"] = (element or {}).get("name", "")
            result_future.set_result(self._apply_autonomy(result))

        inspect_future.then(on_inspected)
        return result_future

    def _raw_assess(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name in READ_ONLY_TOOLS:
            return {"level": "normal", "reasons": [], "requires_confirmation": False}
        if name in VISUAL_WRITE_TOOLS:
            # Only reached by a caller that skips AgentSession and calls
            # assess() directly - AgentSession itself always uses
            # assess_async() for these (see needs_async_assessment). Never
            # clearance to act: _run_visual_click/_run_visual_type do not
            # consult this at all, only the real ConfirmationRequest flow
            # gates them.
            return {"level": "elevated",
                    "reasons": ["visual target not yet resolved - use assess_async"],
                    "requires_confirmation": False}
        if name.startswith("mcp."):
            # McpConnectionManager.assess_call is where the actual decision
            # is made: PyBrowser's own classification (never the server's
            # description) plus any remembered per-tool permission. This
            # registry only supplies what it alone knows - which Mission, if
            # any, is active, since app.mcp must never import app.agent (and
            # so cannot ask MissionService that itself).
            if self._mcp is None:
                return {"level": "elevated", "reasons": ["unrecognised MCP tool"],
                        "requires_confirmation": False}
            return self._mcp.assess_call(name, args, mission_id=self.active_mission_id(),
                                         mission_title=self.active_mission_title())
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

    def _apply_autonomy(self, result: dict[str, Any]) -> dict[str, Any]:
        """Turn a sensitivity level into an actual decision, per the user's
        chosen autonomy tier - see app.agent.config.Autonomy.

        A NORMAL-level action is never touched by any tier: reading and
        looking around is not what autonomy settings are about.
        """
        if result.get("level", "normal") == "normal":
            return result
        if self._autonomy == Autonomy.READ_ONLY:
            result = dict(result)
            result["requires_confirmation"] = False
            result["refused"] = True
            return result
        if self._autonomy == Autonomy.ASK_ALWAYS:
            result = dict(result)
            result["requires_confirmation"] = True
            return result
        return result  # STANDARD: the browser's own judgement stands, unchanged.

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
            if name == "mission_save_decision":
                return _decision_activity(self._string(args, "decision"))
            if name == "mission_save_challenge":
                return f"Challenging the claim: {self._string(args, 'verdict')}"
            if name == "mission_save_ghost_run":
                return _ghost_run_activity(self._string(args, "option"))
            if name == "mission_set_progress":
                return self._string(args, "label") or "Updating progress"
            if name == "mission_save_result":
                return "Recording the result"
            if name == "mission_save_constraints":
                return "Recording the mission's constraints"
            if name == "mission_note_source":
                return ("Noting a useful source" if args.get("useful")
                       else "Ruling out a source")
            if name == "mission_save_question":
                return _question_activity(self._string(args, "text"))
            if name == "mission_resolve_question":
                return _answer_activity(self._string(args, "answer"))
            if name == "browser_get_page":
                return "Reading the page"
            if name == "browser_get_page_text":
                return "Reading the page text"
            if name == "browser_get_pdf_text":
                return "Reading the PDF"
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
            if name == "browser_visual_observe":
                return "Looking at the page"
            if name == "browser_visual_click":
                return f"Clicking at ({args.get('x')}, {args.get('y')})"
            if name == "browser_visual_focus":
                return f"Focusing at ({args.get('x')}, {args.get('y')})"
            if name == "browser_visual_type":
                return "Typing into the focused field"
            if name == "browser_visual_scroll":
                return f"Scrolling {self._string(args, 'direction', default='down')}"
        except ToolError:
            pass
        if name.startswith("mcp.") and self._mcp is not None:
            return self._mcp.describe_call(name, args)
        return name.replace("browser_", "").replace("_", " ").capitalize()

    def schemas(self) -> list[dict[str, Any]]:
        """The full tool list to send the model: native tools plus whatever
        read-only MCP tools are currently connected and enabled. Called
        instead of the module-level TOOL_SCHEMAS wherever a session builds
        its request, so MCP tools appear and disappear as servers connect
        and disconnect without any other code needing to know MCP exists.

        When a Skill has an allowlist, this is where it actually takes
        effect for the model: a disallowed tool is not merely refused if
        called, it is never offered in the first place.
        """
        all_schemas = list(TOOL_SCHEMAS)
        if not self._vision_capable:
            # Never offered to a text-only provider - there is nothing it
            # could do with a tool whose entire point is reading a
            # screenshot it cannot process (see the phase's provider-
            # capability requirement).
            all_schemas = [s for s in all_schemas if s["name"] not in VISUAL_TOOL_NAMES]
        if self._mcp is not None:
            all_schemas += self._mcp.schemas()
        if self._allowed_tools is None:
            return all_schemas
        return [schema for schema in all_schemas if schema["name"] in self._allowed_tools]

    # -- running ---------------------------------------------------------
    def run(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        """Validate and dispatch. Raises ToolError for bad arguments.

        No permission check happens here, for MCP or anything else: by the
        time AgentSession calls run(), assess() has already decided whether
        this call was allowed to reach this point at all (refused outright,
        or approved via the confirmation prompt) - exactly the same
        division of responsibility a browser_click already has between
        assess() and _run_click. A write-classified MCP tool executes here
        precisely because getting here already means it was permitted.

        The one exception is a Skill's tool allowlist: checked again here,
        not only in knows(), so a direct run() call (this method is public,
        and tests and any future caller may reach it without going through
        AgentSession's knows()-then-assess()-then-run() sequence at all)
        can never execute a tool outside the active Skill's scope either.
        """
        if self._allowed_tools is not None and name not in self._allowed_tools:
            raise ToolError(f"'{name}' is not permitted by the active Skill.")
        if name in VISUAL_TOOL_NAMES and not self._vision_capable:
            raise ToolError(
                f"'{name}' requires a vision-capable provider. Visual interaction is not "
                "available with the currently configured provider.")
        if name.startswith("mcp."):
            if self._mcp is None or not self._mcp.knows(name):
                raise ToolError(f"Unknown tool '{name}'.")
            if not isinstance(args, dict):
                raise ToolError("Tool arguments must be an object.")
            return ToolOutcome(
                mcp_future=self._mcp.run_tool(
                    name, args, mission_id=self.active_mission_id(),
                    mission_title=self.active_mission_title()),
                activity=self._mcp.describe_call(name, args))
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
                           # The mission-local reference, not a row id: this is
                           # how the finding is cited when a decision is
                           # recorded, and the only handle the model ever sees.
                           "ref": result.get("ref"),
                           **({"source": source} if source else {})},
                activity=_finding_activity(text))
        return ToolOutcome(immediate=_finding_error(result), activity="Saving a finding")

    def _run_note_source(self, args: dict) -> ToolOutcome:
        """Record that a page was reviewed, whether or not it was useful -
        the counterpart to _run_save_finding for a source that did not pan
        out, so reviewed/useful/skipped counts reflect real work done."""
        useful = self._bool(args, "useful")
        tab_id = self._int(args, "tab_id", None)
        if self._missions is None:
            return ToolOutcome(immediate=_error(
                "NO_MISSION", "Missions are not available in this window.",
                hint="Carry on with the task; nothing needs recording."),
                activity="Noting a source")

        result = self._missions.note_source(useful, tab_id)
        status = result.get("status")
        activity = "Noting a useful source" if useful else "Ruling out a source"
        if status == "ok":
            return ToolOutcome(immediate={"ok": True, "outcome": result.get("outcome")},
                              activity=activity)
        if status == "unknown_tab":
            return ToolOutcome(immediate=_error(
                "UNKNOWN_TAB", f"Tab {tab_id} is not open.",
                hint="Use browser_list_tabs to see what is actually open."),
                activity=activity)
        if status == "no_active_tab":
            return ToolOutcome(immediate=_error(
                "NO_ACTIVE_TAB", "There is no active tab to attribute this to.",
                hint="Pass a tab_id, or open a page first."), activity=activity)
        if status == "not_associable":
            return ToolOutcome(immediate=_error(
                "NOT_ASSOCIABLE", "This page cannot be recorded as a source.",
                hint="Internal pages and blank tabs are not sources."), activity=activity)
        if status == "no_mission":
            return ToolOutcome(immediate=_error(
                "NO_MISSION", "There is no active mission to record this against.",
                hint="Carry on with the task; nothing needs recording."), activity=activity)
        return ToolOutcome(immediate=_error(
            "FAILED", "Could not record this source."), activity=activity)

    def _run_save_question(self, args: dict) -> ToolOutcome:
        """Raise an open question against the active Mission."""
        text = self._string(args, "text", required=True)
        if self._missions is None:
            return ToolOutcome(immediate=_error(
                "NO_MISSION", "Missions are not available in this window.",
                hint="Carry on with the task; nothing needs recording."),
                activity="Flagging an open question")

        result = self._missions.save_question(text)
        status = result.get("status")
        if status in ("saved", "updated"):
            return ToolOutcome(
                immediate={"ok": True, "status": status,
                          "question_id": result.get("question_id")},
                activity=_question_activity(text))
        return ToolOutcome(immediate=_question_error(result),
                           activity="Flagging an open question")

    def _run_resolve_question(self, args: dict) -> ToolOutcome:
        """Answer a previously raised open question."""
        question = self._string(args, "question", required=True)
        answer = self._string(args, "answer", required=True)
        if self._missions is None:
            return ToolOutcome(immediate=_error(
                "NO_MISSION", "Missions are not available in this window.",
                hint="Carry on with the task; nothing needs recording."),
                activity="Resolving a question")

        result = self._missions.resolve_question(question, answer)
        status = result.get("status")
        if status == "updated":
            return ToolOutcome(immediate={"ok": True, "status": status},
                              activity=_answer_activity(answer))
        return ToolOutcome(immediate=_resolve_error(result), activity="Resolving a question")

    def _run_save_decision(self, args: dict) -> ToolOutcome:
        """Record a decision against the active Mission.

        Writes rows and nothing else. This method holds no controller, so
        there is no path from here to an action - which is what makes "a saved
        decision is never permission" a property of the code rather than a
        promise in a prompt.
        """
        decision = self._string(args, "decision", required=True)
        rationale = self._string(args, "rationale", required=True)
        evidence = args.get("evidence") or []
        if not isinstance(evidence, list) or not all(
                isinstance(item, str) for item in evidence):
            raise ToolError("'evidence' must be a list of finding references "
                            "like \"F1\".")
        assumptions = args.get("assumptions") or []
        if not isinstance(assumptions, list) or not all(
                isinstance(item, str) for item in assumptions):
            raise ToolError("'assumptions' must be a list of strings.")
        alternatives = _alternatives_of(args.get("alternatives"))

        if self._missions is None:
            return ToolOutcome(immediate=_error(
                "NO_MISSION", "Missions are not available in this window.",
                hint="Answer the user directly instead."),
                activity="Recording a decision")

        result = self._missions.save_decision(decision, rationale, evidence,
                                              alternatives, assumptions)
        if result.get("status") == "saved":
            return ToolOutcome(
                immediate={"ok": True, "decision_id": result.get("decision_id"),
                           "evidence": result.get("evidence", 0),
                           "note": "Recorded. This is a record, not permission - "
                                   "any action still needs the user."},
                activity=_decision_activity(decision))
        return ToolOutcome(immediate=_decision_error(result),
                           activity="Recording a decision")

    def _run_save_challenge(self, args: dict) -> ToolOutcome:
        """Record the result of an adversarial check on the claim the user picked.

        No target parameter: the target is whatever the user selected, held by
        the Mission service. That is what makes the model structurally unable
        to challenge something nobody asked about.
        """
        verdict = self._string(args, "verdict", required=True)
        summary = self._string(args, "summary", required=True)
        raw_points = args.get("points") or []
        if not isinstance(raw_points, list):
            raise ToolError("'points' must be a list.")

        if self._missions is None:
            return ToolOutcome(immediate=_error(
                "NO_MISSION", "Missions are not available in this window.",
                hint="Tell the user what you found instead."),
                activity="Recording a challenge")

        points: list[tuple[str, str, int | None]] = []
        for item in raw_points:
            if not isinstance(item, dict):
                raise ToolError("Each point must be an object with 'kind' and 'text'.")
            kind, text = item.get("kind"), item.get("text")
            if not isinstance(kind, str) or not isinstance(text, str):
                raise ToolError("A point's 'kind' and 'text' must be strings.")
            tab_id = item.get("tab_id")
            if tab_id is not None and (isinstance(tab_id, bool)
                                       or not isinstance(tab_id, int)):
                raise ToolError("A point's 'tab_id' must be an integer.")
            # Attribution comes from the real tab, never from the model.
            page = self._missions.resolve_page(tab_id)
            if page == "unknown":
                return ToolOutcome(
                    immediate=_finding_error({"status": "unknown_tab"}),
                    activity="Recording a challenge")
            points.append((kind, text, page.id if page is not None else None))

        result = self._missions.save_challenge(verdict, summary, points)
        if result.get("status") == "saved":
            return ToolOutcome(
                immediate={"ok": True, "challenge_id": result.get("challenge_id"),
                           "points": result.get("points", 0),
                           "note": "Recorded beside the original claim, which is "
                                   "unchanged. This is a record, not permission."},
                activity=f"Recording the challenge: {verdict}")
        return ToolOutcome(immediate=_challenge_error(result),
                           activity="Recording a challenge")

    def _run_save_ghost_run(self, args: dict) -> ToolOutcome:
        """Record a prediction of what an option would lead to.

        This method holds no controller and never touches the browser - the
        same structural guarantee as _run_save_decision, for the same reason:
        a prediction must not be able to become an action just by being asked
        to run twice.
        """
        option = self._string(args, "option", required=True)
        confidence = self._string(args, "confidence", required=True)
        raw_effects = args.get("effects") or []
        if not isinstance(raw_effects, list):
            raise ToolError("'effects' must be a list.")

        effects: list[tuple[str, str]] = []
        for item in raw_effects:
            if not isinstance(item, dict):
                raise ToolError("Each effect must be an object with 'kind' and 'text'.")
            kind, text = item.get("kind"), item.get("text")
            if not isinstance(kind, str) or not isinstance(text, str):
                raise ToolError("An effect's 'kind' and 'text' must be strings.")
            effects.append((kind, text))

        if self._missions is None:
            return ToolOutcome(immediate=_error(
                "NO_MISSION", "Missions are not available in this window.",
                hint="Answer the user directly instead."),
                activity="Recording a prediction")

        result = self._missions.save_ghost_run(option, confidence, effects)
        if result.get("status") == "saved":
            return ToolOutcome(
                immediate={"ok": True, "ghost_run_id": result.get("ghost_run_id"),
                           "note": "Recorded as a prediction, not carried out. "
                                   "Nothing was done and nothing was approved."},
                activity=_ghost_run_activity(option))
        return ToolOutcome(immediate=_ghost_run_error(result),
                           activity="Recording a prediction")

    def _run_set_progress(self, args: dict) -> ToolOutcome:
        label = self._string(args, "label", required=True)
        if self._missions is None:
            return ToolOutcome(immediate=_error(
                "NO_MISSION", "Missions are not available in this window.",
                hint="Carry on with the task; nothing needs recording."),
                activity="Updating progress")
        result = self._missions.set_progress(label)
        if result.get("status") == "saved":
            return ToolOutcome(immediate={"ok": True}, activity=label or "Updating progress")
        return ToolOutcome(immediate=_error(
            "NO_MISSION", "There is no active mission to update."),
            activity="Updating progress")

    def _run_save_result(self, args: dict) -> ToolOutcome:
        """Record the mission's outcome. Writes rows only - see _run_save_decision."""
        text = self._string(args, "text", required=True)
        raw_follow_ups = args.get("follow_ups")
        follow_ups: list[str] | None = None
        if raw_follow_ups is not None:
            if not isinstance(raw_follow_ups, list) or not all(
                    isinstance(item, str) for item in raw_follow_ups):
                raise ToolError("'follow_ups' must be a list of strings.")
            follow_ups = raw_follow_ups

        if self._missions is None:
            return ToolOutcome(immediate=_error(
                "NO_MISSION", "Missions are not available in this window.",
                hint="Answer the user directly instead."),
                activity="Recording the result")

        result = self._missions.set_result(text, follow_ups)
        status = result.get("status")
        if status == "saved":
            return ToolOutcome(immediate={"ok": True}, activity="Recording the result")
        if status == "too_long":
            if result.get("field") == "follow_ups":
                message = (f"A follow-up is too long, or there are too many "
                          f"(max {result.get('limit')}).")
                hint = "Shorten or trim the follow-ups and save again."
            else:
                message = f"The result is too long (max {result.get('limit')} characters)."
                hint = "Shorten it and save again."
            return ToolOutcome(immediate=_error("TOO_LONG", message, hint=hint),
                              activity="Recording the result")
        return ToolOutcome(immediate=_error(
            "NO_MISSION", "There is no active mission to record a result for."),
            activity="Recording the result")

    def _run_save_constraints(self, args: dict) -> ToolOutcome:
        """Replace the mission's constraints wholesale - see
        MissionService.save_constraints."""
        raw = args.get("constraints")
        if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
            raise ToolError("'constraints' must be a list of strings.")
        if self._missions is None:
            return ToolOutcome(immediate=_error(
                "NO_MISSION", "Missions are not available in this window.",
                hint="Carry on with the task; nothing needs recording."),
                activity="Recording the mission's constraints")

        result = self._missions.save_constraints(raw)
        status = result.get("status")
        if status == "saved":
            return ToolOutcome(immediate={"ok": True},
                              activity="Recording the mission's constraints")
        if status == "too_long":
            message = (f"A constraint is too long, or there are too many "
                      f"(max {result.get('limit')}).")
            return ToolOutcome(immediate=_error(
                "TOO_LONG", message, hint="Shorten or trim the list and save again."),
                activity="Recording the mission's constraints")
        return ToolOutcome(immediate=_error(
            "NO_MISSION", "There is no active mission to record constraints for."),
            activity="Recording the mission's constraints")

    def _run_search(self, args: dict) -> ToolOutcome:
        """knowledge_search - local-only retrieval over the Phase 13
        knowledge index. Never touches the web; an empty/disabled index
        is a normal (not an error) result, since "nothing local matched"
        is exactly the signal that fresh research is needed."""
        query = self._string(args, "query", required=True)
        limit = self._int(args, "limit", 5)
        if self._knowledge is None or not self._knowledge.enabled:
            return ToolOutcome(
                immediate={"ok": True, "results": [],
                          "note": "Semantic history is unavailable or disabled."},
                activity="Searching local knowledge")
        from app.knowledge.retrieval import search
        from app.knowledge.types import SourceType

        chunks = self._knowledge.store.all_chunks()
        results = search(chunks, query, limit=max(1, min(limit, 20)))
        payload = [{
            "source_type": SourceType.LABELS.get(r.chunk.source_type, r.chunk.source_type),
            "title": r.chunk.title, "location": r.chunk.location,
            "timestamp": r.chunk.timestamp, "excerpt": r.excerpt, "stale": r.stale,
        } for r in results]
        return ToolOutcome(
            immediate={"ok": True, "results": payload}, activity="Searching local knowledge")

    # -- Phase 19: research/knowledge graph - read-only ---------------------
    @staticmethod
    def _graph_node_summary(node) -> dict[str, Any] | None:
        """A small, fenced summary of one graph node - never the raw data
        dict unfenced, since it may carry text drawn from a webpage/PDF/
        file (Part SECURITY: a graph node is untrusted evidence, never an
        instruction)."""
        if node is None:
            return None
        payload = {"title": node.title, "source_ref": node.source_ref, "data": node.data}
        return {
            "id": node.id, "type": node.node_type,
            "content": wrap_untrusted(payload, provenance=Provenance.KNOWLEDGE_RETRIEVAL),
        }

    def _run_graph_search(self, args: dict) -> ToolOutcome:
        query = self._string(args, "query", required=True)
        node_type = self._string(args, "node_type") or None
        if self._graph is None:
            return ToolOutcome(
                immediate={"ok": True, "results": [], "note": "The research graph is unavailable."},
                activity="Searching the research graph")
        from app.knowledge_graph.types import NodeType

        node_types = (node_type,) if node_type in NodeType.ALL else None
        results = self._graph.search(query, node_types=node_types, limit=10)
        payload = [self._graph_node_summary(n) for n in results]
        return ToolOutcome(immediate={"ok": True, "results": payload},
                           activity="Searching the research graph")

    def _run_graph_sources(self, args: dict) -> ToolOutcome:
        node_id = self._string(args, "node_id", required=True)
        if self._graph is None:
            return ToolOutcome(
                immediate={"ok": True, "sources": [], "note": "The research graph is unavailable."},
                activity="Looking up graph sources")
        from app.knowledge_graph.types import NodeType

        node = self._graph.get_node(node_id)
        if node is None:
            return ToolOutcome(immediate=_error(
                "NOT_FOUND", f"No graph node with id '{node_id}'.",
                hint="Use knowledge_graph_search to find a valid node id."),
                activity="Looking up graph sources")
        if node.node_type == NodeType.CLAIM:
            sources = [self._graph_node_summary(n) for n in self._graph.sources_for_claim(node_id)]
            contradictions = [
                {"kind": kind, **(self._graph_node_summary(n) or {})}
                for n, kind in self._graph.contradictions_for_claim(node_id)
            ]
            return ToolOutcome(
                immediate={"ok": True, "sources": sources, "contradictions": contradictions},
                activity="Looking up graph sources")
        related = [self._graph_node_summary(n) for n in self._graph.related_sources(node_id)]
        findings = [self._graph_node_summary(n) for n in self._graph.findings_for_source(node_id)]
        missions = [self._graph_node_summary(n) for n in self._graph.missions_for_source(node_id)]
        return ToolOutcome(
            immediate={"ok": True, "related_sources": related, "findings": findings,
                      "missions": missions},
            activity="Looking up graph sources")

    def _run_graph_related(self, args: dict) -> ToolOutcome:
        node_id = self._string(args, "node_id", required=True)
        if self._graph is None:
            return ToolOutcome(
                immediate={"ok": True, "neighbors": [], "note": "The research graph is unavailable."},
                activity="Finding related graph nodes")
        neighbors = self._graph.neighbors(node_id, limit=25)
        payload = [
            {"relationship": n.edge.edge_type, "direction": n.direction,
             **(self._graph_node_summary(n.node) or {})}
            for n in neighbors if n.node is not None
        ]
        return ToolOutcome(immediate={"ok": True, "neighbors": payload},
                           activity="Finding related graph nodes")

    def _run_graph_provenance(self, args: dict) -> ToolOutcome:
        node_id = self._string(args, "node_id", required=True)
        if self._graph is None:
            return ToolOutcome(
                immediate={"ok": True, "chain": [], "note": "The research graph is unavailable."},
                activity="Tracing graph provenance")
        chain = [self._graph_node_summary(n) for n in self._graph.provenance_chain(node_id)]
        return ToolOutcome(immediate={"ok": True, "chain": chain}, activity="Tracing graph provenance")

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

    def _run_get_pdf_text(self, args: dict) -> ToolOutcome:
        # get_pdf_text is synchronous (plain file/network I/O, not tied to the
        # WebEngine event loop) but still returns page-shaped content that
        # must go through render()'s untrusted-content fencing like any other
        # page read - so it goes through the future path, not `immediate`.
        return ToolOutcome(future=resolved(
            "get_pdf_text", self._browser.get_pdf_text(self._tab(args))))

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

    # -- Phase 14: visual computer-use fallback ---------------------------
    # A screenshot never counts against the write-action budget below (it
    # is read-only), but does count against its own MAX_SCREENSHOTS_PER_TASK.
    def _run_visual_observe(self, args: dict) -> ToolOutcome:
        tab_id = self._tab(args)
        if not self.visual_budget.can_screenshot():
            return ToolOutcome(immediate=_error(
                "VISUAL_BUDGET_EXCEEDED",
                f"This task has already taken {visual_module.MAX_SCREENSHOTS_PER_TASK} "
                "visual observations.",
                hint="Rely on the structured tools (browser_get_page) instead, "
                     "or ask the user for help."),
                activity="Looking at the page")

        result_future = BrowserFuture("visual_observe_tool")

        def on_observed(outcome: Any) -> None:
            observation, error = outcome
            if observation is None:
                result_future.set_result(_error(
                    "SCRIPT_FAILED", error or "Could not capture the page."))
                return
            self.visual_budget.record_screenshot()
            payload = {"ok": True, **observation.to_dict()}
            # run() already refuses this tool outright when not
            # vision_capable (see run()'s defense-in-depth check) -
            # reaching here means an image is always wanted. Sensitive
            # fields (password/API-key/payment inputs) were already
            # blacked out in the pixel data itself by observe() before it
            # ever became this attachment - see app.browser.visual.observe.
            payload["__image__"] = {
                "mime_type": observation.image.mime_type, "data": observation.image.base64}
            result_future.set_result(payload)

        visual_module.observe(self._browser, tab_id).then(on_observed)
        return ToolOutcome(visual_observe_future=result_future, activity="Looking at the page")

    def _run_visual_scroll(self, args: dict) -> ToolOutcome:
        if not self.visual_budget.can_scroll():
            return ToolOutcome(immediate=_error(
                "VISUAL_BUDGET_EXCEEDED",
                f"This task has already scrolled {visual_module.MAX_SCROLLS_PER_TASK} "
                "times in visual mode.",
                hint="Ask the user for help if more scrolling is needed."),
                activity="Scrolling")
        self.visual_budget.record_scroll()
        return self._run_scroll(args)

    def _run_visual_focus(self, args: dict) -> ToolOutcome:
        x = self._int(args, "x")
        y = self._int(args, "y")
        if x is None or y is None:
            raise ToolError("'x' and 'y' are required.")
        return ToolOutcome(future=self._browser.visual_focus_at(x, y, self._tab(args)),
                           activity="Focusing a point on the page")

    def _run_visual_click(self, args: dict) -> ToolOutcome:
        """Perform the click. Classification/confirmation already happened
        in assess_async() before AgentSession ever called run() (see
        AgentSession._next_tool / needs_async_assessment) - the real
        ConfirmationRequest flow, not a model-supplied flag. This handler's
        only job is the budget check and the actual dispatch."""
        x = self._int(args, "x")
        y = self._int(args, "y")
        if x is None or y is None:
            raise ToolError("'x' and 'y' are required.")
        if not self.visual_budget.can_act():
            return ToolOutcome(immediate=_error(
                "VISUAL_BUDGET_EXCEEDED",
                f"This task has already performed {visual_module.MAX_VISUAL_ACTIONS_PER_TASK} "
                "visual actions.",
                hint="Ask the user for help rather than continuing to guess at coordinates."),
                activity="Clicking a point on the page")
        self.visual_budget.record_action()
        return ToolOutcome(future=self._browser.visual_click_at(x, y, self._tab(args)),
                           activity="Clicking a point on the page")

    def _run_visual_type(self, args: dict) -> ToolOutcome:
        """Perform the type. See _run_visual_click - the confirmation gate
        already ran in assess_async(), not here."""
        text = self._string(args, "text", required=True)
        if not self.visual_budget.can_act():
            return ToolOutcome(immediate=_error(
                "VISUAL_BUDGET_EXCEEDED",
                f"This task has already performed {visual_module.MAX_VISUAL_ACTIONS_PER_TASK} "
                "visual actions.",
                hint="Ask the user for help rather than continuing to guess at coordinates."),
                activity="Typing into the focused field")
        self.visual_budget.record_action()
        return ToolOutcome(future=self._browser.visual_type_into_focused(text, self._tab(args)),
                           activity="Typing into the focused field")

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
        if result.ok and result.action not in (
                "get_page_structure", "get_page_text", "get_pdf_text"):
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
            text_block: dict[str, Any] = {"page_text": text,
                                          "truncated": result.data.get("truncated", False)}
            # A PDF read carries a little extra shape (page count, whether it
            # looks scanned, its title) that a plain page-text read does not.
            is_pdf = "page_count" in result.data or "is_scanned" in result.data
            for key in ("page_count", "is_scanned", "title"):
                if key in result.data:
                    text_block[key] = result.data[key]
            blocks.append(wrap_untrusted(
                text_block, provenance=Provenance.PDF if is_pdf else Provenance.WEBPAGE))
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
