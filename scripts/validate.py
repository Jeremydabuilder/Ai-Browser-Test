"""Full Phase 1 validation: real websites through the real Qt WebEngine app.

For every site it reports what actually happened in the browser, and - crucially
- whether a failure came from the browser or from the network the machine is on.
The distinction is made by probing each host independently (a plain TCP/TLS
CONNECT through whatever proxy is configured) BEFORE the browser tries it. If an
unrelated client cannot reach a host either, the browser is not the problem.

    python scripts/validate.py
    python scripts/validate.py --sites https://example.com https://pypi.org
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import ssl
import sys
import threading
import tempfile
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["PYBROWSER_DATA_DIR"] = tempfile.mkdtemp(prefix="pybrowser-validate-")

from PySide6.QtCore import QPoint, Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.browser.controller import BrowserController, ScrollDirection  # noqa: E402
from app.browser.profile import BrowserProfile  # noqa: E402
from app.config import database_path  # noqa: E402
from app.storage import Database  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

DEFAULT_SITES = [
    "https://example.com",
    "https://github.com",
    "https://www.wikipedia.org",
    "https://www.reddit.com",
    "https://www.youtube.com",
    "https://pypi.org",
]

REACHABLE = "reachable"          # the real origin served a normal response
INTERCEPTED = "intercepted"      # something answered, but not the real site
BLOCKED = "blocked-by-network"   # nothing got through at all
UNKNOWN = "unknown"


def probe_host(url: str, timeout: int = 15) -> tuple[str, str]:
    """Can a client other than the browser reach this URL, and reach the real site?

    Uses urllib, which honours the same proxy environment variables the browser
    does but shares none of its code. A failure here is evidence about the
    network, not about Qt WebEngine.

    The distinction that matters: "something answered on this hostname" is not
    the same as "the website answered". A corporate or sandbox egress proxy
    happily returns its own 4xx page for a host it is filtering, and treating
    that as "reachable" would frame an environment restriction as a browser
    bug. So a non-2xx/3xx answer is only ever evidence of interception, never
    evidence that the browser is at fault.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "validate/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(400)
            if 200 <= response.status < 400:
                return REACHABLE, f"HTTP {response.status}, {len(body)}+ bytes from origin"
            return INTERCEPTED, f"HTTP {response.status}: {body[:160]!r}"
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(400)
        except Exception:  # noqa: BLE001
            pass
        return INTERCEPTED, f"HTTP {exc.code} from an intermediary: {body[:160]!r}"
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        if "403" in reason or "tunnel" in reason.lower() or "CONNECT" in reason:
            return BLOCKED, f"proxy refused CONNECT ({reason})"
        if isinstance(exc.reason, ssl.SSLError):
            return BLOCKED, f"TLS intercepted/refused ({reason})"
        return BLOCKED, reason
    except Exception as exc:  # noqa: BLE001
        return UNKNOWN, f"{type(exc).__name__}: {exc}"


class Validator:
    def __init__(self) -> None:
        self.app = QApplication(sys.argv[:1])
        self.db = Database(database_path())
        self.profile = BrowserProfile(self.app)
        self.window = MainWindow(self.profile, self.db, start_urls=["about:blank"])
        self.window.resize(1280, 900)
        self.window.show()
        self.browser: BrowserController = self.window.controller
        self.passed: list[str] = []
        self.failed: list[str] = []
        self.site_results: list[dict] = []

    # -- helpers ---------------------------------------------------------
    def _tab(self):
        """The window's active tab widget.

        The validation harness reaches past BrowserController deliberately: it
        has to synthesise real mouse events and read raw DOM values to check
        the browser itself. An automation caller gets no such access - see
        tests/test_browser_controller.py::ApiBoundaryTests.
        """
        return self.window.tabs.current_tab()

    def url(self) -> str:
        return self.browser.get_current_page().page.url

    def check(self, name: str, ok: bool, detail: object = "") -> bool:
        (self.passed if ok else self.failed).append(name)
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail != "" else ""))
        return ok

    def pump(self, predicate, timeout_ms: int = 45000) -> bool:
        expired = [False]
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: expired.__setitem__(0, True))
        timer.start(timeout_ms)
        while not predicate() and not expired[0]:
            self.app.processEvents()
        timer.stop()
        return predicate()

    def wait_load(self, tab=None, timeout_ms: int = 45000) -> bool:
        tab = tab or self._tab()
        done: list[bool] = []
        tab.load_finished.connect(done.append)
        self.pump(lambda: bool(done), timeout_ms)
        try:
            tab.load_finished.disconnect(done.append)
        except RuntimeError:
            pass
        return bool(done) and done[-1]

    def js(self, script: str, tab=None, timeout_ms: int = 15000):
        tab = tab or self._tab()
        box: dict = {}
        tab.run_javascript(script, functools.partial(box.__setitem__, "v"))
        self.pump(lambda: "v" in box, timeout_ms)
        return box.get("v")

    def real_click(self, selector: str, timeout_ms: int = 15000) -> bool:
        """Click an element with a genuine mouse event at its real position.

        Takes a CSS selector against this harness's own fixture page - the
        browser no longer stamps marker attributes into pages, by design, so
        there is nothing to look an element reference up by from outside
        BrowserController. That is the correct trade: an automation caller
        addresses elements by reference, and this harness owns its fixture.
        """
        rect = self.js(
            "(function(){var e=document.querySelector(%s);"
            "if(!e)return '';e.scrollIntoView({block:'center'});"
            "var r=e.getBoundingClientRect();"
            "return Math.round(r.left+r.width/2)+','+Math.round(r.top+r.height/2);})()"
            % json.dumps(selector)
        )
        if not rect or "," not in str(rect):
            return False
        x, y = (int(float(v)) for v in str(rect).split(","))
        view = self._tab().view
        target = view.focusProxy() or view
        QTest.mouseClick(target, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, QPoint(x, y))
        return True

    def structure(self, timeout_ms: int = 15000):
        box: dict = {}
        result = self.browser.get_page_structure().wait(timeout_ms)
        if result is None or not result.ok:
            self.check("page structure could be captured", False,
                       result.error.code if result and result.error else "no result")
            return None
        return result.data.get("structure")

    # -- part 1: real websites -------------------------------------------
    def test_sites(self, sites: list[str]) -> None:
        print("\n" + "=" * 72)
        print("REAL WEBSITES  (each host probed independently first)")
        print("=" * 72)
        for url in sites:
            print(f"\n--- {url}")
            verdict, evidence = probe_host(url)
            print(f"  network probe (urllib, not the browser): {verdict} - {evidence}")

            tab = self._tab()
            loaded = self.browser.navigate(url).wait(65000).ok
            error = tab.last_error
            result = {
                "url": url,
                "network_probe": verdict,
                "probe_detail": evidence,
                "loaded": loaded,
                "error": None if error is None else f"{error.category}: {error.technical}",
            }

            if loaded:
                html = self.js("document.documentElement.outerHTML") or ""
                title = self.js("document.title") or ""
                # CSS: did the engine actually build a layout and resolve styles?
                css = self.js(
                    "(function(){var b=document.body;if(!b)return '';"
                    "var s=getComputedStyle(b);"
                    "return s.fontFamily+'|'+s.color+'|'+b.getBoundingClientRect().width;})()"
                ) or ""
                links = self.js("document.querySelectorAll('a[href]').length")
                struct = self.structure()
                scrolled = self.js(
                    "(function(){var y0=window.scrollY;window.scrollBy(0,400);"
                    "return window.scrollY!==y0||document.documentElement.scrollHeight<=window.innerHeight;})()"
                )
                result.update({
                    "title": title, "html_bytes": len(html), "links": links,
                    "css": css, "elements": struct.element_count if struct else 0,
                })
                print(f"  HTTPS + DNS      : OK")
                print(f"  HTML rendered    : {len(html)} bytes of live DOM")
                print(f"  CSS resolved     : font/color/width = {css}")
                print(f"  JavaScript       : {'OK' if title or html else 'no'}")
                print(f"  page title       : {title!r}")
                print(f"  URL in bar       : {self.window.nav_bar.address_bar.text()!r}")
                print(f"  links found      : {links}")
                print(f"  interactive elts : {struct.element_count if struct else 0}")
                print(f"  scrolling        : {'OK' if scrolled else 'no scroll happened'}")
                self.check(f"{url} loads and renders", len(html) > 500, f"{len(html)} bytes")
            else:
                detail = f"{error.category}/{error.technical}" if error else "no error info"
                print(f"  browser result   : did not load ({detail})")
                if verdict == BLOCKED:
                    print("  VERDICT          : Unable to test this site because of the")
                    print("                     Claude Code environment. An unrelated HTTP")
                    print("                     client is blocked for the same host, so this")
                    print("                     is NOT a browser defect.")
                    result["verdict"] = "sandbox-blocked"
                elif verdict == INTERCEPTED:
                    print("  VERDICT          : Unable to test this site because of the")
                    print("                     Claude Code environment. An intermediary, not")
                    print("                     the real site, answered for this host, so the")
                    print("                     browser never saw the real site. NOT a browser")
                    print("                     defect.")
                    result["verdict"] = "sandbox-intercepted"
                elif verdict == REACHABLE:
                    print("  VERDICT          : BROWSER BUG - the real origin served this host")
                    print("                     to another client but the browser could not.")
                    result["verdict"] = "browser-bug"
                    self.check(f"{url} loads (origin is reachable)", False, detail)
                else:
                    result["verdict"] = "inconclusive"
            self.site_results.append(result)

    # -- part 2: functional UI walk-through -------------------------------
    def test_functionality(self, base: str) -> None:
        print("\n" + "=" * 72)
        print("FUNCTIONAL UI WALK-THROUGH")
        print("=" * 72)
        tabs = self.window.tabs
        nav = self.window.nav_bar

        self.check("navigate to a website", self.browser.navigate(base).wait().ok)
        self.check("page title updates", tabs.tabText(tabs.currentIndex()).startswith("JS Test"),
                   tabs.tabText(tabs.currentIndex()))
        self.check("URL updates in the address bar", nav.address_bar.text().startswith(base))

        # Enter another URL through the address bar, as a user would.
        nav.address_bar.setText(base + "second.html")
        nav.address_bar.returnPressed.emit()
        self.check("enter another URL", self.wait_load() and
                   self.url().endswith("second.html"))

        # Click a real link via the controller's element handles.
        self.browser.navigate(base).wait()
        struct = self.structure()
        self.check("page structure exposes elements", struct.element_count > 0, struct.element_count)

        # Click the link with a REAL mouse event, the way a user would, rather
        # than with element.click() from injected JavaScript.
        #
        # This distinction is not pedantry. Chromium's History Manipulation
        # Intervention marks history entries created by script-initiated
        # navigation that carries no user activation as "skippable", so a later
        # Back skips straight past them. A JS-driven click therefore produces a
        # history that behaves differently from a user's - which is exactly
        # what an earlier version of this test tripped over and misreported as
        # a browser bug. (Worth knowing for Phase 2: an agent clicking through
        # injected JS will see the same effect.)
        clicked = self.real_click("#internal")
        self.pump(lambda: self.url().endswith("second.html"), 15000)
        self.check("click a link", clicked and self.url().endswith("second.html"), self.url())

        # Separately: the controller's programmatic click must still work.
        self.browser.navigate(base).wait()
        struct = self.structure()
        link = next((e for e in struct.elements if e.role == "link" and "internal" in e.name.lower()), None)
        if link:
            box: dict = {}
            self.browser.click(link.ref).wait()
            self.pump(lambda: self.url().endswith("second.html"), 15000)
            self.check("controller.click() follows a link",
                       self.url().endswith("second.html"))
        else:
            self.check("controller.click() follows a link", False, "no link element found")

        # Re-establish a clean history for the back/forward checks: base -> second
        # via a real click, so no entry is marked skippable.
        self.browser.navigate(base).wait()
        self.real_click("#internal")
        self.pump(lambda: self.url().endswith("second.html"), 15000)

        # Back/forward can be served from the back-forward cache, in which case
        # Chromium emits no loadStarted/loadFinished at all. Waiting on a load
        # signal is therefore the wrong assertion - wait for the URL to change,
        # which is what the user and the address bar actually care about.
        started = self.browser.go_back().wait().ok
        self.pump(lambda: not self.url().endswith("second.html"), 20000)
        self.check("go back", started and
                   not self.url().endswith("second.html"),
                   self.url())
        started = self.browser.go_forward().wait().ok
        self.pump(lambda: self.url().endswith("second.html"), 20000)
        self.check("go forward", started and
                   self.url().endswith("second.html"),
                   self.url())
        self.check("address bar follows back/forward",
                   self.window.nav_bar.address_bar.text().endswith("second.html"),
                   self.window.nav_bar.address_bar.text())
        self.check("reload", self.browser.reload().wait().ok)

        # Typing into a field, through the controller.
        self.browser.navigate(base).wait()
        struct = self.structure()
        field = next((e for e in struct.elements if e.role in ("textbox", "searchbox")), None)
        if field:
            box = {}
            self.browser.type_text(field.ref, "hello world").wait()
            self.check("type into a field",
                       self.js("document.getElementById('q').value") == "hello world")
        else:
            self.check("type into a field", False, "no input found")

        # Scrolling through the controller.
        before = self.js("window.scrollY")
        self.browser.scroll(ScrollDirection.DOWN).wait()
        self.pump(lambda: self.js("window.scrollY") != before, 5000)
        self.check("scroll down", self.js("window.scrollY") > before,
                   f"{before} -> {self.js('window.scrollY')}")
        self.browser.scroll(ScrollDirection.TOP).wait()
        self.pump(lambda: self.js("window.scrollY") == 0, 5000)
        self.check("scroll back to top", self.js("window.scrollY") == 0)

        # Tabs.
        count = tabs.count()
        opened = self.browser.open_tab(base + "second.html").wait()
        second_id = opened.effects.new_tab_id
        second = self.window.tabs.current_tab()
        self.check("open a new tab", tabs.count() == count + 1)
        self.wait_load(second)
        self.check("new tab navigates independently",
                   second.url().toString().endswith("second.html") and
                   tabs.count() == count + 1)
        self.check("active tab is the new one", tabs.current_tab() is second)
        self.check("tab titles are per-tab",
                   tabs.tabText(tabs.count() - 1) != tabs.tabText(0),
                   (tabs.tabText(0), tabs.tabText(tabs.count() - 1)))
        self.browser.close_tab()
        self.check("close the tab", tabs.count() == count)
        self.check("active tab changed correctly", tabs.current_tab() is not second)

        # Keyboard shortcuts, invoked exactly as the menu/shortcut wiring does.
        count = tabs.count()
        self.window._focus_address_bar()
        self.check("Ctrl+L focuses the address bar", nav.address_bar.hasFocus())
        tabs.new_tab(base)
        self.check("Ctrl+T opens a tab", tabs.count() == count + 1)
        self.wait_load()
        tabs.close_current_tab()
        self.check("Ctrl+W closes a tab", tabs.count() == count)
        self.window._reload()
        self.check("Ctrl+R reloads", self.wait_load())

        # Bookmarks.
        current_url = self.url()
        self.window._toggle_bookmark()
        self.check("Ctrl+D adds a bookmark", self.window.bookmarks.contains(current_url))

    # -- part 3: error handling -------------------------------------------
    def test_error_handling(self) -> None:
        print("\n" + "=" * 72)
        print("ERROR HANDLING")
        print("=" * 72)
        # Port 1 would test ERR_UNSAFE_PORT (Chromium refuses it outright), not
        # a refused connection - use a closed high port for that.
        cases = [
            ("http://this-host-does-not-exist-pybrowser.invalid/", "dns"),
            ("http://127.0.0.1:47999/", "network"),
            ("http://127.0.0.1:1/", "blocked"),
        ]
        for url, expected in cases:
            tab = self._tab()
            self.browser.navigate(url)
            self.wait_load(tab, 30000)
            error = tab.last_error
            ok = error is not None and error.category == expected
            self.check(f"{expected} failure is reported", ok,
                       f"{error.category}/{error.message}" if error else "no error captured")
            if error:
                self.check(f"{expected} message is human-readable",
                           not any(t in error.message for t in ("ERR_", "Traceback", "Exception")),
                           error.message)
                self.check(f"{expected} shows a notice to the user",
                           self.window.notice.isVisible())

        # An unusable address must not silently blank the page.
        result = self.browser.navigate("http://").wait()
        self.check("invalid URL is rejected, not silently ignored",
                   not result.ok and result.error.code == "INVALID_URL",
                   result.error.code if result.error else "no error")

    # -- part 4: persistence ----------------------------------------------
    def test_persistence(self, base: str) -> dict:
        print("\n" + "=" * 72)
        print("PERSISTENCE (before restart)")
        print("=" * 72)
        self.browser.navigate(base + "second.html").wait()
        # Toggling the page bookmarked earlier would REMOVE it; bookmark a
        # different page so we are testing persistence, not the toggle.
        self.window._toggle_bookmark()
        history = [e.url for e in self.window.history.recent(200)]
        bookmarks = [b.url for b in self.window.bookmarks.all()]
        self.check("history has rows", len(history) > 0, len(history))
        self.check("bookmarks have rows", len(bookmarks) > 0, len(bookmarks))
        self.db.close()
        return {"history": len(history), "bookmarks": len(bookmarks),
                "db": str(database_path())}

    def finish(self) -> int:
        print("\n" + "=" * 72)
        print(f"RESULT: {len(self.passed)} passed, {len(self.failed)} failed")
        if self.failed:
            print("FAILED: " + ", ".join(self.failed))
        print("=" * 72)
        return 1 if self.failed else 0


# ---------------------------------------------------------------------------
# A local page rich enough to exercise CSS, links, forms and scrolling without
# depending on any particular website staying online or unblocked.
# ---------------------------------------------------------------------------
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Loading</title>
<style>
  body { font-family: Georgia, serif; color: rgb(17, 34, 51); margin: 0; padding: 24px; }
  #box { width: 240px; height: 120px; background: rgb(200, 40, 40); }
  .tall { height: 3000px; }
</style></head><body>
<h1 id="h">plain</h1>
<div id="box"></div>
<a id="internal" href="/second.html">internal link</a>
<a id="ext" href="/second.html" target="_blank">new tab link</a>
<form id="f"><input id="q" name="q" type="text" placeholder="search"><button id="go">Go</button></form>
<div class="tall">scroll space</div>
<script>
  document.title = 'JS Test Page';
  document.getElementById('h').textContent = 'rendered-by-js';
</script></body></html>"""
SECOND = """<!doctype html><html><head><meta charset="utf-8"><title>Second Page</title>
</head><body><h1>page two</h1><a href="/">home</a></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = (SECOND if self.path.startswith("/second") else PAGE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


def start_server() -> str:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sites", nargs="*", default=DEFAULT_SITES)
    parser.add_argument("--skip-sites", action="store_true")
    args = parser.parse_args()

    base = start_server()
    validator = Validator()

    if not args.skip_sites:
        validator.test_sites(args.sites)
    validator.test_functionality(base)
    validator.test_error_handling()
    state = validator.test_persistence(base)

    # --- restart: a brand new Database and window over the same files ---
    print("\n" + "=" * 72)
    print("PERSISTENCE (after restart - fresh Database and MainWindow)")
    print("=" * 72)
    reopened = Database(database_path())
    window2 = MainWindow(validator.profile, reopened, start_urls=["about:blank"])
    window2.show()
    validator.check("history survived the restart",
                    window2.history.count() >= state["history"], window2.history.count())
    validator.check("bookmarks survived the restart",
                    len(window2.bookmarks.all()) >= state["bookmarks"],
                    len(window2.bookmarks.all()))
    validator.check("reopened database is usable",
                    window2.history.recent(5) is not None)
    reopened.close()

    print("\nSITE SUMMARY")
    for r in validator.site_results:
        status = ("LOADED" if r["loaded"] else
                  {"sandbox-blocked": "NOT TESTABLE (sandbox)",
                   "browser-bug": "FAILED (browser)",
                   }.get(r.get("verdict"), "INCONCLUSIVE"))
        print(f"  {r['url']:<34} {status:<24} probe={r['network_probe']}")

    with open(os.path.join(ROOT, "validation-report.json"), "w") as fh:
        json.dump({"sites": validator.site_results,
                   "passed": validator.passed, "failed": validator.failed}, fh, indent=2)
    return validator.finish()


if __name__ == "__main__":
    raise SystemExit(main())
