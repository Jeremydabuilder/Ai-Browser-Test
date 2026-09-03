"""The `pybrowser://` scheme: registration, routing, and the action channel.

PyBrowser serves a few of its own pages - the new tab, the Mission Library -
from a custom scheme rather than from bundled files or an injected widget. That
makes them real pages: they have URLs, they work with back and forward, they
can be bookmarked, and they compose with tabs like anything else.

This module owns the plumbing that is common to all of them. Each page owns its
own content and lives in its own module.

**The action channel.** An internal page cannot call into the browser. What it
can do is navigate to `pybrowser://<host>/action/<name>?...`, which
`BrowserPage.acceptNavigationRequest` intercepts, refuses to render, and turns
into an `internal_action` signal for the window to act on. The page asks; the
window decides. Every action must be something the user could already do from a
menu.

**Why the scheme check matters.** `parse_action` returns None for anything that
is not a `pybrowser://` URL, so a page on the open web cannot mint one. That is
the entire boundary, and it is one line - which is exactly why it has a test.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QUrl
from PySide6.QtWebEngineCore import (
    QWebEngineUrlRequestJob,
    QWebEngineUrlScheme,
    QWebEngineUrlSchemeHandler,
)

SCHEME = "pybrowser"

#: Everything under this path is an instruction to the browser, not a page.
ACTION_PREFIX = "/action/"


def register_scheme() -> None:
    """Register the scheme with Chromium.

    Must run **before** QApplication is constructed - Chromium reads the scheme
    registry once at startup, and a scheme registered later is simply unknown.
    `main.py` calls this first thing.
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


def is_internal(url: QUrl | str) -> bool:
    """Is this one of our own pages?"""
    target = QUrl(url) if isinstance(url, str) else url
    return target.scheme() == SCHEME


def host_of(url: QUrl | str) -> str:
    target = QUrl(url) if isinstance(url, str) else url
    return target.host() if target.scheme() == SCHEME else ""


def parse_action(url: QUrl) -> tuple[str, dict[str, str]] | None:
    """Split an action URL into (name, parameters), or None if it is not one.

    The name is namespaced by host - `missions:open`, not `open` - so two pages
    can both have an "open" action without one being able to trigger the
    other's. The new-tab page keeps unprefixed names, because its actions
    shipped before there was a second page and renaming them would break URLs
    that already exist in users' history.
    """
    if not is_internal(url):
        return None
    path = url.path()
    if not path.startswith(ACTION_PREFIX):
        return None
    name = path[len(ACTION_PREFIX):].strip("/")
    if not name:
        return None
    host = url.host()
    if host != "newtab":
        name = f"{host}:{name}"
    from PySide6.QtCore import QUrlQuery

    # Qt's FullyDecoded resolves percent-escapes but leaves "+" alone, and in a
    # query string "+" means space. Our own pages use %20, but a pasted or
    # hand-written action URL may not, so decode it the way a query is defined.
    query = QUrlQuery(url)
    decoded = QUrl.ComponentFormattingOption.FullyDecoded
    return name, {
        key: value.replace("+", " ")
        for key, value in query.queryItems(decoded)
    }


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------

#: host -> a callable returning the page's HTML for one request.
_ROUTES: dict[str, object] = {}

#: The handler serving pybrowser://, and the profile it is installed on.
_OWNER: tuple = ()


def route(host: str, renderer) -> None:
    """Serve `pybrowser://<host>/` with `renderer(url) -> str`.

    Registering a route is cheap and idempotent, so a page can claim its host
    at import time and a second window changes nothing.
    """
    _ROUTES[host] = renderer


def claim_scheme(qt_profile) -> "InternalSchemeHandler":
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
    lifetime - but it means a second profile's tabs cannot show an internal
    page, and the honest thing is to say so here rather than let someone spend
    an afternoon on a page that never loads. Tests share one profile for the
    same reason (`tests/qt_profile.py`).
    """
    global _OWNER
    if _OWNER:
        return _OWNER[0]
    handler = InternalSchemeHandler()
    qt_profile.installUrlSchemeHandler(SCHEME.encode(), handler)
    _OWNER = (handler, qt_profile)
    return handler


def scheme_owner():
    """The QWebEngineProfile serving pybrowser://, or None. For diagnostics."""
    return _OWNER[1] if _OWNER else None


class InternalSchemeHandler(QWebEngineUrlSchemeHandler):
    """Serves every `pybrowser://` page from memory, by host."""

    def requestStarted(self, job) -> None:  # noqa: N802 - Qt's name
        url = job.requestUrl()
        if parse_action(url) is not None:
            # Action URLs are intercepted before they ever reach here; if one
            # arrives anyway (a direct paste, say) it must not render as a page.
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        renderer = _ROUTES.get(url.host())
        if renderer is None:
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        try:
            html = renderer(url)
        except Exception:  # noqa: BLE001 - an internal page must not 500
            html = "<!doctype html><title>PyBrowser</title>"
        reply(job, html)


def reply(job, html: str) -> None:
    """Answer a scheme request with a page. Kept here so every internal page
    replies the same way, buffer lifetime included."""
    from PySide6.QtCore import QBuffer, QIODevice

    buffer = QBuffer(job)
    buffer.setData(QByteArray(html.encode("utf-8")))
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    job.reply(QByteArray(b"text/html"), buffer)
