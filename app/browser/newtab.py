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

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QUrl
from PySide6.QtWebEngineCore import (
    QWebEngineUrlRequestJob,
    QWebEngineUrlScheme,
    QWebEngineUrlSchemeHandler,
)

SCHEME = "pybrowser"
NEW_TAB_URL = "pybrowser://newtab/"
#: Everything under this path is an instruction to the browser, not a page.
ACTION_PREFIX = "/action/"


def register_scheme() -> None:
    """Register the scheme with Chromium.

    Must run **before** QApplication is constructed - Chromium reads the
    scheme registry once at startup, and a scheme registered later is simply
    unknown. `main.py` calls this first thing.
    """
    if QWebEngineUrlScheme.schemeByName(QByteArray(SCHEME.encode())).name():
        return                       # already registered (e.g. a second window)
    scheme = QWebEngineUrlScheme(SCHEME.encode())
    scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
    scheme.setFlags(
        QWebEngineUrlScheme.Flag.SecureScheme          # not "insecure origin"
        | QWebEngineUrlScheme.Flag.LocalAccessAllowed
        | QWebEngineUrlScheme.Flag.ContentSecurityPolicyIgnored
    )
    QWebEngineUrlScheme.registerScheme(scheme)


#: The handler serving pybrowser://, and the profile it is installed on.
_OWNER: tuple = ()


def claim_scheme(qt_profile, provider) -> "NewTabSchemeHandler":
    """Serve `pybrowser://` from `qt_profile`, if no profile has claimed it yet.

    **Engine limitation, measured not guessed.** In Qt WebEngine 6.11 exactly
    one QWebEngineProfile per process can serve a custom URL scheme. The first
    profile to install a handler keeps it for the life of the process:

    * installing the same scheme on a second profile stops requests being
      answered on *both* profiles - the page hangs, with no error anywhere;
    * `removeUrlSchemeHandler` on the first profile does not release it, so
      ownership cannot be handed over either;
    * this holds whether the profiles share a storage name or not.

    So the first claim wins and later profiles are simply told no. That is
    invisible in the real browser, which has exactly one profile for its
    lifetime - but it means a second profile's tabs cannot show the new-tab
    page, and the honest thing is to say so here rather than let someone spend
    an afternoon on a page that never loads. Tests share one profile for the
    same reason (`tests/qt_profile.py`).
    """
    global _OWNER
    if _OWNER:
        return _OWNER[0]
    handler = NewTabSchemeHandler(provider)
    qt_profile.installUrlSchemeHandler(SCHEME.encode(), handler)
    _OWNER = (handler, qt_profile)
    return handler


def scheme_owner():
    """The QWebEngineProfile serving pybrowser://, or None. For diagnostics."""
    return _OWNER[1] if _OWNER else None


def is_new_tab(url: QUrl | str) -> bool:
    target = QUrl(url) if isinstance(url, str) else url
    return target.scheme() == SCHEME and target.host() == "newtab"


def parse_action(url: QUrl) -> tuple[str, dict[str, str]] | None:
    """Split an action URL into (name, parameters), or None if it is not one."""
    if not is_new_tab(url):
        return None
    path = url.path()
    if not path.startswith(ACTION_PREFIX):
        return None
    name = path[len(ACTION_PREFIX):].strip("/")
    if not name:
        return None
    from PySide6.QtCore import QUrlQuery

    # Qt's FullyDecoded resolves percent-escapes but leaves "+" alone, and in a
    # query string "+" means space. Our own page uses %20, but a pasted or
    # hand-written action URL may not, so decode it the way a query is defined.
    query = QUrlQuery(url)
    decoded = QUrl.ComponentFormattingOption.FullyDecoded
    return name, {
        key: value.replace("+", " ")
        for key, value in query.queryItems(decoded)
    }


# ---------------------------------------------------------------------------
# The data the page shows
# ---------------------------------------------------------------------------


@dataclass
class NewTabData:
    """What the page displays. Plain data, so it can be built and tested
    without a browser anywhere in sight."""

    recent: list[dict[str, str]] = field(default_factory=list)
    bookmarks: list[dict[str, str]] = field(default_factory=list)
    agent_available: bool = False

    def to_json(self) -> str:
        payload = json.dumps({
            "recent": self.recent,
            "bookmarks": self.bookmarks,
            "agentAvailable": self.agent_available,
        }, ensure_ascii=False)
        # A page title containing "</script>" would otherwise end the block and
        # let arbitrary markup follow. Escaping "<" makes that impossible while
        # remaining valid JSON.
        return payload.replace("<", "\\u003c")


def collect(history=None, bookmarks=None, *, agent_available: bool = False,
            limit: int = 8) -> NewTabData:
    """Gather the page's content from the stores, tolerating failure.

    A new tab must open even if the database is locked, missing or broken, so
    every read is guarded: the page degrades to its empty state instead of
    failing to appear.
    """
    data = NewTabData(agent_available=agent_available)
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
    return data


# ---------------------------------------------------------------------------
# The scheme handler
# ---------------------------------------------------------------------------


class NewTabSchemeHandler(QWebEngineUrlSchemeHandler):
    """Serves `pybrowser://newtab/` from memory.

    `provider` is a zero-argument callable returning `NewTabData`; the profile
    supplies one that reads the stores. Keeping it a callable means this class
    knows nothing about SQLite, and the tests can hand it a fixed list.
    """

    def __init__(self, provider, parent=None) -> None:
        super().__init__(parent)
        self._provider = provider

    def set_provider(self, provider) -> None:
        self._provider = provider

    def requestStarted(self, job) -> None:  # noqa: N802 - Qt's name
        url = job.requestUrl()
        if parse_action(url) is not None:
            # Action URLs are intercepted before they ever reach here; if one
            # arrives anyway (a direct paste, say) it must not render as a page.
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        try:
            data = self._provider()
        except Exception:  # noqa: BLE001
            data = NewTabData()
        html = render(data).encode("utf-8")

        buffer = QBuffer(job)
        buffer.setData(QByteArray(html))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        job.reply(QByteArray(b"text/html"), buffer)


# ---------------------------------------------------------------------------
# The page itself
# ---------------------------------------------------------------------------


def render(data: NewTabData) -> str:
    """The complete page. No external requests: no fonts, no CDN, no analytics.

    Everything is inline, which is what makes it appear instantly and work with
    no network at all - including the mascot, which is embedded rather than
    fetched so the page never waits on a file read to finish drawing.
    """
    return (_TEMPLATE
            .replace("__MASCOT__", _mascot_markup())
            .replace("__DATA__", data.to_json()))


def _mascot_markup() -> str:
    """The character for the top of the page, inlined.

    Uses the real artwork when `app/ui/assets/mascot/` has an idle image, and
    the placeholder mark otherwise. Inlined as a data URI rather than linked,
    because a `pybrowser://` sub-resource would need its own handler route for
    no benefit on a page this small.
    """
    try:
        from app.ui.mascot import MascotState, asset_for

        path = asset_for(MascotState.IDLE)
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


#: The stand-in mark, until the character exists. Matches app/ui/mascot.py.
_PLACEHOLDER_MARK = (
    '<svg class="mark" viewBox="0 0 36 36" fill="none" aria-hidden="true">'
    '<rect x="3" y="7" width="30" height="26" rx="9" fill="currentColor" fill-opacity=".12"/>'
    '<rect x="3" y="7" width="30" height="26" rx="9" stroke="currentColor"'
    ' stroke-opacity=".4" stroke-width="1.4"/>'
    '<path d="M18 3v4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>'
    '<circle cx="18" cy="2.6" r="1.7" fill="currentColor"/>'
    '<circle cx="12" cy="19" r="2.1" fill="currentColor"/>'
    '<circle cx="24" cy="19" r="2.1" fill="currentColor"/>'
    '<path d="M14.5 26q3.5 2.4 7 0" stroke="currentColor" stroke-width="1.8"'
    ' stroke-linecap="round" fill="none" opacity=".7"/></svg>'
)


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>New Tab</title>
<style>
  /* Kept in step with app/ui/theme.py: same accent, same radii, same 4px
     spacing scale. The page inside the browser and the chrome around it are
     one design, so they use one set of numbers. */
  :root {
    --bg: #f4f4f7;
    --surface: #ffffff;
    --surface-alt: #eaeaf0;
    --line: #e0e0e8;
    --text: #17171d;
    --muted: #65656f;
    --disabled: #a8a8b4;
    --accent: #4b46d4;
    --accent-soft: #eeedfc;
    --glow: rgba(75, 70, 212, .07);
    --shadow: 0 1px 2px rgba(20, 20, 40, .04), 0 10px 30px rgba(20, 20, 40, .06);
    --shadow-lift: 0 2px 6px rgba(20, 20, 40, .07), 0 16px 40px rgba(20, 20, 40, .10);
    --radius-sm: 6px;
    --radius-md: 9px;
    --radius-lg: 14px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #141419;
      --surface: #1e1e25;
      --surface-alt: #262630;
      --line: #30303b;
      --text: #eeeef3;
      --muted: #9797a6;
      --disabled: #61616e;
      --accent: #8b86ff;
      --accent-soft: #282740;
      --glow: rgba(139, 134, 255, .10);
      --shadow: 0 1px 2px rgba(0, 0, 0, .35), 0 10px 30px rgba(0, 0, 0, .35);
      --shadow-lift: 0 2px 6px rgba(0, 0, 0, .4), 0 16px 40px rgba(0, 0, 0, .45);
    }
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    /* A single very soft wash behind the search box, so the middle of the page
       has a centre of gravity. Two stops, no animation, no second layer: the
       point is that the page feels considered, not that it has a gradient. */
    background:
      radial-gradient(ellipse 720px 420px at 50% 22%,
                      var(--glow) 0%, transparent 70%),
      var(--bg);
    color: var(--text);
    font: 14px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
    display: flex;
    justify-content: center;
    -webkit-font-smoothing: antialiased;
  }
  main {
    width: 100%;
    max-width: 640px;
    padding: 0 24px 72px;
    display: flex;
    flex-direction: column;
    /* Above the true centre - the optical centre of a screen sits higher than
       its middle, and a search box pinned to the exact middle looks low. */
    padding-top: clamp(56px, 15vh, 148px);
    animation: rise .32s cubic-bezier(.22, .8, .3, 1) both;
  }
  @keyframes rise {
    from { opacity: 0; transform: translateY(8px); }
    to   { opacity: 1; transform: none; }
  }
  /* Someone who has asked their system not to animate things means it. */
  @media (prefers-reduced-motion: reduce) {
    main { animation: none; }
    * { transition: none !important; }
  }

  .brand {
    display: flex; flex-direction: column; align-items: center;
    gap: 12px; margin-bottom: 26px; user-select: none;
  }
  .mark {
    width: 64px; height: 64px; color: var(--accent);
    cursor: pointer;
    border-radius: 50%;
    transition: transform .16s ease;
    /* Py breathes here the same way Py breathes in the agent panel. Slow, tiny,
       and off entirely for anyone who asked for less motion. */
    animation: breathe 5.5s ease-in-out infinite;
  }
  /* Below about 420px the character competes with the search box for the
     little vertical space there is, so it steps down rather than dominating. */
  @media (max-height: 620px), (max-width: 420px) {
    .mark { width: 44px; height: 44px; }
    main { padding-top: clamp(24px, 8vh, 72px); }
  }
  /* Barely there on purpose: enough that the page is not a still image,
     little enough that it never asks to be watched. */
  @keyframes breathe {
    0%, 100% { transform: translateY(0) }
    50%      { transform: translateY(-3px) }
  }
  .mark:hover { transform: scale(1.06); animation-play-state: paused; }
  .mark:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
  .wordmark {
    font-size: 23px; font-weight: 600; letter-spacing: -.022em;
  }
  .wordmark span { color: var(--accent); }
  .greeting {
    margin: 2px 0 0; font-size: 13.5px; color: var(--muted); text-align: center;
  }

  form { position: static; }
  .field { position: relative; }
  #q {
    width: 100%;
    height: 52px;
    padding: 0 46px 0 46px;
    font: inherit;
    font-size: 15px;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 26px;
    box-shadow: var(--shadow);
    outline: none;
    transition: border-color .16s ease, box-shadow .16s ease;
  }
  #q::placeholder { color: var(--muted); }
  #q:hover { box-shadow: var(--shadow-lift); }
  #q:focus {
    border-color: var(--accent);
    box-shadow: var(--shadow-lift), 0 0 0 4px var(--accent-soft);
  }
  .search-icon {
    position: absolute; left: 17px; top: 50%; transform: translateY(-50%);
    width: 17px; height: 17px; color: var(--muted); pointer-events: none;
    transition: color .16s ease;
  }
  #q:focus ~ .search-icon { color: var(--accent); }
  .enter {
    position: absolute; right: 14px; top: 50%; transform: translateY(-50%);
    font-size: 11px; color: var(--disabled); pointer-events: none;
    opacity: 0; transition: opacity .16s ease;
  }
  .hint {
    margin: 8px 4px 0; font-size: 12px; color: var(--muted);
    min-height: 17px;
  }

  .ai {
    margin-top: 10px;
    display: flex; align-items: center; gap: 11px; width: 100%;
    padding: 12px 15px;
    text-align: left;
    font: inherit;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius-lg);
    cursor: pointer;
    transition: border-color .16s ease, box-shadow .16s ease, transform .08s ease;
  }
  .ai:hover { border-color: var(--accent); box-shadow: var(--shadow); }
  .ai:active { transform: translateY(1px); }
  .ai:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .ai .glyph {
    width: 30px; height: 30px; flex: none;
    display: flex; align-items: center; justify-content: center;
    background: var(--accent-soft); border-radius: 50%;
  }
  .ai svg { width: 16px; height: 16px; color: var(--accent); }
  .ai b { font-weight: 600; font-size: 13.5px; }
  .ai small { display: block; color: var(--muted); font-size: 12px; }

  /* Things Py can do, offered as cards rather than as a toolbar: each says
     what it is for, because "Compare" on its own is a word, not an offer. */
  .offer-label {
    margin: 18px 0 8px; text-align: center;
    font-size: 12px; color: var(--muted);
  }
  .actions {
    display: grid; gap: 8px;
    grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
  }
  .action {
    font: inherit; text-align: left;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: var(--radius-lg);
    padding: 11px 13px;
    /* Equal height whether the blurb wraps or not - a row of cards that
       disagree about their height is the fastest way to look unfinished. */
    min-height: 62px;
    display: flex; flex-direction: column; justify-content: center;
    cursor: pointer;
    color: var(--text);
    transition: border-color .14s ease, box-shadow .14s ease, transform .08s ease;
  }
  .action:hover {
    border-color: var(--accent); box-shadow: var(--shadow);
  }
  .action:active { transform: translateY(1px); }
  .action:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .action b {
    display: block; font-size: 13px; font-weight: 600; margin-bottom: 2px;
  }
  .action small { color: var(--muted); font-size: 11.5px; line-height: 1.35; }

  .columns {
    margin-top: 32px;
    display: grid; gap: 14px 32px;
    grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  }
  h2 {
    margin: 0 0 6px; font-size: 11px; font-weight: 600;
    letter-spacing: .08em; text-transform: uppercase; color: var(--disabled);
  }
  ul { list-style: none; margin: 0; padding: 0; }
  li a {
    display: flex; align-items: baseline; gap: 8px;
    padding: 7px 9px; margin: 0 -9px;
    border-radius: var(--radius-md); text-decoration: none; color: inherit;
    transition: background .12s ease;
  }
  li a:hover { background: var(--surface-alt); }
  li a:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
  li a .title {
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  li a .host {
    color: var(--disabled); font-size: 12px; flex: none;
    max-width: 42%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .empty {
    color: var(--disabled); font-size: 13px; padding: 7px 0;
  }
  footer {
    margin-top: 32px; text-align: center;
    font-size: 12px; color: var(--disabled);
  }
  footer a { color: var(--muted); text-decoration: none; }
  footer a:hover { color: var(--accent); }
  footer a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
</style>
</head>
<body>
<main>
  <div class="brand">
    <!-- __MASCOT__ is replaced with the character's artwork when there is
         any; otherwise this placeholder mark is used. Both are square and the
         same size, so the layout does not move when the artwork arrives. -->
    __MASCOT__
    <div class="wordmark">Py<span>Browser</span></div>
    <p class="greeting" id="greeting">Hey, I\u2019m Py. What shall we explore?</p>
  </div>

  <form id="f" autocomplete="off">
    <div class="field">
    <input id="q" name="q" type="text" autofocus
           placeholder="Search the web or enter an address"
           aria-label="Search the web or enter an address">
    <svg class="search-icon" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <circle cx="9" cy="9" r="6" stroke="currentColor" stroke-width="1.7"/>
      <path d="m13.5 13.5 3.5 3.5" stroke="currentColor" stroke-width="1.7"
            stroke-linecap="round"/>
    </svg>
    <span class="enter" id="enter">Enter</span>
    </div>
    <p class="hint" id="hint"></p>
  </form>

  <p class="offer-label" id="offer-label">Or let me help with\u2026</p>
  <div class="actions" id="actions"></div>

  <button class="ai" id="ai" type="button">
    <span class="glyph">
      <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
        <path d="M10 2.5 11.6 7 16 8.6 11.6 10.2 10 14.7 8.4 10.2 4 8.6 8.4 7 10 2.5Z"
              fill="currentColor"/>
        <path d="M15.5 13.2 16.2 15 18 15.7 16.2 16.4 15.5 18.2 14.8 16.4 13 15.7 14.8 15 15.5 13.2Z"
              fill="currentColor" opacity=".55"/>
      </svg>
    </span>
    <span>
      <b>Ask Py something else</b>
      <small id="ai-sub">Anything about this page, your tabs, or the web</small>
    </span>
  </button>

  <div class="columns">
    <section>
      <h2>Recent</h2>
      <ul id="recent"></ul>
    </section>
    <section>
      <h2>Bookmarks</h2>
      <ul id="bookmarks"></ul>
    </section>
  </div>

  <footer>
    <a href="pybrowser://newtab/action/history">All history</a>
    &nbsp;·&nbsp;
    <a href="pybrowser://newtab/action/bookmarks">All bookmarks</a>
  </footer>
</main>

<script id="data" type="application/json">__DATA__</script>
<script>
(function () {
  "use strict";
  var data;
  try {
    data = JSON.parse(document.getElementById("data").textContent);
  } catch (e) {
    data = { recent: [], bookmarks: [], agentAvailable: false };
  }

  function act(name, params) {
    var url = "pybrowser://newtab/action/" + name;
    var parts = [];
    for (var key in params) {
      if (params[key]) {
        parts.push(encodeURIComponent(key) + "=" + encodeURIComponent(params[key]));
      }
    }
    // Navigating is how the page reaches Python: the browser intercepts this
    // URL, refuses the navigation, and acts on it.
    window.location.href = parts.length ? url + "?" + parts.join("&") : url;
  }

  function host(url) {
    try {
      var h = new URL(url).hostname;
      return h.indexOf("www.") === 0 ? h.slice(4) : h;
    } catch (e) { return ""; }
  }

  function fill(id, items, emptyText) {
    var list = document.getElementById(id);
    if (!items.length) {
      var note = document.createElement("li");
      note.className = "empty";
      note.textContent = emptyText;
      list.appendChild(note);
      return;
    }
    items.forEach(function (item) {
      var li = document.createElement("li");
      var a = document.createElement("a");
      a.href = "pybrowser://newtab/action/open?url=" + encodeURIComponent(item.url);
      var title = document.createElement("span");
      title.className = "title";
      // textContent, never innerHTML: these titles come from arbitrary
      // websites and this is a privileged page.
      title.textContent = item.title;
      a.appendChild(title);
      a.title = item.url;
      var span = document.createElement("span");
      span.className = "host";
      span.textContent = host(item.url);
      a.appendChild(span);
      li.appendChild(a);
      list.appendChild(li);
    });
  }

  fill("recent", data.recent, "Pages you visit will show up here.");
  fill("bookmarks", data.bookmarks, "Press Ctrl+D on a page to bookmark it.");

  if (!data.agentAvailable) {
    document.getElementById("ai-sub").textContent =
      "Set Py up first in Tools \\u2192 Configure AI Agent";
  }

  var box = document.getElementById("q");
  var hint = document.getElementById("hint");

  document.getElementById("f").addEventListener("submit", function (event) {
    event.preventDefault();
    var text = box.value.trim();
    if (text) { act("search", { q: text }); }
  });

  // A hint, not a decision: Python still decides URL vs. search, so this only
  // has to be roughly right, and being wrong here changes nothing.
  var enter = document.getElementById("enter");
  box.addEventListener("input", function () {
    var text = box.value.trim();
    enter.style.opacity = text ? "1" : "0";
    if (!text) { hint.textContent = ""; return; }
    var looksLikeUrl = /^[a-z][a-z0-9+.-]*:\\/\\//i.test(text) ||
                       /^[^\\s]+\\.[a-z]{2,}([/:?#]|$)/i.test(text) ||
                       /^localhost([:/]|$)/i.test(text);
    hint.textContent = looksLikeUrl ? "Press Enter to go to this address"
                                    : "Press Enter to search the web";
  });

  document.getElementById("ai").addEventListener("click", function () {
    act("ai", { q: box.value.trim() });
  });

  // Py is the companion, so Py is also a button: clicking the character opens
  // the panel. The card above is what makes that discoverable.
  var mark = document.querySelector(".mark");
  if (mark) {
    mark.setAttribute("role", "button");
    mark.setAttribute("tabindex", "0");
    mark.setAttribute("title", "Ask Py");
    mark.setAttribute("aria-label", "Ask Py");
    mark.addEventListener("click", function () { act("ai", { q: box.value.trim() }); });
    mark.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        act("ai", { q: box.value.trim() });
      }
    });
  }

  // Quick actions open the AI panel with the request already written, so the
  // user lands one keystroke from an answer rather than at an empty box. They
  // go through the same action as everything else - there is one AI here.
  var ACTIONS = [
    ["Research", "Go deep on a topic",
     "Research this for me and give me a few good sources: "],
    ["Summarise", "Get the key points",
     "Summarise the page I am looking at."],
    ["Compare", "Look across my tabs",
     "Compare my open tabs and tell me how they differ."],
    ["Explain", "Make it simple and clear",
     "Explain what this page is and who it is for, in plain language."]
  ];
  var actions = document.getElementById("actions");
  ACTIONS.forEach(function (spec) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "action";
    var title = document.createElement("b");
    title.textContent = spec[0];
    var blurb = document.createElement("small");
    blurb.textContent = spec[1];
    button.appendChild(title);
    button.appendChild(blurb);
    button.addEventListener("click", function () {
      var typed = box.value.trim();
      // "Research" is the one that takes whatever is in the box.
      var prompt = spec[2].slice(-2) === ": " ? spec[2] + (typed || "this topic")
                                              : spec[2];
      // Py acknowledges the click here, before the panel has even opened, so
      // the two halves of the interaction feel like one thing.
      var greeting = document.getElementById("greeting");
      greeting.textContent = "On it \u2014 opening Py\u2026";
      act("ai", { q: prompt });
    });
    actions.appendChild(button);
  });
})();
</script>
</body>
</html>
"""
