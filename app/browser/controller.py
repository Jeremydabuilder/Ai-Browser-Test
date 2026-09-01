"""BrowserController - the programmatic control surface for the browser.

This is a general-purpose browser automation API. It contains no AI, no model,
no network client, and no knowledge that a Phase 2 agent will ever exist.

    Browser UI  ─┐
                 ├─→ BrowserController ─→ Qt WebEngine / Chromium
    Automation  ─┘

The boundary is deliberately narrow, and three properties define it:

**Nothing Qt crosses it.** Every method takes and returns plain data - strings,
ints, dataclasses. A caller never receives a ``BrowserTab``, a
``QWebEngineView`` or a ``QWebEnginePage``, so it cannot reach around the API
into Qt internals. Tabs are addressed by a stable integer ``tab_id``.

**No arbitrary JavaScript.** There is no ``execute_script`` method and there
never should be. JavaScript *is* how DOM inspection is implemented, but that
script is ours, injected into an isolated world, and the caller can neither
supply nor influence it. Callers get semantic operations - click this element,
type into that field - not a shell on the page.

**Everything is asynchronous and says so.** Each operation returns a
``BrowserFuture`` resolving to an ``ActionResult``. Nothing pretends to be
synchronous, because in Qt WebEngine nothing is.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, QUrl, Signal

from app.browser import safety
from app.browser.futures import BrowserFuture, resolved
from app.browser.results import (
    ActionError,
    ActionResult,
    Effects,
    ElementRef,
    ErrorCode,
    PageState,
    error_from_page_status,
)
from app.browser.tab import BrowserTab
from app.browser.tab_manager import TabManager

# How long we watch for the consequences of an action before reporting them.
SETTLE_QUIET_MS = 220      # no DOM mutations for this long counts as settled
SETTLE_MAX_MS = 2500       # ...but never watch longer than this
DEFAULT_TIMEOUT_MS = 30000
DEFAULT_POLL_MS = 100

# A reference is "s3:e12" in the main document, "s3.2:e12" inside the second
# frame of that same snapshot. The frame part is produced and consumed only by
# this module; a caller passes the whole string back and never parses it.
_REF_PATTERN = re.compile(r"^s\d+(?:\.\d+)?:[ef]\d+$")

#: How many frames deep to look, and how many in total. A page can nest frames
#: arbitrarily and some ad-heavy pages carry dozens; both are bounded so one
#: pathological page cannot turn a snapshot into a hundred round-trips.
MAX_FRAME_DEPTH = 3
MAX_FRAMES = 12


def _frame_tag(ref: str) -> str:
    """The frame part of a reference, or "" for the main document."""
    head = ref.split(":", 1)[0]
    return head.split(".", 1)[1] if "." in head else ""


class ScrollDirection:
    UP = "up"
    DOWN = "down"
    TOP = "top"
    BOTTOM = "bottom"


# ---------------------------------------------------------------------------
# Page representation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PageElement:
    """One interactive element, described the way an accessibility tree would.

    Fields are omitted rather than sent empty when they do not apply, so a
    checkbox does not carry an empty ``options`` list and a link does not carry
    a ``placeholder``. That keeps the serialised form compact - it is meant to
    be read by something with a token budget.
    """

    ref: str
    role: str
    name: str = ""
    tag: str = ""
    visible: bool = True
    in_viewport: bool = False
    disabled: bool = False
    value: str | None = None
    placeholder: str | None = None
    input_type: str | None = None
    autocomplete: str | None = None
    field_name: str | None = None
    href: str | None = None
    target: str | None = None
    download: bool | None = None
    checked: bool | None = None
    required: bool | None = None
    readonly: bool | None = None
    secret: bool | None = None
    max_length: int | None = None
    level: int | None = None
    expanded: bool | None = None
    form: int | None = None
    options: list[dict[str, Any]] | None = None
    #: Which frame this element lives in: None for the page's own document,
    #: otherwise the frame's index within this snapshot.
    frame: int | None = None
    #: The frame's origin, when it differs from the page's. Present so a caller
    #: can tell embedded third-party content from the site's own.
    frame_origin: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class PageForm:
    ref: str
    name: str = ""
    action: str = ""
    method: str = "get"
    field_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Heading:
    level: int
    text: str


@dataclass(frozen=True)
class PageStructure:
    """A compact, structured view of the current page.

    Built from the DOM and ARIA roles, never from raw HTML and never from a
    screenshot. Raw HTML is not exposed by this API at all: it is enormous,
    mostly irrelevant, and invites a caller to write selectors instead of using
    element references.
    """

    url: str = ""
    title: str = ""
    lang: str = ""
    snapshot_id: str = ""
    doc_id: str = ""
    dom_revision: int = 0
    headings: list[Heading] = field(default_factory=list)
    forms: list[PageForm] = field(default_factory=list)
    elements: list[PageElement] = field(default_factory=list)
    element_count: int = 0
    elements_truncated: bool = False
    text: str = ""
    text_truncated: bool = False
    scroll_y: int = 0
    scroll_height: int = 0
    viewport_height: int = 0
    viewport_width: int = 0
    at_bottom: bool = False
    tab_id: int = -1
    #: One entry per embedded frame that was read: index, url, origin, and
    #: whether it is same-origin with the page.
    frames: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "lang": self.lang,
            "snapshot_id": self.snapshot_id,
            "tab_id": self.tab_id,
            "scroll": {
                "y": self.scroll_y,
                "height": self.scroll_height,
                "viewport_height": self.viewport_height,
                "at_bottom": self.at_bottom,
            },
            "headings": [asdict(h) for h in self.headings],
            "forms": [f.to_dict() for f in self.forms],
            "elements": [e.to_dict() for e in self.elements],
            "element_count": self.element_count,
            "elements_truncated": self.elements_truncated,
            "text": self.text,
            "text_truncated": self.text_truncated,
            **({"frames": self.frames} if self.frames else {}),
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    # -- lookup helpers --------------------------------------------------
    def find(
        self,
        role: str | None = None,
        name_contains: str | None = None,
        *,
        visible_only: bool = True,
        enabled_only: bool = False,
    ) -> list[PageElement]:
        needle = (name_contains or "").lower()
        results = []
        for element in self.elements:
            if role and element.role != role:
                continue
            if needle and needle not in element.name.lower():
                continue
            if visible_only and not element.visible:
                continue
            if enabled_only and element.disabled:
                continue
            results.append(element)
        return results

    def first(self, role: str | None = None, name_contains: str | None = None) -> PageElement | None:
        matches = self.find(role, name_contains)
        return matches[0] if matches else None

    def by_ref(self, ref: str) -> PageElement | None:
        return next((e for e in self.elements if e.ref == ref), None)

    @property
    def links(self) -> list[PageElement]:
        return self.find(role="link")

    @property
    def buttons(self) -> list[PageElement]:
        return self.find(role="button")

    @property
    def text_fields(self) -> list[PageElement]:
        return [e for e in self.elements
                if e.role in ("textbox", "searchbox", "textarea") and e.visible]

    @property
    def checkboxes(self) -> list[PageElement]:
        return [e for e in self.elements if e.role in ("checkbox", "switch") and e.visible]

    @property
    def radios(self) -> list[PageElement]:
        return self.find(role="radio")

    @property
    def selects(self) -> list[PageElement]:
        return [e for e in self.elements if e.role in ("combobox", "listbox") and e.visible]


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class BrowserController(QObject):
    """The supported programmatic interface to one browser window."""

    #: Emitted for every completed operation. Useful for logging and, later,
    #: for showing an agent's activity in the UI.
    action_completed = Signal(object)  # ActionResult

    def __init__(self, tabs: TabManager, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tabs = tabs
        self._ids: dict[int, BrowserTab] = {}
        self._next_id = 1
        # Remember the last structure per tab so an action can report what it
        # was aiming at, and so sensitivity can be judged from the element's
        # description rather than from a bare reference string.
        self._structures: dict[int, PageStructure] = {}
        for tab in self._tabs.tabs():
            self._register(tab)

    # -- tab identity ----------------------------------------------------
    def _register(self, tab: BrowserTab) -> int:
        for tab_id, known in self._ids.items():
            if known is tab:
                return tab_id
        tab_id = self._next_id
        self._next_id += 1
        self._ids[tab_id] = tab
        return tab_id

    def _id_of(self, tab: BrowserTab) -> int:
        return self._register(tab)

    def _tab_for(self, tab_id: int | None) -> BrowserTab | None:
        """Resolve a tab id, or the active tab when ``tab_id`` is None.

        Ids are never reused, so a stale id from a closed tab reports
        UNKNOWN_TAB rather than silently acting on whatever now sits at that
        index - the same reasoning as stale element references.
        """
        if tab_id is None:
            tab = self._tabs.current_tab()
            if tab is not None:
                self._register(tab)
            return tab
        tab = self._ids.get(tab_id)
        if tab is None or self._tabs.indexOf(tab) == -1:
            return None
        return tab

    # -- public: tabs ----------------------------------------------------
    def open_tab(self, url: str | None = None, *, background: bool = False) -> BrowserFuture:
        """Open a tab and, if ``url`` is given, wait for it to finish loading."""
        started = time.monotonic()
        tab = self._tabs.new_tab(None, background=background)
        tab_id = self._register(tab)
        if url is None:
            return resolved("open_tab", self._success(
                "open_tab", tab, started, effects=Effects(opened_tab=True, new_tab_id=tab_id)))
        future = BrowserFuture("open_tab", self)
        self._navigate_into(tab, url, future, "open_tab", started,
                            extra_effects={"opened_tab": True, "new_tab_id": tab_id})
        return future

    def close_tab(self, tab_id: int | None = None) -> ActionResult:
        """Close a tab. Synchronous: closing a tab completes immediately."""
        started = time.monotonic()
        tab = self._tab_for(tab_id)
        if tab is None:
            return self._failure("close_tab", None, started, ErrorCode.UNKNOWN_TAB,
                                 "There is no open tab with that id.")
        index = self._tabs.indexOf(tab)
        resolved_id = self._id_of(tab)
        self._structures.pop(resolved_id, None)
        self._ids.pop(resolved_id, None)
        self._tabs.close_tab(index)
        result = self._success("close_tab", self._tabs.current_tab(), started,
                               data={"closed_tab_id": resolved_id})
        return result

    def select_tab(self, tab_id: int) -> ActionResult:
        started = time.monotonic()
        tab = self._tab_for(tab_id)
        if tab is None:
            return self._failure("select_tab", None, started, ErrorCode.UNKNOWN_TAB,
                                 "There is no open tab with that id.")
        self._tabs.setCurrentIndex(self._tabs.indexOf(tab))
        return self._success("select_tab", tab, started)

    def list_tabs(self) -> list[dict[str, Any]]:
        current = self._tabs.current_tab()
        return [
            {
                "tab_id": self._register(tab),
                "index": index,
                "title": tab.title(),
                "url": tab.url().toString(),
                "active": tab is current,
                "loading": tab.is_loading,
            }
            for index, tab in enumerate(self._tabs.tabs())
        ]

    def tab_count(self) -> int:
        return self._tabs.count()

    # -- public: navigation ----------------------------------------------
    def navigate(self, url: str, tab_id: int | None = None) -> BrowserFuture:
        """Load ``url``; the future resolves when the page finishes loading."""
        started = time.monotonic()
        tab = self._tab_for(tab_id)
        if tab is None:
            return resolved("navigate", self._no_tab("navigate", started))
        future = BrowserFuture("navigate", self)
        self._navigate_into(tab, url, future, "navigate", started)
        return future

    def go_back(self, tab_id: int | None = None) -> BrowserFuture:
        return self._history_move("go_back", tab_id)

    def go_forward(self, tab_id: int | None = None) -> BrowserFuture:
        return self._history_move("go_forward", tab_id)

    def reload(self, tab_id: int | None = None) -> BrowserFuture:
        started = time.monotonic()
        tab = self._tab_for(tab_id)
        if tab is None:
            return resolved("reload", self._no_tab("reload", started))
        future = BrowserFuture("reload", self)
        before = tab.url().toString()
        self._invalidate(tab)
        tab.reload()
        self._await_load(tab, future, "reload", started, before)
        return future

    def stop(self, tab_id: int | None = None) -> ActionResult:
        started = time.monotonic()
        tab = self._tab_for(tab_id)
        if tab is None:
            return self._no_tab("stop", started)
        tab.stop()
        return self._success("stop", tab, started)

    def _history_move(self, action: str, tab_id: int | None) -> BrowserFuture:
        started = time.monotonic()
        tab = self._tab_for(tab_id)
        if tab is None:
            return resolved(action, self._no_tab(action, started))
        can_move = tab.can_go_back() if action == "go_back" else tab.can_go_forward()
        if not can_move:
            direction = "back" if action == "go_back" else "forward"
            return resolved(action, self._failure(
                action, tab, started, ErrorCode.NO_HISTORY,
                f"There is no page to go {direction} to in this tab."))
        before = tab.url().toString()
        self._invalidate(tab)
        future = BrowserFuture(action, self)
        # A back/forward move served from the back-forward cache emits no load
        # signals at all, so we watch the URL as well as loadFinished.
        self._await_navigation_or_url_change(tab, future, action, started, before)
        tab.back() if action == "go_back" else tab.forward()
        return future

    # -- public: reading the page ----------------------------------------
    def get_current_page(self, tab_id: int | None = None) -> ActionResult:
        """Cheap synchronous status: URL, title, loading state, last error.

        Synchronous because it reads state Qt already holds; it never touches
        the page. Use ``get_page_structure`` when you need the contents.
        """
        started = time.monotonic()
        tab = self._tab_for(tab_id)
        if tab is None:
            return self._no_tab("get_current_page", started)
        return self._success("get_current_page", tab, started)

    def get_page_structure(
        self,
        tab_id: int | None = None,
        *,
        max_elements: int = 300,
        max_text: int = 20000,
        include_invisible: bool = False,
        include_frames: bool = True,
    ) -> BrowserFuture:
        """Capture a fresh structural snapshot of the page, frames included.

        Every call mints a NEW snapshot id, and element references are scoped
        to it. References from an older snapshot keep working only while the
        document and the elements themselves are unchanged - see the module
        documentation on staleness.

        A page with iframes is captured as one logical snapshot spanning
        several documents: the main document first, then each frame, all filed
        under the same snapshot id. Elements from a frame carry the frame's
        index and origin, so a caller can tell embedded third-party content
        from the page's own. Pass ``include_frames=False`` for the main
        document alone.
        """
        started = time.monotonic()
        tab = self._tab_for(tab_id)
        if tab is None:
            return resolved("get_page_structure", self._no_tab("get_page_structure", started))
        future = BrowserFuture("get_page_structure", self)

        def options_for(snapshot_id: str | None) -> str:
            payload: dict[str, Any] = {
                "max_elements": max_elements,
                "max_text": max_text,
                "include_invisible": include_invisible,
            }
            if snapshot_id:
                payload["snapshot_id"] = snapshot_id
            return json.dumps(payload)

        def fail() -> None:
            self._finish(future, self._failure(
                "get_page_structure", tab, started, ErrorCode.SCRIPT_FAILED,
                "The page could not be inspected. It may still be loading, "
                "or it may be an internal page that does not allow inspection."))

        def on_result(raw: Any) -> None:
            if not isinstance(raw, dict):
                fail()
                return
            frames = self._frames(tab)[1:] if include_frames else []
            if not frames:
                finish(raw, [])
                return
            # Capture each frame under the main document's snapshot id, so one
            # reference space covers the whole page.
            snapshot_id = raw.get("snapshot_id") or ""
            collected: list[tuple[str, dict]] = []
            pending = {"count": len(frames)}

            def collect(tag: str, frame_raw: Any) -> None:
                if isinstance(frame_raw, dict):
                    collected.append((tag, frame_raw))
                pending["count"] -= 1
                if pending["count"] == 0:
                    collected.sort(key=lambda item: int(item[0]))
                    finish(raw, collected)

            for tag, frame in frames:
                self._call_page(
                    tab, f"window.__pb.capture({options_for(f'{snapshot_id}.{tag}')})",
                    lambda frame_raw, t=tag: collect(t, frame_raw), frame=frame)

        def finish(raw: dict, frames: list[tuple[str, dict]]) -> None:
            structure = self._structure_from(raw, tab, frames)
            self._structures[self._id_of(tab)] = structure
            self._finish(future, self._success(
                "get_page_structure", tab, started, data={"structure": structure}))

        def capture() -> None:
            self._call_page(tab, f"window.__pb.capture({options_for(None)})", on_result)

        # Inspecting a page mid-navigation would race the document swap and
        # fail spuriously. Waiting first means a caller always gets the
        # structure of the page that actually ends up loaded, which is what it
        # asked for - and removes a race the caller would otherwise have to
        # know about.
        if tab.is_loading:
            self.wait_for_load(tab_id).then(lambda _result: capture())
        else:
            capture()

        future.set_timeout(DEFAULT_TIMEOUT_MS, lambda: self._failure(
            "get_page_structure", tab, started, ErrorCode.TIMEOUT,
            "Inspecting the page took too long."))
        return future

    def get_page_text(self, tab_id: int | None = None, *, max_chars: int = 20000) -> BrowserFuture:
        """The page's readable text only - no element references."""
        started = time.monotonic()
        tab = self._tab_for(tab_id)
        if tab is None:
            return resolved("get_page_text", self._no_tab("get_page_text", started))
        future = BrowserFuture("get_page_text", self)

        def on_result(raw: Any) -> None:
            text = "" if raw is None else str(raw)
            self._finish(future, self._success(
                "get_page_text", tab, started,
                data={"text": text[:max_chars], "truncated": len(text) > max_chars}))

        # toPlainText is Qt's own extraction; no page script needed.
        tab.page.toPlainText(on_result)
        future.set_timeout(DEFAULT_TIMEOUT_MS, lambda: self._failure(
            "get_page_text", tab, started, ErrorCode.TIMEOUT, "Reading the page took too long."))
        return future

    def find_elements(
        self,
        queries: list[str] | None = None,
        *,
        role: str | None = None,
        limit: int = 10,
        include_invisible: bool = False,
        tab_id: int | None = None,
    ) -> BrowserFuture:
        """Search the whole page for elements matching any of ``queries``.

        Unlike ``get_page_structure`` this is not capped at the element limit:
        the control a caller wants is often past the cap on a large page. It
        returns a short ranked list with match scores and a total count, so the
        caller can distinguish one obvious match from several plausible ones.

        The references it hands back come from a real snapshot, so they resolve
        and go stale on exactly the same terms as any others.
        """
        started = time.monotonic()
        tab = self._tab_for(tab_id)
        if tab is None:
            return resolved("find_elements", self._no_tab("find_elements", started))
        future = BrowserFuture("find_elements", self)
        options = json.dumps({
            "queries": [str(q) for q in (queries or []) if str(q).strip()],
            "role": role or "",
            "limit": max(1, min(limit, 40)),
            "include_invisible": include_invisible,
        })

        def on_result(raw: Any) -> None:
            if not isinstance(raw, dict):
                self._finish(future, self._failure(
                    "find_elements", tab, started, ErrorCode.SCRIPT_FAILED,
                    "The page could not be searched. It may still be loading."))
                return
            known = {f.name for f in PageElement.__dataclass_fields__.values()}  # type: ignore[attr-defined]
            matches = [
                {**{k: v for k, v in item.items() if k in known},
                 "match_score": item.get("match_score", 0)}
                for item in raw.get("matches", [])
            ]
            self._finish(future, self._success(
                "find_elements", tab, started,
                data={"matches": matches,
                      "total_matches": raw.get("total_matches", len(matches)),
                      "snapshot_id": raw.get("snapshot_id", "")}))

        def run() -> None:
            self._call_page(tab, f"window.__pb.search({options})", on_result)

        if tab.is_loading:
            self.wait_for_load(tab_id).then(lambda _r: run())
        else:
            run()
        future.set_timeout(DEFAULT_TIMEOUT_MS, lambda: self._failure(
            "find_elements", tab, started, ErrorCode.TIMEOUT,
            "Searching the page took too long."))
        return future

    def inspect_element(self, ref: str, tab_id: int | None = None) -> BrowserFuture:
        """Re-read one element - the cheap way to check a reference is still good."""
        return self._page_action("inspect_element", {"op": "inspect", "ref": ref}, ref, tab_id,
                                 watch_effects=False)

    # -- public: acting on the page --------------------------------------
    def click(self, ref: str, tab_id: int | None = None) -> BrowserFuture:
        """Click an element, then report whether it navigated or changed the DOM."""
        return self._page_action("click", {"op": "click", "ref": ref}, ref, tab_id)

    def type_text(
        self,
        ref: str,
        text: str,
        *,
        submit: bool = False,
        append: bool = False,
        tab_id: int | None = None,
    ) -> BrowserFuture:
        """Type into a field. ``submit=True`` submits its form afterwards."""
        request = {"op": "type", "ref": ref, "text": text, "append": append}
        future = self._page_action("type_text", request, ref, tab_id, typed_text=text)
        if not submit:
            return future
        chained = BrowserFuture("type_text", self)

        def after_typing(result: ActionResult) -> None:
            if not result.ok:
                chained.set_result(result)
                return
            self.submit(ref, tab_id=tab_id).then(chained.set_result)

        future.then(after_typing)
        return chained

    def submit(self, ref: str, tab_id: int | None = None) -> BrowserFuture:
        """Submit the form containing ``ref`` (or ``ref`` itself if it is a form)."""
        return self._page_action("submit", {"op": "submit", "ref": ref}, ref, tab_id)

    def set_checked(self, ref: str, checked: bool = True, tab_id: int | None = None) -> BrowserFuture:
        return self._page_action("set_checked", {"op": "set_checked", "ref": ref, "checked": checked},
                                 ref, tab_id)

    def select_option(self, ref: str, value: str, tab_id: int | None = None) -> BrowserFuture:
        return self._page_action("select_option", {"op": "select_option", "ref": ref, "value": value},
                                 ref, tab_id)

    def focus(self, ref: str, tab_id: int | None = None) -> BrowserFuture:
        return self._page_action("focus", {"op": "focus", "ref": ref}, ref, tab_id,
                                 watch_effects=False)

    def scroll_to_element(self, ref: str, tab_id: int | None = None) -> BrowserFuture:
        return self._page_action("scroll_to_element", {"op": "scroll_to", "ref": ref}, ref, tab_id,
                                 watch_effects=False)

    def scroll(
        self,
        direction: str = ScrollDirection.DOWN,
        amount: int | None = None,
        tab_id: int | None = None,
    ) -> BrowserFuture:
        request = {"op": "scroll", "direction": direction, "amount": amount or 0}
        return self._page_action("scroll", request, None, tab_id, watch_effects=False)

    def screenshot(self, path: str, tab_id: int | None = None) -> ActionResult:
        """Save a PNG of what the tab is currently showing.

        A picture of the rendered page, not of the DOM: it captures what a
        person would see, which is the one thing the structured page
        representation cannot express (layout, overlap, whether something is
        actually visible).

        Read-only and synchronous - `QWidget.grab()` copies the widget's
        current backing store, so there is nothing to wait for and no future
        to resolve. It captures the page only, never the browser's own chrome.
        """
        started = time.monotonic()
        tab = self._tab_for(tab_id)
        if tab is None:
            return self._no_tab("screenshot", started)
        try:
            image = tab.view.grab()
            if image.isNull():
                return self._failure(
                    "screenshot", tab, started, ErrorCode.SCRIPT_FAILED,
                    "The page could not be captured.",
                    detail="The tab has no rendered surface yet.")
            if not image.save(path, "PNG"):
                return self._failure(
                    "screenshot", tab, started, ErrorCode.SCRIPT_FAILED,
                    f"Could not write the image to {path}.",
                    detail="Check that the directory exists and is writable.")
        except Exception as exc:  # noqa: BLE001
            return self._failure("screenshot", tab, started, ErrorCode.SCRIPT_FAILED,
                                 "Capturing the page failed.", detail=str(exc))
        result = self._success("screenshot", tab, started)
        result.data["path"] = path
        result.data["width"] = image.width()
        result.data["height"] = image.height()
        return result

    # -- public: waiting --------------------------------------------------
    def wait_for_load(self, tab_id: int | None = None, *, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> BrowserFuture:
        """Resolve when the tab is not loading (immediately if it already isn't)."""
        started = time.monotonic()
        tab = self._tab_for(tab_id)
        if tab is None:
            return resolved("wait_for_load", self._no_tab("wait_for_load", started))
        future = BrowserFuture("wait_for_load", self)
        if not tab.is_loading:
            future.set_result(self._success("wait_for_load", tab, started))
            return future
        self._await_load(tab, future, "wait_for_load", started, tab.url().toString(),
                         timeout_ms=timeout_ms)
        return future

    def wait_for_element(
        self,
        *,
        role: str | None = None,
        name_contains: str | None = None,
        text_contains: str | None = None,
        tab_id: int | None = None,
        timeout_ms: int = 10000,
        poll_ms: int = DEFAULT_POLL_MS,
    ) -> BrowserFuture:
        """Poll until a matching element (or page text) appears.

        This is how a caller handles content that arrives late - lazy lists,
        a spinner replaced by results, anything rendered after the load event.
        It polls a cheap predicate rather than capturing a full snapshot each
        time, and it never creates element references.
        """
        started = time.monotonic()
        tab = self._tab_for(tab_id)
        if tab is None:
            return resolved("wait_for_element", self._no_tab("wait_for_element", started))
        future = BrowserFuture("wait_for_element", self)
        query = json.dumps({
            "role": role, "name_contains": name_contains, "text_contains": text_contains,
        })
        timer = QTimer(self)
        timer.setInterval(max(20, poll_ms))

        def poll() -> None:
            if future.done:
                timer.stop()
                return

            def on_probe(raw: Any) -> None:
                if future.done:
                    return
                if isinstance(raw, dict) and raw.get("matches", 0) > 0:
                    timer.stop()
                    self._finish(future, self._success(
                        "wait_for_element", tab, started,
                        data={"matches": raw.get("matches", 0), "sample": raw.get("sample")}))

            self._call_page(tab, f"window.__pb.probe({query})", on_probe)

        timer.timeout.connect(poll)
        timer.start()
        poll()

        def on_timeout() -> ActionResult:
            timer.stop()
            return self._failure(
                "wait_for_element", tab, started, ErrorCode.TIMEOUT,
                "No matching element appeared before the timeout.")

        future.set_timeout(timeout_ms, on_timeout)
        return future

    # -- public: sensitivity preview --------------------------------------
    def describe_action(
        self,
        action: str,
        ref: str | None = None,
        text: str = "",
        url: str = "",
        tab_id: int | None = None,
    ) -> dict[str, Any]:
        """Judge how consequential an action would be, WITHOUT performing it.

        This is the hook a future agent uses to decide whether to ask the user
        first. It is purely advisory - nothing in this codebase blocks an
        action on the strength of it, and adding that policy is Phase 2 work.
        """
        element = self._known_element(ref, tab_id) if ref else None
        payload = element.to_dict() if element else None
        if action == "click":
            assessment = safety.classify_click(payload)
        elif action in ("type_text", "type"):
            assessment = safety.classify_type(payload, text)
        elif action == "submit":
            fields = self._fields_of_form(payload, tab_id) if payload else []
            assessment = safety.classify_submit(payload, fields)
        elif action == "navigate":
            assessment = safety.classify_navigate(url)
        elif action in ("set_checked", "select_option"):
            # Changing a control writes into the page, same as typing does.
            assessment = safety.classify_type(payload, "")
        else:
            assessment = safety.classify_read()
        return {
            "action": action,
            "ref": ref,
            "target": payload,
            **assessment.to_dict(),
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    # -- frames -----------------------------------------------------------
    #
    # A page is not one document. Anything inside an <iframe> is a separate
    # document with its own DOM, and until now the page representation stopped
    # at the frame boundary - which meant embedded logins, payment forms,
    # comment widgets and video players were invisible to automation.
    #
    # Qt 6.8 added QWebEngineFrame, so each frame can be scripted directly.
    # Our automation script already runs in every frame (setRunsOnSubFrames),
    # in the isolated world, so nothing new is injected: this only routes calls
    # to the right frame.
    #
    # Note this reaches cross-origin frames as well as same-origin ones. It is
    # not a same-origin-policy bypass: the engine runs our script inside each
    # frame's own context, exactly as it already does for the main document,
    # and no page script gains any access it did not have. Every frame's origin
    # is reported alongside its content so the model can tell third-party
    # content apart - see get_page_structure.

    def _frames(self, tab: BrowserTab) -> list[tuple[str, Any]]:
        """Every frame worth capturing: [("", main), ("1", child), ...].

        Depth-first, so the order matches reading order on the page. Bounded by
        MAX_FRAME_DEPTH and MAX_FRAMES; an invalid frame (one that navigated
        away mid-walk) is skipped rather than raising.
        """
        try:
            main = tab.page.mainFrame()
        except Exception:  # noqa: BLE001 - older Qt without the frame API
            return [("", None)]
        if main is None:
            return [("", None)]

        found: list[tuple[str, Any]] = [("", main)]
        counter = [0]

        def walk(frame, depth: int) -> None:
            if depth >= MAX_FRAME_DEPTH or len(found) > MAX_FRAMES:
                return
            try:
                children = list(frame.children())
            except Exception:  # noqa: BLE001
                return
            for child in children:
                if len(found) > MAX_FRAMES:
                    return
                try:
                    if not child.isValid():
                        continue
                except Exception:  # noqa: BLE001
                    continue
                counter[0] += 1
                found.append((str(counter[0]), child))
                walk(child, depth + 1)

        walk(main, 0)
        return found

    def _frame_for(self, tab: BrowserTab, tag: str):
        """The frame a reference belongs to, or None if it is gone."""
        if not tag:
            return None                      # main document: the ordinary path
        for found_tag, frame in self._frames(tab):
            if found_tag == tag:
                return frame
        return None

    def _call_page(self, tab: BrowserTab, expression: str,
                   callback: Callable[[Any], None], frame=None) -> None:
        """Evaluate one of OUR expressions in the isolated world.

        With ``frame`` the expression runs inside that frame's document instead
        of the main one; the isolated world is the same either way.

        Private on purpose: callers cannot reach this, cannot pass an
        expression to it, and cannot influence what it runs. Every call site
        below builds the expression from a fixed template plus JSON-encoded
        arguments.

        The result is marshalled as JSON rather than relying on Qt's
        JavaScript-to-Python object conversion, which returns an empty string
        for a plain JS object on this Qt build. Serialising explicitly also
        keeps nested structures intact regardless of Qt version.
        """
        wrapped = (
            "(function(){try{"
            "if(!window.__pb){return JSON.stringify({__error:'not_ready'});}"
            f"return JSON.stringify({expression});"
            "}catch(e){return JSON.stringify({__error:String(e&&e.message||e)});}})()"
        )

        def on_raw(raw: Any) -> None:
            if not raw:
                callback(None)
                return
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                callback(None)
                return
            callback(None if isinstance(parsed, dict) and "__error" in parsed else parsed)

        if frame is None:
            tab.run_isolated_javascript(wrapped, on_raw)
            return
        try:
            from PySide6.QtWebEngineCore import QWebEngineScript

            frame.runJavaScript(
                wrapped, QWebEngineScript.ScriptWorldId.ApplicationWorld, on_raw)
        except Exception:  # noqa: BLE001 - a frame that vanished mid-call
            callback(None)

    def _invalidate(self, tab: BrowserTab) -> None:
        self._structures.pop(self._id_of(tab), None)

    def _known_element(self, ref: str | None, tab_id: int | None) -> PageElement | None:
        if not ref:
            return None
        tab = self._tab_for(tab_id)
        if tab is None:
            return None
        structure = self._structures.get(self._id_of(tab))
        return structure.by_ref(ref) if structure else None

    def _fields_of_form(self, form_payload: dict[str, Any], tab_id: int | None) -> list[dict[str, Any]]:
        tab = self._tab_for(tab_id)
        structure = self._structures.get(self._id_of(tab)) if tab else None
        if structure is None:
            return []
        index = form_payload.get("form")
        if index is None:
            return []
        return [e.to_dict() for e in structure.elements if e.form == index]

    # -- result construction ---------------------------------------------
    def _page_state(self, tab: BrowserTab | None) -> PageState:
        if tab is None:
            return PageState()
        error = tab.last_error
        return PageState(
            url=tab.url().toString(),
            title=tab.title(),
            loading=tab.is_loading,
            can_go_back=tab.can_go_back(),
            can_go_forward=tab.can_go_forward(),
            tab_id=self._id_of(tab),
            load_error=None if error is None else {
                "category": error.category, "message": error.message, "code": error.code,
            },
        )

    def _success(
        self,
        action: str,
        tab: BrowserTab | None,
        started: float,
        *,
        target: ElementRef | None = None,
        effects: Effects | None = None,
        data: dict[str, Any] | None = None,
        sensitivity: dict[str, Any] | None = None,
    ) -> ActionResult:
        result = ActionResult(
            ok=True, action=action, target=target,
            effects=effects or Effects(),
            page=self._page_state(tab),
            sensitivity=sensitivity or {},
            data=data or {},
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        self.action_completed.emit(result)
        return result

    def _failure(
        self,
        action: str,
        tab: BrowserTab | None,
        started: float,
        code: str,
        message: str,
        *,
        target: ElementRef | None = None,
        recoverable: bool | None = None,
        detail: str = "",
    ) -> ActionResult:
        if recoverable is None:
            recoverable = code.startswith("STALE_") or code == ErrorCode.UNKNOWN_REF
        result = ActionResult(
            ok=False, action=action, target=target,
            error=ActionError(code=code, message=message, recoverable=recoverable, detail=detail),
            page=self._page_state(tab),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        self.action_completed.emit(result)
        return result

    def _no_tab(self, action: str, started: float) -> ActionResult:
        return self._failure(action, None, started, ErrorCode.NO_TAB, "No tab is open.")

    def _finish(self, future: BrowserFuture, result: ActionResult) -> None:
        future.set_result(result)

    def _structure_from(self, raw: dict[str, Any], tab: BrowserTab,
                        frames: list[tuple[str, dict]] | None = None) -> PageStructure:
        known = {f.name for f in PageElement.__dataclass_fields__.values()}  # type: ignore[attr-defined]

        def build(item: dict, tag: str = "", origin: str = "") -> PageElement:
            fields = {k: v for k, v in item.items() if k in known}
            if tag:
                fields["frame"] = int(tag)
                if origin:
                    fields["frame_origin"] = origin
            return PageElement(**fields)

        elements = [build(item) for item in raw.get("elements", [])]
        page_origin = raw.get("origin", "")
        headings = [Heading(level=h.get("level", 2), text=h.get("text", ""))
                    for h in raw.get("headings", [])]
        text_parts = [raw.get("text", "")]
        frame_summary: list[dict[str, Any]] = []
        truncated = raw.get("elements_truncated", False)

        for tag, frame_raw in frames or []:
            origin = frame_raw.get("origin", "")
            frame_elements = frame_raw.get("elements", [])
            if not frame_elements and not (frame_raw.get("text") or "").strip():
                continue                     # an empty frame is noise
            elements.extend(build(item, tag, origin) for item in frame_elements)
            headings.extend(
                Heading(level=h.get("level", 2), text=h.get("text", ""))
                for h in frame_raw.get("headings", []))
            frame_text = (frame_raw.get("text") or "").strip()
            if frame_text:
                # Labelled, not merged silently: text from an embedded document
                # is not the page's own words, and a caller reading a summary
                # should be able to see where each part came from.
                text_parts.append(f"\n[frame {tag} - {origin or frame_raw.get('url', '')}]\n"
                                  f"{frame_text}")
            truncated = truncated or frame_raw.get("elements_truncated", False)
            frame_summary.append({
                "index": int(tag),
                "url": frame_raw.get("url", ""),
                "origin": origin,
                "same_origin": bool(origin) and origin == page_origin,
                "element_count": len(frame_elements),
            })

        return PageStructure(
            frames=frame_summary,
            url=raw.get("url", ""),
            title=raw.get("title", ""),
            lang=raw.get("lang", ""),
            snapshot_id=raw.get("snapshot_id", ""),
            doc_id=raw.get("doc_id", ""),
            dom_revision=raw.get("dom_revision", 0),
            headings=headings,
            forms=[PageForm(**f) for f in raw.get("forms", [])],
            elements=elements,
            element_count=len(elements),
            elements_truncated=truncated,
            text="".join(text_parts),
            text_truncated=raw.get("text_truncated", False),
            scroll_y=raw.get("scroll_y", 0),
            scroll_height=raw.get("scroll_height", 0),
            viewport_height=raw.get("viewport_height", 0),
            viewport_width=raw.get("viewport_width", 0),
            at_bottom=raw.get("at_bottom", False),
            tab_id=self._id_of(tab),
        )

    # -- navigation plumbing ----------------------------------------------
    def _navigate_into(
        self,
        tab: BrowserTab,
        url: str,
        future: BrowserFuture,
        action: str,
        started: float,
        extra_effects: dict[str, Any] | None = None,
    ) -> None:
        before = tab.url().toString()
        self._invalidate(tab)
        if not tab.navigate(url):
            self._finish(future, self._failure(
                action, tab, started, ErrorCode.INVALID_URL,
                f"'{url}' is not a valid web address."))
            return
        self._await_load(tab, future, action, started, before, extra_effects=extra_effects)

    def _await_load(
        self,
        tab: BrowserTab,
        future: BrowserFuture,
        action: str,
        started: float,
        url_before: str,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        extra_effects: dict[str, Any] | None = None,
    ) -> None:
        """Resolve ``future`` when this tab's load finishes."""

        def on_finished(ok: bool) -> None:
            try:
                tab.load_finished.disconnect(on_finished)
            except (RuntimeError, TypeError):
                pass
            if future.done:
                return
            effects = Effects(
                navigated=tab.url().toString() != url_before,
                url_before=url_before,
                url_after=tab.url().toString(),
                **(extra_effects or {}),
            )
            if ok:
                self._finish(future, self._success(action, tab, started, effects=effects))
                return
            error = tab.last_error
            self._finish(future, self._failure(
                action, tab, started, ErrorCode.LOAD_FAILED,
                error.message if error else "The page could not be loaded.",
                detail=error.technical if error else ""))

        tab.load_finished.connect(on_finished)

        def on_timeout() -> ActionResult:
            try:
                tab.load_finished.disconnect(on_finished)
            except (RuntimeError, TypeError):
                pass
            return self._failure(action, tab, started, ErrorCode.TIMEOUT,
                                 "The page did not finish loading in time.")

        future.set_timeout(timeout_ms, on_timeout)

    def _await_navigation_or_url_change(
        self,
        tab: BrowserTab,
        future: BrowserFuture,
        action: str,
        started: float,
        url_before: str,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        """Resolve on either a finished load or a URL change.

        Back/forward restored from the back-forward cache emits no load
        signals, so waiting only on loadFinished would hang. A URL change plus
        a short settle is enough evidence that the move happened.
        """
        settled = QTimer(self)
        settled.setSingleShot(True)
        settled.setInterval(SETTLE_QUIET_MS)

        def resolve_now() -> None:
            if future.done:
                return
            for signal, slot in ((tab.load_finished, on_finished), (tab.url_changed, on_url)):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
            self._finish(future, self._success(action, tab, started, effects=Effects(
                navigated=tab.url().toString() != url_before,
                url_before=url_before,
                url_after=tab.url().toString(),
            )))

        settled.timeout.connect(resolve_now)

        def on_finished(_ok: bool) -> None:
            resolve_now()

        def on_url(_url: QUrl) -> None:
            settled.start()

        tab.load_finished.connect(on_finished)
        tab.url_changed.connect(on_url)

        def on_timeout() -> ActionResult:
            for signal, slot in ((tab.load_finished, on_finished), (tab.url_changed, on_url)):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
            return self._failure(action, tab, started, ErrorCode.TIMEOUT,
                                 "The page did not change in time.")

        future.set_timeout(timeout_ms, on_timeout)

    # -- page actions ------------------------------------------------------
    def _page_action(
        self,
        action: str,
        request: dict[str, Any],
        ref: str | None,
        tab_id: int | None,
        *,
        watch_effects: bool = True,
        typed_text: str = "",
    ) -> BrowserFuture:
        started = time.monotonic()
        tab = self._tab_for(tab_id)
        if tab is None:
            return resolved(action, self._no_tab(action, started))
        if ref is not None and not _REF_PATTERN.match(ref):
            # Rejected before touching the page: a malformed reference is a
            # caller bug, not a page state.
            return resolved(action, self._failure(
                action, tab, started, ErrorCode.INVALID_REF,
                "That element reference is not in a valid format. "
                "References come from get_page_structure() and look like 's3:e12'.",
                recoverable=False))

        element = self._known_element(ref, tab_id)
        # Results carry the assessment only; describe_action() additionally
        # echoes the action and its target, which a result already reports.
        preview = (
            self.describe_action(action, ref=ref, text=typed_text, tab_id=tab_id)
            if ref else {**safety.classify_read().to_dict()}
        )
        sensitivity = {key: preview[key] for key in
                       ("level", "reasons", "requires_confirmation") if key in preview}

        future = BrowserFuture(action, self)
        url_before = tab.url().toString()
        payload = json.dumps(request)

        # A target=_blank link or window.open() makes Chromium call
        # createWindow() synchronously inside element.click(), which means the
        # signal fires before our JavaScript callback returns. The watcher has
        # to be connected before the action, not after it.
        watch = {"spawned": False, "tab_count_before": self._tabs.count()}

        def on_new_tab(_new_tab: object) -> None:
            watch["spawned"] = True

        tab.new_tab_requested.connect(on_new_tab)

        def release_watch(_result: object) -> None:
            try:
                tab.new_tab_requested.disconnect(on_new_tab)
            except (RuntimeError, TypeError):
                pass

        future.then(release_watch)

        def on_result(raw: Any) -> None:
            if not isinstance(raw, dict):
                self._finish(future, self._failure(
                    action, tab, started, ErrorCode.SCRIPT_FAILED,
                    "The page could not be reached for this action. It may still be loading."))
                return
            status = raw.get("status", "")
            target = ElementRef(
                ref=ref or "",
                role=(raw.get("target") or {}).get("role", element.role if element else ""),
                name=(raw.get("target") or {}).get("name", element.name if element else ""),
                tag=(raw.get("target") or {}).get("tag", element.tag if element else ""),
            ) if ref else None

            if status != "ok":
                page_error = error_from_page_status(status)
                self._finish(future, self._failure(
                    action, tab, started, page_error.code, page_error.message,
                    target=target, recoverable=page_error.recoverable))
                return

            if not watch_effects:
                effects = Effects(url_before=url_before, url_after=tab.url().toString())
                if "scroll_before" in raw:
                    effects = Effects(
                        url_before=url_before, url_after=tab.url().toString(),
                        scroll_before=raw.get("scroll_before"), scroll_after=raw.get("scroll_after"),
                    )
                data = {"element": raw["element"]} if "element" in raw else {}
                self._finish(future, self._success(
                    action, tab, started, target=target, effects=effects,
                    data=data,
                    sensitivity=sensitivity if isinstance(sensitivity, dict) else {}))
                return

            self._settle(tab, future, action, started, target, url_before,
                         raw.get("dom_revision", 0), sensitivity, watch)

        # Run the action in the document the reference came from. A reference
        # to an element inside an iframe resolves only in that frame - the main
        # document has never heard of it.
        frame = self._frame_for(tab, _frame_tag(ref)) if ref else None
        if ref and _frame_tag(ref) and frame is None:
            return resolved(action, self._failure(
                action, tab, started, ErrorCode.STALE_DOCUMENT,
                "The frame that element was in is no longer on the page.",
                recoverable=True))
        self._call_page(tab, f"window.__pb.act({payload})", on_result, frame=frame)
        future.set_timeout(DEFAULT_TIMEOUT_MS, lambda: self._failure(
            action, tab, started, ErrorCode.TIMEOUT, "The action did not complete in time."))
        return future

    def _settle(
        self,
        tab: BrowserTab,
        future: BrowserFuture,
        action: str,
        started: float,
        target: ElementRef | None,
        url_before: str,
        revision_before: int,
        sensitivity: dict[str, Any],
        watch: dict[str, Any],
    ) -> None:
        """Watch what an action actually caused, then resolve.

        Three outcomes have to be told apart, and none of them is knowable at
        the moment the click returns:

        * it started a navigation - wait for the load to finish;
        * it changed the DOM in place - detected via the mutation counter;
        * it did nothing observable.

        So we watch for up to SETTLE_MAX_MS, resolving as soon as a load
        completes or the DOM goes quiet.
        """
        tab_id = self._id_of(tab)
        navigated = {"value": False}
        deadline = QTimer(self)
        deadline.setSingleShot(True)
        quiet = QTimer(self)
        quiet.setSingleShot(True)
        quiet.setInterval(SETTLE_QUIET_MS)

        def cleanup() -> None:
            for timer in (deadline, quiet):
                timer.stop()
            for signal, slot in ((tab.load_started, on_load_started),
                                 (tab.load_finished, on_load_finished)):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass

        def report() -> None:
            if future.done:
                return
            cleanup()

            def with_status(raw: Any) -> None:
                revision_after = (raw.get("dom_revision", revision_before)
                                  if isinstance(raw, dict) else revision_before)
                url_after = tab.url().toString()
                # A target=_blank link or window.open() adds a tab. The tab
                # can be adopted a moment after the click settles, so trust the
                # engine's own "new window wanted" signal as well as the count.
                opened_tab = watch["spawned"] or self._tabs.count() > watch["tab_count_before"]
                effects = Effects(
                    navigated=navigated["value"] or url_after != url_before,
                    dom_changed=revision_after != revision_before or navigated["value"],
                    url_before=url_before,
                    url_after=url_after,
                    opened_tab=opened_tab,
                    new_tab_id=self._newest_tab_id() if opened_tab else None,
                )
                # Any of these invalidates every reference from the old page,
                # so drop the cached structure rather than let a caller reuse it.
                if effects.navigated or opened_tab:
                    self._structures.pop(tab_id, None)
                self._finish(future, self._success(
                    action, tab, started, target=target, effects=effects,
                    sensitivity=sensitivity if isinstance(sensitivity, dict) else {}))

            self._call_page(tab, "window.__pb.status()", with_status)

        def on_load_started() -> None:
            navigated["value"] = True
            quiet.stop()

        def on_load_finished(_ok: bool) -> None:
            report()

        tab.load_started.connect(on_load_started)
        tab.load_finished.connect(on_load_finished)
        deadline.timeout.connect(report)
        quiet.timeout.connect(report)
        deadline.start(SETTLE_MAX_MS)
        quiet.start()

    def _newest_tab_id(self) -> int | None:
        tabs = self._tabs.tabs()
        return self._register(tabs[-1]) if tabs else None
