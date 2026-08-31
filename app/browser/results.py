"""Structured types returned by BrowserController.

Every operation returns an ``ActionResult`` rather than a bare bool or a
string. The point is that a future automation caller - an AI agent included -
should be able to branch on machine-readable fields instead of parsing prose,
while a human reading a log still gets a sentence that makes sense.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class ErrorCode:
    """Machine-readable failure reasons.

    ``recoverable`` in ActionError says whether re-inspecting the page and
    retrying could plausibly work. That is the single most useful bit for an
    automation loop: STALE_* means "look again", INVALID_URL means "you asked
    for something impossible".
    """

    # Reference lifecycle - all mean "re-inspect the page".
    STALE_SNAPSHOT = "STALE_SNAPSHOT"        # snapshot expired or page replaced
    STALE_DOCUMENT = "STALE_DOCUMENT"        # page navigated since the snapshot
    STALE_DETACHED = "STALE_DETACHED"        # element removed from the DOM
    STALE_MUTATED = "STALE_MUTATED"          # node reused for different content
    UNKNOWN_REF = "UNKNOWN_REF"              # no such reference in the snapshot
    INVALID_REF = "INVALID_REF"              # malformed reference string

    # Element state.
    ELEMENT_DISABLED = "ELEMENT_DISABLED"
    ELEMENT_NOT_VISIBLE = "ELEMENT_NOT_VISIBLE"
    ELEMENT_NOT_EDITABLE = "ELEMENT_NOT_EDITABLE"
    ELEMENT_READONLY = "ELEMENT_READONLY"
    ELEMENT_NOT_CHECKABLE = "ELEMENT_NOT_CHECKABLE"
    ELEMENT_NOT_SELECTABLE = "ELEMENT_NOT_SELECTABLE"
    OPTION_NOT_FOUND = "OPTION_NOT_FOUND"
    NO_FORM = "NO_FORM"

    # Navigation and tabs.
    INVALID_URL = "INVALID_URL"
    NO_HISTORY = "NO_HISTORY"                # nothing to go back/forward to
    NO_TAB = "NO_TAB"
    UNKNOWN_TAB = "UNKNOWN_TAB"
    LOAD_FAILED = "LOAD_FAILED"

    # Timing and internals.
    TIMEOUT = "TIMEOUT"
    SCRIPT_FAILED = "SCRIPT_FAILED"          # page script unavailable
    UNSUPPORTED = "UNSUPPORTED"


# Statuses the injected page script can report, mapped to an error code and a
# sentence. Keeping the mapping here (not in JS) means the wording is testable
# in Python and the JS stays a dumb reporter.
_PAGE_STATUS: dict[str, tuple[str, str, bool]] = {
    "unknown_snapshot": (
        ErrorCode.STALE_SNAPSHOT,
        "That page snapshot is no longer available. Inspect the page again to get fresh references.",
        True,
    ),
    "document_changed": (
        ErrorCode.STALE_DOCUMENT,
        "The page changed since that snapshot was taken. Inspect the page again to get fresh references.",
        True,
    ),
    "detached": (
        ErrorCode.STALE_DETACHED,
        "That element has been removed from the page. Inspect the page again.",
        True,
    ),
    "mutated": (
        ErrorCode.STALE_MUTATED,
        "That element now holds different content, so it is not the element that was captured. "
        "Inspect the page again.",
        True,
    ),
    "unknown_ref": (ErrorCode.UNKNOWN_REF, "No element with that reference exists in the snapshot.", True),
    "invalid_ref": (ErrorCode.INVALID_REF, "That element reference is not in a valid format.", False),
    "disabled": (ErrorCode.ELEMENT_DISABLED, "That element is disabled and cannot be used.", False),
    "not_visible": (ErrorCode.ELEMENT_NOT_VISIBLE, "That element is not visible on the page.", False),
    "not_editable": (ErrorCode.ELEMENT_NOT_EDITABLE, "That element does not accept typed text.", False),
    "readonly": (ErrorCode.ELEMENT_READONLY, "That field is read-only.", False),
    "not_checkable": (ErrorCode.ELEMENT_NOT_CHECKABLE, "That element is not a checkbox or switch.", False),
    "not_selectable": (ErrorCode.ELEMENT_NOT_SELECTABLE, "That element is not a dropdown.", False),
    "option_not_found": (ErrorCode.OPTION_NOT_FOUND, "That dropdown has no option with that label or value.", False),
    "no_form": (ErrorCode.NO_FORM, "That element is not inside a form.", False),
    "unknown_op": (ErrorCode.UNSUPPORTED, "That operation is not supported.", False),
}


@dataclass(frozen=True)
class ActionError:
    code: str
    message: str
    recoverable: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def error_from_page_status(status: str) -> ActionError:
    code, message, recoverable = _PAGE_STATUS.get(
        status, (ErrorCode.SCRIPT_FAILED, f"The page reported an unexpected state ({status}).", False)
    )
    return ActionError(code=code, message=message, recoverable=recoverable)


@dataclass(frozen=True)
class ElementRef:
    """The subset of an element's description echoed back with an action."""

    ref: str = ""
    role: str = ""
    name: str = ""
    tag: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PageState:
    """Where the browser ended up. Always present, success or failure."""

    url: str = ""
    title: str = ""
    loading: bool = False
    can_go_back: bool = False
    can_go_forward: bool = False
    tab_id: int = -1
    load_error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Effects:
    """What an action actually caused.

    ``navigated`` and ``dom_changed`` are the two an automation loop needs
    most: they answer "did my click go somewhere, redraw the page, or do
    nothing at all?" without a screenshot or an HTML diff.
    """

    navigated: bool = False
    dom_changed: bool = False
    opened_tab: bool = False
    url_before: str = ""
    url_after: str = ""
    new_tab_id: int | None = None
    scroll_before: int | None = None
    scroll_after: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ActionResult:
    """The single return type for every BrowserController operation."""

    ok: bool
    action: str
    target: ElementRef | None = None
    error: ActionError | None = None
    effects: Effects = field(default_factory=Effects)
    page: PageState = field(default_factory=PageState)
    # Advisory only. Nothing in Phase 1/2-prep enforces this; it exists so a
    # future caller can decide to ask the user first. See app/browser/safety.py.
    sensitivity: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": self.ok,
            "action": self.action,
            "target": self.target.to_dict() if self.target else None,
            "error": self.error.to_dict() if self.error else None,
            "effects": self.effects.to_dict(),
            "page": self.page.to_dict(),
            "sensitivity": self.sensitivity,
            "duration_ms": self.duration_ms,
        }
        if self.data:
            out["data"] = self.data
        return out

    @property
    def should_reinspect(self) -> bool:
        """True when the right response is to fetch a fresh page structure."""
        return bool(self.error and self.error.recoverable)
