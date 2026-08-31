"""Contracts the Phase 2 AI agent will be built against.

These are Protocols, not implementations. Writing them now is cheap and it
forces Phase 1 to expose the right seams: notice that every capability below
can already be satisfied by ``BrowserTab`` (navigate / run_javascript / url)
plus ``TabManager``. That is the point - Phase 2 should not require rewriting
the browser.

Nothing in this file is imported by the running application.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable


@dataclass(frozen=True)
class PageElement:
    """One interactive element the model is allowed to reference.

    ``ref`` is a short opaque handle ("e12") that the agent sends back to act on
    the element. Handles rather than raw CSS selectors keep the model from
    inventing selectors that do not exist, and keep prompts small.
    """

    ref: str
    role: str                 # link, button, textbox, checkbox, combobox…
    name: str                 # accessible name (label, aria-label, text)
    value: str = ""
    enabled: bool = True
    visible: bool = True
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PageSnapshot:
    """What the agent "sees" on each turn.

    Deliberately DOM/accessibility based rather than screenshot based: it is far
    cheaper in tokens, it is stable across themes and window sizes, and it gives
    the model exact handles to act on. A screenshot can be added later as an
    optional extra for visual questions.
    """

    url: str
    title: str
    text: str                                  # readable page text, truncated
    elements: list[PageElement] = field(default_factory=list)
    scroll_y: int = 0
    scroll_height: int = 0


class ActionRisk:
    """How much scrutiny an action needs before it runs."""

    SAFE = "safe"          # scroll, read, navigate within the same site
    ELEVATED = "elevated"  # submitting a form, leaving the current site
    SENSITIVE = "sensitive"  # payment, purchase, delete, send, account changes


@runtime_checkable
class BrowserController(Protocol):
    """The only surface the agent is allowed to touch.

    Implemented in Phase 2 by a thin adapter over ``BrowserTab``. Keeping it
    narrow means the agent can be unit-tested against a fake controller with no
    Qt and no network.
    """

    def snapshot(self) -> PageSnapshot: ...
    def navigate(self, url: str) -> None: ...
    def click(self, ref: str) -> None: ...
    def type_text(self, ref: str, text: str, submit: bool = False) -> None: ...
    def scroll(self, delta_y: int) -> None: ...
    def go_back(self) -> None: ...
    def extract(self, instruction: str) -> str: ...


@runtime_checkable
class ConfirmationPolicy(Protocol):
    """Decides whether an action runs, asks the user, or is refused.

    The agent never calls the UI directly; it asks the policy. That keeps the
    "ask before doing something irreversible" rule in one auditable place
    instead of scattered through tool handlers.
    """

    def requires_confirmation(self, action: str, risk: str, detail: str) -> bool: ...
    def confirm(self, prompt: str, callback: Callable[[bool], None]) -> None: ...


@runtime_checkable
class AgentTransport(Protocol):
    """Talks to the Claude API. Swappable so tests can replay fixtures."""

    def send(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_event: Callable[[dict[str, Any]], None],
    ) -> None: ...
