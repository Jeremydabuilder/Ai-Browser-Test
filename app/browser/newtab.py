"""PyBrowser's own new-tab page, served from a custom URL scheme.

Why a custom scheme rather than a Qt widget
-------------------------------------------
The new-tab page could have been a `QWidget` swapped into the tab. Serving it
as a real page at `pybrowser://newtab/` instead means it is a genuine
navigation target: it has a URL, it participates in back/forward, `TabManager`
and `BrowserTab` need no notion of "this tab is special", and the automation
API sees it like any other page. Nothing in the browser had to learn a new
concept - which is the whole reason for the choice.

It is served from memory, so it appears instantly and works with no network,
no search provider and no API key.

How the page talks back to the browser
--------------------------------------
The page is HTML in a web engine; it cannot call Python. It does not need to.
Clicking or submitting navigates to an action URL such as
`pybrowser://newtab/search?q=...`, and `BrowserPage.acceptNavigationRequest`
intercepts anything under `/action/`, refuses the navigation, and emits it as a
signal for the window to act on.

That keeps one rule intact: **the page never decides what happens.** It states
an intention; Python decides. In particular the URL-or-search decision stays in
`app/utils/urls.py` rather than being reimplemented in JavaScript, so the
address bar and the new-tab box cannot drift apart.

Untrusted content
-----------------
Page titles come from arbitrary websites and are rendered on a privileged
internal page, so they are injected as JSON (with `<` escaped, defeating a
`</script>` break-out) and written to the DOM with `textContent`, never
`innerHTML`. A page titled `<img onerror=...>` is displayed, not executed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from PySide6.QtCore import QUrl

from app.browser.internal import (
    ACTION_PREFIX,
    SCHEME,
    is_internal,
    parse_action,
    register_scheme,
    route,
    scheme_owner,
)

HOST = "newtab"
NEW_TAB_URL = "pybrowser://newtab/"

# Re-exported so existing callers and tests keep working: the scheme plumbing
# moved to app/browser/internal.py when a second internal page arrived, and
# this page's own content did not change with it.
__all__ = [
    "ACTION_PREFIX", "NEW_TAB_URL", "SCHEME", "NewTabData", "collect",
    "is_new_tab", "parse_action", "register_scheme", "render", "scheme_owner",
]


def is_new_tab(url: QUrl | str) -> bool:
    target = QUrl(url) if isinstance(url, str) else url
    return is_internal(target) and target.host() == HOST


# ---------------------------------------------------------------------------
# The data the page shows
# ---------------------------------------------------------------------------


@dataclass
class NewTabData:
    """What the page displays. Plain data, so it can be built and tested
    without a browser anywhere in sight."""

    recent: list[dict[str, str]] = field(default_factory=list)
    bookmarks: list[dict[str, str]] = field(default_factory=list)
    #: Missions started recently - the mission-first entry point on the new
    #: tab page, alongside the ordinary Recent/Bookmarks columns. Each is
    #: {"id", "title", "status", "progress"}.
    recent_missions: list[dict[str, object]] = field(default_factory=list)
    agent_available: bool = False
    #: The AI master switch. Off = a plain new tab (search + your sites), no AI wording, no AI Mode.
    ai_enabled: bool = False
    #: Show the first-launch explainer card. True only before the user has
    #: started a first Mission and before they have dismissed it - see
    #: main_window._new_tab_data. Never shown again once either happens.
    show_onboarding: bool = False

    def to_json(self) -> str:
        payload = json.dumps({
            "recent": self.recent,
            "bookmarks": self.bookmarks,
            "aiEnabled": self.ai_enabled,
        }, ensure_ascii=False)
        # A page title containing "</script>" would otherwise end the block and
        # let arbitrary markup follow. Escaping "<" makes that impossible while
        # remaining valid JSON.
        return payload.replace("<", "\\u003c")


def collect(history=None, bookmarks=None, missions=None, *,
            agent_available: bool = False, limit: int = 8,
            show_onboarding: bool = False, ai_enabled: bool = False) -> NewTabData:
    """Gather the page's content from the stores, tolerating failure.

    A new tab must open even if the database is locked, missing or broken, so
    every read is guarded: the page degrades to its empty state instead of
    failing to appear. ``missions`` is a MissionService (or anything with the
    same ``.recent(limit)``); read-only, the same as history and bookmarks.
    """
    data = NewTabData(agent_available=agent_available, show_onboarding=show_onboarding, ai_enabled=ai_enabled)
    try:
        if history is not None:
            seen: set[str] = set()
            for entry in history.recent(limit * 4):
                if entry.url in seen or is_new_tab(entry.url):
                    continue
                seen.add(entry.url)
                data.recent.append({"title": entry.title or entry.url, "url": entry.url})
                if len(data.recent) >= limit:
                    break
    except Exception:  # noqa: BLE001 - an empty section beats no new tab page
        data.recent = []
    try:
        if bookmarks is not None:
            data.bookmarks = [
                {"title": mark.title or mark.url, "url": mark.url}
                for mark in bookmarks.all()[:limit]
            ]
    except Exception:  # noqa: BLE001
        data.bookmarks = []
    try:
        if missions is not None:
            data.recent_missions = [
                {"id": m.id, "title": m.title, "status": m.status, "progress": m.progress}
                for m in missions.recent(5)
            ]
    except Exception:  # noqa: BLE001
        data.recent_missions = []
    return data


# ---------------------------------------------------------------------------
# The scheme handler
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The page itself
# ---------------------------------------------------------------------------


#: Where the page gets its content. A zero-argument callable returning
#: NewTabData; the profile supplies one that reads the stores. Keeping it a
#: callable means this module knows nothing about SQLite, and the tests can
#: hand it a fixed list.
_PROVIDER = NewTabData


def set_provider(provider) -> None:
    global _PROVIDER
    _PROVIDER = provider


def _serve(_url) -> str:
    try:
        data = _PROVIDER()
    except Exception:  # noqa: BLE001
        data = NewTabData()
    return render(data)


route(HOST, _serve)


def render(data: NewTabData, dark: bool | None = None) -> str:
    """The complete page. No external requests: no fonts, no CDN, no analytics.

    Everything is inline, which is what makes it appear instantly and work with
    no network at all - including the mascot, which is embedded rather than
    fetched so the page never waits on a file read to finish drawing.
    """
    if dark is None:
        dark = _browser_is_dark()
    return (_TEMPLATE
            .replace("__THEME__", ' data-theme="dark"' if dark else ' data-theme="light"')
            .replace("__FIELD_CLASS__", " with-ai" if data.ai_enabled else "")
            .replace("__AI_BUTTON__", _AI_BUTTON.strip() if data.ai_enabled else "")
            .replace("__AI_HINT__", " \\u00b7 Alt+Enter for AI Mode" if data.ai_enabled else "")
            .replace("__DATA__", data.to_json()))


def _browser_is_dark() -> bool:
    """Whether the browser's own chrome is dark right now.

    Asked of the same palette the toolbar and tabs use, so the page inside the
    window and the window itself can never disagree.
    """
    try:
        from PySide6.QtWidgets import QApplication

        from app.ui import theme

        return theme.palette_for(QApplication.instance()) is theme.DARK
    except Exception:  # noqa: BLE001 - a theme lookup must not cost a new tab
        return False


def _mascot_markup() -> str:
    """The character for the top of the page, inlined.

    Uses the real artwork when `app/ui/assets/mascot/` has an idle image, and
    the placeholder mark otherwise. Inlined as a data URI rather than linked,
    because a `pybrowser://` sub-resource would need its own handler route for
    no benefit on a page this small.
    """
    try:
        from app.ui.mascot import MascotState, Variant, asset_for

        # The full-body crop: this is where you meet Py, and there is room for
        # a pose. The agent panel takes the bust crop of the same character.
        path = asset_for(MascotState.IDLE, Variant.FULL)
        if path:
            import base64
            import mimetypes

            with open(path, "rb") as handle:
                encoded = base64.b64encode(handle.read()).decode("ascii")
            media = mimetypes.guess_type(path)[0] or "image/svg+xml"
            return (f'<img class="mark" alt="" aria-hidden="true" '
                    f'src="data:{media};base64,{encoded}">')
    except Exception:  # noqa: BLE001 - a missing mascot must not cost a new tab
        pass
    return _PLACEHOLDER_MARK


#: The stand-in mark, until the character exists. Matches app/ui/mascot.py -
#: a fox head, so that even with no artwork at all the page is still Py's.
_PLACEHOLDER_MARK = (
    '<svg class="mark" viewBox="0 0 36 36" fill="none" aria-hidden="true">'
    '<g fill="currentColor" fill-opacity=".12" stroke="currentColor"'
    ' stroke-opacity=".55" stroke-width="1.4" stroke-linejoin="round">'
    '<path d="M11.5 11 7 3.5 14.5 7.5Z"/><path d="M24.5 11 29 3.5 21.5 7.5Z"/>'
    '<path d="M18 8.5c6.2 0 10.5 4.3 10.5 10.2 0 5.6-4.3 9.8-10.5 9.8'
    'S7.5 24.3 7.5 18.7C7.5 12.8 11.8 8.5 18 8.5Z"/></g>'
    '<circle cx="13.6" cy="18" r="1.9" fill="currentColor"/>'
    '<circle cx="22.4" cy="18" r="1.9" fill="currentColor"/>'
    '<ellipse cx="18" cy="23" rx="2.1" ry="1.6" fill="currentColor"/>'
    '<path d="M18 24.6v1.6M15 27.4q3 2 6 0" stroke="currentColor" stroke-width="1.5"'
    ' fill="none" stroke-linecap="round" opacity=".8"/></svg>'
)


_AI_BUTTON = """
      <button id="ai" type="button" title="Ask a question and get a sourced answer (Alt+Enter)">
        <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path d="M10 1.5l1.7 4.6 4.6 1.7-4.6 1.7L10
          14.1 8.3 9.5 3.7 7.8l4.6-1.7zM16 12.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z"/></svg>AI Mode</button>
"""

_TEMPLATE = """<!doctype html>
<html lang="en"__THEME__>
<head>
<meta charset="utf-8">
<title>New Tab</title>
<style>
  :root {
    --bg: #fafafc; --surface: #ffffff; --line: #e4e4ec; --text: #1a1a22; --muted: #6b6b78;
    --accent: #3d5afe; --accent2: #7b3ff2; --accent-soft: #eef0ff;
    --shadow: 0 1px 2px rgba(20,20,40,.05), 0 8px 24px rgba(20,20,40,.06);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #16161b; --surface: #1f1f27; --line: #30303b; --text: #eeeef3; --muted: #9a9aa8;
      --accent: #8c9cff; --accent2: #b98cff; --accent-soft: #272a45;
      --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.35);
    }
  }
  :root[data-theme="dark"] {
    --bg: #16161b; --surface: #1f1f27; --line: #30303b; --text: #eeeef3; --muted: #9a9aa8;
    --accent: #8c9cff; --accent2: #b98cff; --accent-soft: #272a45;
    --shadow: 0 1px 2px rgba(0,0,0,.4), 0 8px 24px rgba(0,0,0,.35);
  }
  * { box-sizing: border-box; }
  html, body { min-height: 100%; }
  html { background: var(--bg); }
  body {
    margin: 0; background: var(--bg); color: var(--text); -webkit-font-smoothing: antialiased;
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
    display: flex; justify-content: center;
  }
  main { width: 100%; max-width: 680px; padding: clamp(48px, 22vh, 190px) 24px 64px; }
  .wordmark {
    text-align: center; font-size: 30px; font-weight: 600; letter-spacing: -.03em; margin: 0 0 28px;
    user-select: none;
  }
  .wordmark span { color: var(--accent); }
  .field { position: relative; }
  #q {
    width: 100%; height: 54px; padding: 0 20px 0 50px; font: inherit; font-size: 16px; color: var(--text);
    background: var(--surface); border: 1px solid var(--line); border-radius: 27px; outline: none;
    box-shadow: var(--shadow); transition: border-color .12s, box-shadow .12s;
  }
  #q::placeholder { color: var(--muted); }
  #q:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft), var(--shadow); }
  .lead { position: absolute; left: 19px; top: 50%; transform: translateY(-50%); color: var(--muted); display: flex; }
  .lead svg { width: 19px; height: 19px; }
  .with-ai #q { padding-right: 124px; }
  #ai {
    position: absolute; right: 8px; top: 50%; transform: translateY(-50%); height: 38px; padding: 0 14px;
    display: none; align-items: center; gap: 6px; cursor: pointer; font: inherit; font-size: 13.5px; font-weight: 600;
    color: var(--accent); background: var(--accent-soft); border: 1px solid transparent; border-radius: 19px;
  }
  .with-ai #ai { display: inline-flex; }
  #ai:hover { border-color: var(--accent); }
  #ai:focus-visible, .tile:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  #ai svg { width: 15px; height: 15px; }
  #hint { height: 22px; margin: 8px 0 0; text-align: center; font-size: 12.5px; color: var(--muted); }
  .sites { display: grid; grid-template-columns: repeat(auto-fill, minmax(92px, 1fr)); gap: 8px 6px; margin-top: 36px; }
  .tile {
    display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 12px 6px 10px; border-radius: 14px;
    color: var(--text); text-decoration: none; min-width: 0;
  }
  .tile:hover { background: var(--accent-soft); }
  .tile .icon {
    width: 46px; height: 46px; border-radius: 50%; display: grid; place-items: center; font-weight: 600; font-size: 18px;
    color: var(--accent); background: var(--surface); border: 1px solid var(--line);
  }
  .tile .name { width: 100%; text-align: center; font-size: 12.5px; color: var(--muted); white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; }
  .empty { margin-top: 36px; text-align: center; font-size: 13px; color: var(--muted); }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
</head>
<body>
<main>
  <h1 class="wordmark">Py<span>Browser</span></h1>
  <form id="f" autocomplete="off">
    <div class="field__FIELD_CLASS__" id="field">
      <span class="lead" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8"
        stroke-linecap="round"><circle cx="8.5" cy="8.5" r="5.5"/><path d="M13 13l4 4"/></svg></span>
      <input id="q" type="text" name="q" placeholder="Search or enter address" aria-label="Search or enter address"
             autofocus spellcheck="false">
      __AI_BUTTON__
    </div>
    <p id="hint" aria-live="polite"></p>
  </form>
  <nav class="sites" id="sites" aria-label="Your sites"></nav>
  <p class="empty" id="empty" hidden>Sites you visit and bookmark will show up here.</p>
</main>
<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  "use strict";
  var data;
  try { data = JSON.parse(document.getElementById("data").textContent); }
  catch (e) { data = { recent: [], bookmarks: [], aiEnabled: false }; }

  function act(name, params) {
    var url = "pybrowser://newtab/action/" + name, parts = [];
    for (var key in params) { if (params[key]) { parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(params[key])); } }
    // The page only states an intention; the browser intercepts this URL, refuses the navigation and decides.
    window.location.href = parts.length ? url + "?" + parts.join("&") : url;
  }
  function host(url) {
    try { var h = new URL(url).hostname; return h.indexOf("www.") === 0 ? h.slice(4) : h; } catch (e) { return ""; }
  }

  var box = document.getElementById("q"), hint = document.getElementById("hint");

  // Bookmarks first, then recent pages; one tile per site.
  var seen = {}, tiles = [];
  (data.bookmarks || []).concat(data.recent || []).forEach(function (item) {
    var h = host(item.url);
    if (!h || seen[h] || tiles.length >= 10) { return; }
    seen[h] = true;
    tiles.push({ title: item.title, url: item.url, host: h });
  });
  var sites = document.getElementById("sites");
  tiles.forEach(function (item) {
    var a = document.createElement("a");
    a.className = "tile";
    a.href = "pybrowser://newtab/action/open?url=" + encodeURIComponent(item.url);
    a.title = item.title + "\\n" + item.url;
    var icon = document.createElement("span");
    icon.className = "icon";
    icon.textContent = (item.host.charAt(0) || "?").toUpperCase();   // textContent: titles come from arbitrary sites
    var name = document.createElement("span");
    name.className = "name";
    name.textContent = item.host;
    a.appendChild(icon); a.appendChild(name);
    sites.appendChild(a);
  });
  document.getElementById("empty").hidden = tiles.length > 0;

  document.getElementById("f").addEventListener("submit", function (event) {
    event.preventDefault();
    var text = box.value.trim();
    if (text) { act("search", { q: text }); }
  });

  function askAi() { if (data.aiEnabled) { act("aimode", { q: box.value.trim() }); } }
  var aiButton = document.getElementById("ai");
  if (aiButton) { aiButton.addEventListener("click", askAi); }
  box.addEventListener("keydown", function (event) {
    if (event.altKey && event.key === "Enter") { event.preventDefault(); askAi(); }
  });

  // A hint, not a decision: Python still decides URL vs. search.
  box.addEventListener("input", function () {
    var text = box.value.trim();
    if (!text) { hint.textContent = ""; return; }
    var looksLikeUrl = /^[a-z][a-z0-9+.-]*:\\/\\//i.test(text) || /^[^\\s]+\\.[a-z]{2,}([/:?#]|$)/i.test(text) ||
                       /^localhost([:/]|$)/i.test(text);
    hint.textContent = (looksLikeUrl ? "Press Enter to go to this address" : "Press Enter to search the web")
                       + (data.aiEnabled && !looksLikeUrl ? "__AI_HINT__" : "");
  });
})();
</script>
</body>
</html>
"""
