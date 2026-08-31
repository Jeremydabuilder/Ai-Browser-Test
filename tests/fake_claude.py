"""A scripted stand-in for the Claude API, for deterministic agent tests.

The agent talks to a ``ClaudeTransport`` protocol, not to the SDK, so the whole
loop - tool dispatch, confirmation, cancellation, error recovery - can be
exercised offline with no API key and no cost, and with byte-identical results
on every run.

The fake also records what it was sent, which is how the tests assert on things
that would otherwise be invisible: that page content arrives fenced as
untrusted, that a password never reaches the conversation, that tool results
carry the recovery hint.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

from app.agent.claude_client import AgentResponse, ClaudeError, ToolCall


class ScriptedClaude:
    """Replays a list of turns.

    Each entry is either an ``AgentResponse``, a callable taking the message
    history and returning one (for tests that need to react to what happened),
    or an exception to raise.
    """

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self._lock = threading.Lock()
        #: Every request the session made, in order. Read from the GUI thread
        #: only after the task finishes.
        self.requests: list[dict[str, Any]] = []
        self.delay_event: threading.Event | None = None

    def send(self, *, system: str, messages: list, tools: list) -> AgentResponse:
        with self._lock:
            self.requests.append({
                "system": system,
                # Copy: the session keeps mutating its own list afterwards.
                "messages": [dict(m) for m in messages],
                "tools": tools,
            })
            if not self._script:
                raise ClaudeError("The test script ran out of responses.")
            entry = self._script.pop(0)

        # Lets a test hold a request open to check the UI stays responsive.
        if self.delay_event is not None:
            self.delay_event.wait(timeout=10)

        if isinstance(entry, Exception):
            raise entry
        if callable(entry):
            return entry(messages)
        return entry

    # -- introspection used by the tests ---------------------------------
    @property
    def request_count(self) -> int:
        return len(self.requests)

    def all_text(self) -> str:
        """Every scrap of text the session ever sent, flattened."""
        chunks: list[str] = []
        for request in self.requests:
            chunks.append(request["system"])
            for message in request["messages"]:
                content = message.get("content")
                if isinstance(content, str):
                    chunks.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            value = block.get("content")
                            if isinstance(value, str):
                                chunks.append(value)
                            chunks.append(str(block.get("input", "")))
        return "\n".join(chunks)

    def tool_results(self) -> list[str]:
        """The tool_result contents the session sent back, in order."""
        out: list[str] = []
        for request in self.requests:
            for message in request["messages"]:
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            out.append(str(block.get("content", "")))
        return out


# ---------------------------------------------------------------------------
# Builders, so tests read as a story rather than as dict literals
# ---------------------------------------------------------------------------


def says(text: str) -> AgentResponse:
    """A final answer with no tool calls - ends the loop."""
    return AgentResponse(text=text, stop_reason="end_turn",
                         raw_content=[{"type": "text", "text": text}])


def calls(name: str, arguments: dict[str, Any] | None = None,
          call_id: str = "", text: str = "") -> AgentResponse:
    """One tool call."""
    return calls_many([(name, arguments or {}, call_id)], text=text)


def calls_many(specs: list[tuple], text: str = "") -> AgentResponse:
    """Several tool calls in one turn, as parallel tool use produces."""
    tool_calls = []
    raw: list[dict[str, Any]] = []
    if text:
        raw.append({"type": "text", "text": text})
    for index, spec in enumerate(specs):
        name, arguments = spec[0], spec[1]
        call_id = spec[2] if len(spec) > 2 and spec[2] else f"call_{index}"
        tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
        raw.append({"type": "tool_use", "id": call_id, "name": name, "input": arguments})
    return AgentResponse(text=text, tool_calls=tool_calls,
                         stop_reason="tool_use", raw_content=raw)


def reacting(fn: Callable[[list], AgentResponse]) -> Callable[[list], AgentResponse]:
    """A turn that inspects the history before deciding - for recovery tests."""
    return fn


def last_tool_result(messages: list) -> str:
    """The most recent tool_result content in a message history."""
    for message in reversed(messages):
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    return str(block.get("content", ""))
    return ""


# ---------------------------------------------------------------------------
# Reading the page the way the model does
# ---------------------------------------------------------------------------
#
# A scripted turn runs on the WORKER thread, exactly where the real Claude
# request runs. It must therefore never touch BrowserController - that is the
# GUI thread's alone, and Qt will refuse it. (Qt caught precisely this mistake
# when these tests were first written, which is a good sign the boundary is
# real.)
#
# So a scripted turn picks an element the same way the model does: out of the
# page structure that arrived in the previous tool result. Pure data, no Qt.


def structure_from(messages: list) -> dict:
    """Parse the page structure out of the most recent tool result."""
    from app.agent.tools import UNTRUSTED_CLOSE, UNTRUSTED_OPEN

    payload = last_tool_result(messages)
    if UNTRUSTED_OPEN not in payload:
        return {}
    body = payload.split(UNTRUSTED_OPEN, 1)[1].split(UNTRUSTED_CLOSE, 1)[0].strip()
    try:
        return json.loads(body)
    except (TypeError, ValueError):
        return {}


def find_ref(messages: list, role: str | None = None, name_contains: str = "",
             input_type: str | None = None) -> str:
    """The reference of the first matching element in the latest structure."""
    needle = name_contains.lower()
    for element in structure_from(messages).get("elements", []):
        if role and element.get("role") != role:
            continue
        if input_type and element.get("input_type") != input_type:
            continue
        if needle and needle not in element.get("name", "").lower():
            continue
        return element["ref"]
    raise AssertionError(
        f"no element matching role={role!r} name~{name_contains!r} "
        f"input_type={input_type!r} in the last page structure")
