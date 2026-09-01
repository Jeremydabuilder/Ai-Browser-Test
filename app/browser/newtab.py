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
    no network at all.
    """
    return _TEMPLATE.replace("__DATA__", data.to_json())


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>New Tab</title>
<style>
  :root {
    --bg: #fbfbfd;
    --surface: #ffffff;
    --line: #e6e6ee;
    --text: #1b1b21;
    --muted: #6c6c7a;
    --accent: #4b46d4;
    --accent-soft: #eeedfc;
    --shadow: 0 1px 2px rgba(20, 20, 40, .05), 0 8px 24px rgba(20, 20, 40, .06);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #131318;
      --surface: #1c1c23;
      --line: #2e2e39;
      --text: #f2f2f6;
      --muted: #9a9aab;
      --accent: #8b86ff;
      --accent-soft: #24233a;
      --shadow: 0 1px 2px rgba(0, 0, 0, .3), 0 8px 24px rgba(0, 0, 0, .35);
    }
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
    display: flex;
    justify-content: center;
    -webkit-font-smoothing: antialiased;
  }
  main {
    width: 100%;
    max-width: 720px;
    padding: 0 24px 64px;
    display: flex;
    flex-direction: column;
    /* Sits a little above centre - the classic new-tab optical centre. */
    padding-top: clamp(48px, 14vh, 140px);
  }
  .brand {
    display: flex; align-items: center; gap: 10px;
    justify-content: center; margin-bottom: 28px;
    user-select: none;
  }
  .mark {
    width: 30px; height: 30px; flex: none;
  }
  .wordmark {
    font-size: 25px; font-weight: 640; letter-spacing: -.022em;
  }
  .wordmark span { color: var(--accent); }

  form { position: relative; }
  #q {
    width: 100%;
    padding: 15px 18px 15px 46px;
    font: inherit;
    font-size: 16px;
    color: var(--text);
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 13px;
    box-shadow: var(--shadow);
    outline: none;
    transition: border-color .15s, box-shadow .15s;
  }
  #q::placeholder { color: var(--muted); }
  #q:focus {
    border-color: var(--accent);
    box-shadow: var(--shadow), 0 0 0 3px var(--accent-soft);
  }
  .search-icon {
    position: absolute; left: 16px; top: 50%; transform: translateY(-50%);
    width: 18px; height: 18px; color: var(--muted); pointer-events: none;
  }
  .hint {
    margin: 9px 2px 0; font-size: 12.5px; color: var(--muted);
    min-height: 18px;
  }

  .ai {
    margin-top: 22px;
    display: flex; align-items: center; gap: 12px; width: 100%;
    padding: 13px 16px;
    text-align: left;
    font: inherit;
    color: var(--text);
    background: var(--accent-soft);
    border: 1px solid transparent;
    border-radius: 12px;
    cursor: pointer;
    transition: border-color .15s, transform .06s;
  }
  .ai:hover { border-color: var(--accent); }
  .ai:active { transform: translateY(1px); }
  .ai svg { width: 18px; height: 18px; color: var(--accent); flex: none; }
  .ai b { font-weight: 600; }
  .ai small { display: block; color: var(--muted); font-size: 12.5px; }

  .columns {
    margin-top: 34px;
    display: grid; gap: 26px;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  }
  h2 {
    margin: 0 0 10px; font-size: 11.5px; font-weight: 600;
    letter-spacing: .07em; text-transform: uppercase; color: var(--muted);
  }
  ul { list-style: none; margin: 0; padding: 0; }
  li a {
    display: block; padding: 8px 10px; margin: 0 -10px;
    border-radius: 8px; text-decoration: none; color: inherit;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  li a:hover { background: var(--surface); box-shadow: var(--shadow); }
  li a .host { color: var(--muted); font-size: 12.5px; margin-left: 8px; }
  .empty {
    color: var(--muted); font-size: 13.5px; padding: 8px 0;
  }
  footer {
    margin-top: 40px; text-align: center;
    font-size: 12px; color: var(--muted);
  }
  footer a { color: var(--muted); text-decoration: none; }
  footer a:hover { color: var(--accent); text-decoration: underline; }
</style>
</head>
<body>
<main>
  <div class="brand">
    <svg class="mark" viewBox="0 0 32 32" fill="none" aria-hidden="true">
      <rect x="1.5" y="1.5" width="29" height="29" rx="9"
            stroke="currentColor" stroke-opacity=".18" stroke-width="1.6"/>
      <path d="M11 23V9h5.4a4.3 4.3 0 0 1 0 8.6H11"
            stroke="currentColor" stroke-width="2.6" stroke-linecap="round"
            stroke-linejoin="round" style="color:var(--accent)"/>
      <circle cx="22" cy="21.5" r="2.2" fill="currentColor" style="color:var(--accent)"/>
    </svg>
    <div class="wordmark">Py<span>Browser</span></div>
  </div>

  <form id="f" autocomplete="off">
    <svg class="search-icon" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <circle cx="9" cy="9" r="6" stroke="currentColor" stroke-width="1.8"/>
      <path d="m13.5 13.5 3.5 3.5" stroke="currentColor" stroke-width="1.8"
            stroke-linecap="round"/>
    </svg>
    <input id="q" name="q" type="text" autofocus
           placeholder="Search the web or enter an address"
           aria-label="Search the web or enter an address">
    <p class="hint" id="hint"></p>
  </form>

  <button class="ai" id="ai" type="button">
    <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path d="M10 2.5 11.6 7 16 8.6 11.6 10.2 10 14.7 8.4 10.2 4 8.6 8.4 7 10 2.5Z"
            fill="currentColor"/>
      <path d="M15.5 13.2 16.3 15.2 18.3 16 16.3 16.8 15.5 18.8 14.7 16.8 12.7 16 14.7 15.2 15.5 13.2Z"
            fill="currentColor" opacity=".55"/>
    </svg>
    <span>
      <b>Ask Py AI</b>
      <small id="ai-sub">Summarise a page, compare tabs, or research something</small>
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
      // textContent, never innerHTML: these titles come from arbitrary
      // websites and this is a privileged page.
      a.textContent = item.title;
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
      "Set up the AI assistant in Tools \\u2192 Configure AI Agent";
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
  box.addEventListener("input", function () {
    var text = box.value.trim();
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
})();
</script>
</body>
</html>
"""
