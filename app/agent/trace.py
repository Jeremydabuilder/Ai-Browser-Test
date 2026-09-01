"""Structured records of what the agent did, and why.

An agent loop is opaque from the outside: something happened, several tools
ran, an answer appeared. When it goes wrong - a tool that keeps failing, a task
that hits its step limit, an approval the user does not remember giving - there
has to be a record that says what happened in order.

What is recorded
----------------
Events, each a small dict with a name, a monotonic offset from the start of the
task, and a few fields describing *what kind of thing* happened. They are
designed to be readable by a person and countable by a test.

What is deliberately NOT recorded
---------------------------------
* **Page content.** Only sizes. A trace that quoted pages would be a copy of
  everything the user browsed, sitting in memory, with none of the care the
  page itself gets.
* **Anything typed.** `type_text` records the field and the length, never the
  text - the same rule the agent panel already follows for passwords.
* **Credentials.** Nothing here ever sees an API key; the trace records the
  *name* of a failure, not the exception detail that might quote a header.

Where it goes
-------------
In memory, capped, per session. Nothing is written to disk: a browsing trace is
sensitive, and a file the user did not ask for is a file they have to know to
delete. `export()` hands the current task's events to whoever asks - the panel's
"Show details", a bug report the user chooses to make.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

#: Event names. Fixed set, so a reader can rely on them and a test can assert
#: on them without matching prose.
TASK_STARTED = "task_started"
MODEL_REQUESTED = "model_requested"
MODEL_RESPONDED = "model_responded"
TOOL_REQUESTED = "tool_requested"
TOOL_REJECTED = "tool_rejected"           # failed validation; never ran
APPROVAL_REQUESTED = "approval_requested"
APPROVAL_GRANTED = "approval_granted"
APPROVAL_DENIED = "approval_denied"
TOOL_STARTED = "tool_started"
TOOL_SUCCEEDED = "tool_succeeded"
TOOL_FAILED = "tool_failed"
TASK_FINISHED = "task_finished"
TASK_CANCELLED = "task_cancelled"
TASK_ERROR = "task_error"


@dataclass(frozen=True)
class Event:
    name: str
    at_ms: int
    detail: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        """One line, for a person."""
        parts = [f"{self.at_ms / 1000:7.2f}s  {self.name}"]
        if self.detail:
            parts.append("  " + " ".join(f"{k}={v}" for k, v in self.detail.items()))
        return "".join(parts)


class Trace:
    """The events of one agent task.

    Capped: a runaway loop is exactly when a trace is most wanted and least
    affordable, so the oldest events are dropped rather than the newest.
    """

    def __init__(self, limit: int = 500) -> None:
        self._events: deque[Event] = deque(maxlen=limit)
        self._started = time.monotonic()

    def start(self) -> None:
        self._events.clear()
        self._started = time.monotonic()

    def record(self, name: str, **detail: Any) -> Event:
        event = Event(
            name=name,
            at_ms=int((time.monotonic() - self._started) * 1000),
            detail={k: v for k, v in detail.items() if v is not None},
        )
        self._events.append(event)
        return event

    # -- reading ----------------------------------------------------------
    @property
    def events(self) -> list[Event]:
        return list(self._events)

    def count(self, name: str) -> int:
        return sum(1 for event in self._events if event.name == name)

    def names(self) -> list[str]:
        return [event.name for event in self._events]

    def export(self) -> str:
        """The whole trace as text, for a bug report or a details pane."""
        return "\n".join(event.describe() for event in self._events)


def summarise_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """What is safe to record about a tool call.

    References, URLs and tab ids describe *where* something happened and are
    worth keeping. Typed text is the one argument that is routinely a secret,
    so only its length is recorded - a password's length is not a password.
    """
    safe: dict[str, Any] = {}
    for key in ("ref", "tab_id", "direction", "checked", "value"):
        if key in arguments:
            safe[key] = arguments[key]
    if "url" in arguments:
        safe["url"] = _origin_of(str(arguments["url"]))
    if "text" in arguments:
        safe["text_length"] = len(str(arguments["text"]))
    if "queries" in arguments:
        value = arguments["queries"]
        safe["queries"] = len(value) if isinstance(value, list) else 1
    return safe


def _origin_of(url: str) -> str:
    """Scheme and host only. A full URL can carry a token in its query."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return "?"
    if not parts.scheme:
        return url[:40]
    return f"{parts.scheme}://{parts.netloc}" if parts.netloc else f"{parts.scheme}:"
