"""Visual computer-use fallback: a small, coordinate-based action
vocabulary for pages structured browser tools cannot reliably reach -
canvas apps, custom controls, unusual dropdowns, sites with poor DOM
semantics.

This is a FALLBACK, never a default path. Every caller (see the
browser_visual_* tools in app/agent/tools.py) is expected to have already
tried, or reasoned about why it cannot use, the structured
BrowserController tools - get_page_structure() plus click(ref)/
type_text(ref) - before ever reaching here.

This module supplies exactly three things, and nothing that duplicates
what already exists elsewhere:

* observe() - a screenshot + viewport metadata, downscaled for token
  efficiency, never retained beyond whatever the caller does with it.
* classify_visual_target() - the SAME safety.classify_click/classify_type
  functions every structured action already goes through, fed the
  element dict a coordinate resolves to (BrowserController.visual_inspect
  builds it in the identical shape describe() builds for a structured
  snapshot - see app/browser/page_script.js). There is no second,
  coordinate-only classifier: a visual target that looks like "Buy now"
  is judged exactly as harshly as a ref-based one would be.
* confidence_for() / VisualBudget - the target-confidence and bounded-
  retry/bounded-action bookkeeping the phase requires, so an agent cannot
  turn "occasional fallback" into an unbounded click-and-hope loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.browser import safety
from app.browser.image_context import ImageAttachment, ImageContextError, screenshot_attachment

#: Longest side a visual observation's screenshot is downscaled to, in CSS
#: pixels - large enough to read on-page text/targets reliably, small
#: enough not to repeatedly burn tokens resending a full-resolution
#: capture on every observe() call (see the phase's screenshot-efficiency
#: requirement).
MAX_SCREENSHOT_DIMENSION = 1280

#: Per-task ceilings. Never configurable up from here by an agent's own
#: request - only a human changing this constant raises them.
MAX_VISUAL_ACTIONS_PER_TASK = 12
MAX_SCREENSHOTS_PER_TASK = 10
MAX_SCROLLS_PER_TASK = 6

#: Bounded retries: a click that does not visibly change the page is
#: reconsidered, not repeated blindly - see MAX_VERIFICATION_RETRIES.
MAX_VERIFICATION_RETRIES = 2

#: Roles describe() can report that count as "a real, nameable control" -
#: used by confidence_for(). A bare <div>/<canvas>/<span> with no ARIA
#: role is "generic" and is never high-confidence no matter how it looks
#: in the screenshot.
_INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "checkbox", "radio",
    "combobox", "listbox", "switch", "slider", "filepicker",
})


class Confidence:
    HIGH = "high"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class VisualObservation:
    """One screenshot-backed look at the page. Not a stored/persisted
    type - callers that want Mission history to remember *that* visual
    fallback happened record a summary (see app.missions.coordinator),
    never this object or its image bytes."""

    image: ImageAttachment
    viewport_width: int
    viewport_height: int
    url: str
    title: str
    scroll_x: int | None
    scroll_y: int | None
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe metadata only - the image itself rides separately as
        a real image content block (see app.agent.tools), never inlined
        as base64 text here."""
        return {
            "viewport_width": self.viewport_width,
            "viewport_height": self.viewport_height,
            "url": self.url,
            "title": self.title,
            "scroll_x": self.scroll_x,
            "scroll_y": self.scroll_y,
            "timestamp": self.timestamp,
        }


@dataclass
class VisualBudget:
    """Per-task counters. One instance lives exactly as long as one agent
    task (see AgentSession) - never persisted, never shared across tasks,
    never raised by anything the model itself can request."""

    actions: int = 0
    screenshots: int = 0
    scrolls: int = 0
    verification_retries: int = 0

    def can_act(self) -> bool:
        return self.actions < MAX_VISUAL_ACTIONS_PER_TASK

    def can_screenshot(self) -> bool:
        return self.screenshots < MAX_SCREENSHOTS_PER_TASK

    def can_scroll(self) -> bool:
        return self.scrolls < MAX_SCROLLS_PER_TASK

    def can_retry_verification(self) -> bool:
        return self.verification_retries < MAX_VERIFICATION_RETRIES

    def record_action(self) -> None:
        self.actions += 1

    def record_screenshot(self) -> None:
        self.screenshots += 1

    def record_scroll(self) -> None:
        self.scrolls += 1

    def record_verification_retry(self) -> None:
        self.verification_retries += 1

    def reset_verification_retries(self) -> None:
        self.verification_retries = 0


def observe(controller: Any, tab_id: int | None = None) -> tuple["VisualObservation | None", str]:
    """A visual observation: screenshot + viewport dims + URL + title +
    scroll position (if available) + timestamp.

    Fully synchronous: the screenshot (BrowserController.grab_tab_pixmap,
    the same primitive the existing manual "ask Py about this
    screenshot" flow uses) and the URL/title (BrowserController.
    get_current_page, which reads state Qt already holds rather than
    touching the page) both are. Scroll position needs a page script
    round trip this module deliberately does not make merely to fill in
    one optional field - "if available" per the phase's own spec - so it
    is always reported as None here; a caller that needs it can still ask
    for it explicitly via BrowserController.visual_viewport.
    """
    pixmap, error = controller.grab_tab_pixmap(tab_id)
    if pixmap is None:
        return None, error or "The page could not be captured."

    scaled = pixmap
    longest = max(pixmap.width(), pixmap.height())
    if longest > MAX_SCREENSHOT_DIMENSION:
        from PySide6.QtCore import Qt

        scaled = pixmap.scaled(
            MAX_SCREENSHOT_DIMENSION, MAX_SCREENSHOT_DIMENSION,
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)

    page = controller.get_current_page(tab_id)
    url = page.page.url if page.ok else ""
    title = page.page.title if page.ok else ""
    width, height = controller.viewport_size(tab_id)

    try:
        attachment = screenshot_attachment(scaled, source=f"visual:{url}")
    except ImageContextError as exc:
        return None, str(exc)

    observation = VisualObservation(
        image=attachment,
        viewport_width=width or scaled.width(),
        viewport_height=height or scaled.height(),
        url=url,
        title=title,
        scroll_x=None,
        scroll_y=None,
        timestamp=time.time(),
    )
    return observation, ""


def classify_visual_target(
    element: dict[str, Any] | None, *, action: str, text: str = "",
) -> safety.SensitivityAssessment:
    """The exact same sensitivity judgement a structured, ref-based action
    would get for this element - never a separate, coordinate-only
    classifier. ``element`` is the dict BrowserController.visual_inspect()
    already returns, built by the same describe() function get_page_
    structure() uses.

    This is the one property that keeps a coordinate from becoming a
    loophole around BrowserController.describe_action(): whatever
    describe() would have called this element had it come from a
    structured snapshot, classify_click/classify_type judges it exactly
    the same way here.
    """
    if action == "type":
        return safety.classify_type(element, text)
    return safety.classify_click(element)


def confidence_for(element: dict[str, Any] | None) -> str:
    """"high" only for a coordinate that clearly resolved to a real,
    visible, enabled, NAMED interactive control. Everything else -
    nothing there, a bare <div>/<canvas> with no accessible name, a
    hidden or disabled node, an unlabelled generic element - is
    "uncertain": the caller (see app.agent.tools) is expected to ask the
    user rather than act on it, especially for anything consequential.
    """
    if not element:
        return Confidence.UNCERTAIN
    if element.get("disabled") or element.get("visible") is False:
        return Confidence.UNCERTAIN
    role = element.get("role", "generic")
    name = (element.get("name") or "").strip()
    if role in _INTERACTIVE_ROLES and name:
        return Confidence.HIGH
    return Confidence.UNCERTAIN
