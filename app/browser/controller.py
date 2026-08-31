"""A programmatic control surface for the browser.

This is a plain browser API, not an AI system. It exists because "navigate,
read the page, click something, type something, scroll" is exactly the vocabulary
that the UI, the tests, and (eventually) the Phase 2 agent all need, and it is
much better to have one audited implementation of it than three.

Two properties matter:

* **It is the only supported way to drive the browser programmatically.** A
  caller gets `navigate()` and `click(ref)`; it does not get to reach into
  QWebEngineView, poke at QTabWidget indices, or synthesise Qt events into
  arbitrary widgets.
* **Elements are addressed by opaque handles**, not by CSS selectors supplied
  by the caller. `page_structure()` hands back `e0, e1, e2…`; `click("e7")`
  acts on one of them. A handle that no longer exists is a clean, catchable
  error rather than a click on the wrong thing.

Everything is synchronous-looking but Qt is asynchronous underneath, so the
JavaScript-backed calls take a callback. Phase 2 will wrap these in its own
turn loop; nothing here knows that Phase 2 exists.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from PySide6.QtCore import QObject, QUrl, Signal

from app.browser.tab import BrowserTab
from app.browser.tab_manager import TabManager

# Injected into the page to build a structural summary. Kept deliberately small:
# it reports role, accessible name and geometry for interactive elements only,
# and tags each with an index that becomes the element handle.
_STRUCTURE_JS = r"""
(function() {
  const SEL = 'a[href], button, input, textarea, select, [role="button"],' +
              '[role="link"], [role="textbox"], [role="checkbox"], [onclick], [contenteditable="true"]';
  const out = [];
  const nodes = document.querySelectorAll(SEL);
  const limit = Math.min(nodes.length, %(limit)d);
  for (let i = 0; i < limit; i++) {
    const el = nodes[i];
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const visible = rect.width > 0 && rect.height > 0 &&
                    style.visibility !== 'hidden' && style.display !== 'none';
    let role = el.getAttribute('role') || el.tagName.toLowerCase();
    if (role === 'input') role = (el.getAttribute('type') || 'text');
    const name = (el.getAttribute('aria-label') || el.getAttribute('title') ||
                  el.getAttribute('placeholder') || el.innerText ||
                  el.getAttribute('alt') || el.value || '').trim().slice(0, 200);
    // Stamp the handle onto the node so later calls can find this exact element
    // again without relying on a selector we would have to re-derive.
    el.setAttribute('data-pybrowser-ref', 'e' + i);
    out.push({
      ref: 'e' + i, role: role, name: name,
      value: (el.value || '').toString().slice(0, 200),
      enabled: !el.disabled, visible: visible,
      href: el.getAttribute('href') || ''
    });
  }
  return JSON.stringify({
    url: location.href,
    title: document.title,
    text: (document.body ? document.body.innerText : '').slice(0, %(text_limit)d),
    scrollY: Math.round(window.scrollY),
    scrollHeight: Math.round(document.documentElement.scrollHeight),
    viewportHeight: Math.round(window.innerHeight),
    elementCount: nodes.length,
    elements: out
  });
})()
"""

_FIND_BY_REF = "document.querySelector('[data-pybrowser-ref=\"%s\"]')"


@dataclass(frozen=True)
class PageElement:
    ref: str
    role: str
    name: str
    value: str = ""
    enabled: bool = True
    visible: bool = True
    href: str = ""


@dataclass(frozen=True)
class PageStructure:
    """A structural snapshot of the current page.

    Built from the DOM and ARIA roles rather than from a screenshot: it is
    cheaper, it survives theme and window-size changes, and it gives callers
    exact handles to act on.
    """

    url: str
    title: str
    text: str = ""
    scroll_y: int = 0
    scroll_height: int = 0
    viewport_height: int = 0
    element_count: int = 0
    elements: list[PageElement] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def find(self, role: str = "", name_contains: str = "") -> list[PageElement]:
        """Convenience lookup used by callers that know what they want."""
        needle = name_contains.lower()
        return [
            element
            for element in self.elements
            if (not role or element.role == role)
            and (not needle or needle in element.name.lower())
        ]


class ScrollDirection:
    UP = "up"
    DOWN = "down"
    TOP = "top"
    BOTTOM = "bottom"


class BrowserController(QObject):
    """The supported programmatic interface to a browser window.

    Wraps a TabManager. Every method operates on the *current* tab unless a tab
    is passed explicitly.
    """

    action_performed = Signal(str, str)  # action name, detail

    def __init__(self, tabs: TabManager, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tabs = tabs

    # -- tab-level operations -------------------------------------------
    def open_tab(self, url: str | None = None, *, background: bool = False) -> BrowserTab:
        tab = self._tabs.new_tab(url, background=background)
        self.action_performed.emit("open_tab", url or "")
        return tab

    def close_tab(self, index: int | None = None) -> bool:
        target = self._tabs.currentIndex() if index is None else index
        if not 0 <= target < self._tabs.count():
            return False
        self._tabs.close_tab(target)
        self.action_performed.emit("close_tab", str(target))
        return True

    def select_tab(self, index: int) -> bool:
        if not 0 <= index < self._tabs.count():
            return False
        self._tabs.setCurrentIndex(index)
        return True

    def tab_count(self) -> int:
        return self._tabs.count()

    def list_tabs(self) -> list[dict[str, Any]]:
        return [
            {"index": i, "title": tab.title(), "url": tab.url().toString(),
             "active": tab is self._tabs.current_tab()}
            for i, tab in enumerate(self._tabs.tabs())
        ]

    def current_tab(self) -> BrowserTab | None:
        return self._tabs.current_tab()

    # -- navigation ------------------------------------------------------
    def navigate(self, url: str, tab: BrowserTab | None = None) -> bool:
        target = tab or self._require_tab()
        ok = target.navigate(QUrl(url) if isinstance(url, str) else url)
        self.action_performed.emit("navigate", url)
        return ok

    def go_back(self, tab: BrowserTab | None = None) -> bool:
        target = tab or self._require_tab()
        if not target.can_go_back():
            return False
        target.back()
        self.action_performed.emit("go_back", "")
        return True

    def go_forward(self, tab: BrowserTab | None = None) -> bool:
        target = tab or self._require_tab()
        if not target.can_go_forward():
            return False
        target.forward()
        self.action_performed.emit("go_forward", "")
        return True

    def reload(self, tab: BrowserTab | None = None) -> None:
        (tab or self._require_tab()).reload()
        self.action_performed.emit("reload", "")

    def stop(self, tab: BrowserTab | None = None) -> None:
        (tab or self._require_tab()).stop()

    # -- reading the page ------------------------------------------------
    def get_current_page(self, tab: BrowserTab | None = None) -> dict[str, Any]:
        """Cheap, synchronous summary: URL, title, loading state, last error."""
        target = tab or self._require_tab()
        error = target.last_error
        return {
            "url": target.url().toString(),
            "title": target.title(),
            "loading": target.is_loading,
            "can_go_back": target.can_go_back(),
            "can_go_forward": target.can_go_forward(),
            "error": None if error is None else {
                "category": error.category,
                "message": error.message,
                "technical": error.technical,
            },
        }

    def get_page_structure(
        self,
        callback: Callable[[PageStructure], None],
        tab: BrowserTab | None = None,
        *,
        max_elements: int = 200,
        max_text: int = 20000,
    ) -> None:
        """Asynchronously deliver a PageStructure for the current page."""
        target = tab or self._require_tab()
        script = _STRUCTURE_JS % {"limit": max_elements, "text_limit": max_text}

        def on_result(raw: Any) -> None:
            callback(self._parse_structure(raw, target))

        target.run_javascript(script, on_result)

    @staticmethod
    def _parse_structure(raw: Any, tab: BrowserTab) -> PageStructure:
        if not raw:
            # A page that refuses script execution (or an error page) still
            # deserves a valid, empty structure rather than an exception.
            return PageStructure(url=tab.url().toString(), title=tab.title())
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return PageStructure(url=tab.url().toString(), title=tab.title())
        return PageStructure(
            url=data.get("url", ""),
            title=data.get("title", ""),
            text=data.get("text", ""),
            scroll_y=data.get("scrollY", 0),
            scroll_height=data.get("scrollHeight", 0),
            viewport_height=data.get("viewportHeight", 0),
            element_count=data.get("elementCount", 0),
            elements=[PageElement(**e) for e in data.get("elements", [])],
        )

    def get_text(self, callback: Callable[[str], None], tab: BrowserTab | None = None) -> None:
        (tab or self._require_tab()).page.toPlainText(callback)

    # -- acting on the page ----------------------------------------------
    def click(
        self,
        ref: str,
        callback: Callable[[bool], None] | None = None,
        tab: BrowserTab | None = None,
    ) -> None:
        """Click the element previously reported as ``ref``.

        Note for callers that care about session history: this is a
        script-initiated click with no user activation behind it. Chromium's
        History Manipulation Intervention marks the resulting history entry as
        skippable, so a later ``go_back()`` can step over it rather than
        landing on the page the click came from. That is Chromium behaving as
        designed, not a defect - but it means a caller driving a multi-step
        task should track its own trail rather than assuming one back() undoes
        one click().
        """
        script = f"""
        (function() {{
          const el = {_FIND_BY_REF % self._escape(ref)};
          if (!el) return false;
          el.scrollIntoView({{block: 'center'}});
          el.click();
          return true;
        }})()
        """
        self._run(script, callback, tab, "click", ref)

    def type_text(
        self,
        ref: str,
        text: str,
        submit: bool = False,
        callback: Callable[[bool], None] | None = None,
        tab: BrowserTab | None = None,
    ) -> None:
        """Type into an input/textarea/contenteditable addressed by ``ref``.

        Dispatches real input/change events so frameworks that listen for them
        (React and friends) actually see the value.
        """
        script = f"""
        (function() {{
          const el = {_FIND_BY_REF % self._escape(ref)};
          if (!el) return false;
          el.focus();
          const value = {json.dumps(text)};
          if (el.isContentEditable) {{ el.textContent = value; }}
          else {{ el.value = value; }}
          el.dispatchEvent(new Event('input', {{bubbles: true}}));
          el.dispatchEvent(new Event('change', {{bubbles: true}}));
          if ({json.dumps(bool(submit))}) {{
            if (el.form) {{ el.form.requestSubmit ? el.form.requestSubmit() : el.form.submit(); }}
            else {{ el.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', bubbles: true}})); }}
          }}
          return true;
        }})()
        """
        self._run(script, callback, tab, "type_text", ref)

    def scroll(
        self,
        direction: str = ScrollDirection.DOWN,
        amount: int | None = None,
        callback: Callable[[bool], None] | None = None,
        tab: BrowserTab | None = None,
    ) -> None:
        """Scroll the viewport. ``amount`` defaults to about one screen."""
        if direction == ScrollDirection.TOP:
            body = "window.scrollTo(0, 0);"
        elif direction == ScrollDirection.BOTTOM:
            body = "window.scrollTo(0, document.documentElement.scrollHeight);"
        else:
            step = amount if amount is not None else 0
            sign = -1 if direction == ScrollDirection.UP else 1
            delta = f"{sign} * ({step} || Math.round(window.innerHeight * 0.85))"
            body = f"window.scrollBy(0, {delta});"
        self._run(f"(function() {{ {body} return true; }})()", callback, tab,
                  "scroll", direction)

    # -- internals -------------------------------------------------------
    def _run(self, script, callback, tab, action, detail) -> None:
        target = tab or self._require_tab()
        self.action_performed.emit(action, detail)
        if callback is None:
            target.run_javascript(script)
        else:
            target.run_javascript(script, lambda result: callback(bool(result)))

    def _require_tab(self) -> BrowserTab:
        tab = self._tabs.current_tab()
        if tab is None:
            raise RuntimeError("No tab is open")
        return tab

    @staticmethod
    def _escape(ref: str) -> str:
        # Handles are generated by us (e<digits>), but never interpolate an
        # unvalidated string into JavaScript.
        if not ref.replace("e", "", 1).isdigit() or not ref.startswith("e"):
            raise ValueError(f"invalid element handle: {ref!r}")
        return ref
