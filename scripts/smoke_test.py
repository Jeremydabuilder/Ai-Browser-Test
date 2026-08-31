"""Headless end-to-end check of the browser.

Boots the real MainWindow against a throwaway profile, serves a small JS page
from a local HTTP server, and exercises rendering, navigation, tabs, history
and bookmarks. Optionally also loads a real website.

    python scripts/smoke_test.py                       # local only
    python scripts/smoke_test.py --url https://pypi.org  # also hit the network

Runs offscreen, so it works over SSH and in CI.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import os
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# A throwaway profile so a smoke run never touches your real history.
_TMP = tempfile.mkdtemp(prefix="pybrowser-smoke-")
os.environ["PYBROWSER_DATA_DIR"] = _TMP

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.browser.profile import BrowserProfile  # noqa: E402
from app.config import database_path  # noqa: E402
from app.storage import Database  # noqa: E402
from app.ui.main_window import MainWindow  # noqa: E402

PAGE = """<!doctype html><html><head><title>Loading</title></head><body>
<h1 id="h">plain</h1>
<a id="ext" href="/second.html" target="_blank">new tab</a>
<script>document.title='JS Test Page';
document.getElementById('h').textContent='rendered-by-js';</script>
</body></html>"""
SECOND = "<!doctype html><title>Second Page</title><h1>two</h1>"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = (SECOND if self.path.startswith("/second") else PAGE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence request logging
        return


def start_server() -> str:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/"


class Runner:
    def __init__(self, app: QApplication) -> None:
        self.app = app
        self.passed: list[str] = []
        self.failed: list[str] = []

    def check(self, name: str, ok: bool, detail: object = "") -> bool:
        (self.passed if ok else self.failed).append(name)
        suffix = f"  ({detail})" if detail != "" else ""
        print(f"{'PASS' if ok else 'FAIL'}  {name}{suffix}")
        return ok

    def pump_until(self, predicate, timeout_ms: int = 45000) -> bool:
        expired = [False]
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: expired.__setitem__(0, True))
        timer.start(timeout_ms)
        while not predicate() and not expired[0]:
            self.app.processEvents()
        return predicate()

    def wait_load(self, tab, timeout_ms: int = 45000) -> bool:
        done: list[bool] = []
        tab.load_finished.connect(done.append)
        finished = self.pump_until(lambda: bool(done), timeout_ms)
        tab.load_finished.disconnect(done.append)
        return finished and done[-1]

    def js(self, tab, script: str, timeout_ms: int = 15000):
        box: dict = {}
        tab.run_javascript(script, functools.partial(box.__setitem__, "v"))
        self.pump_until(lambda: "v" in box, timeout_ms)
        return box.get("v")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="a real website to additionally load")
    args = parser.parse_args()

    local = start_server()
    app = QApplication(sys.argv[:1])
    db = Database(database_path())
    profile = BrowserProfile(app)
    window = MainWindow(profile, db, start_urls=["about:blank"])
    window.resize(1280, 800)
    window.show()

    r = Runner(app)
    tab = window.tabs.current_tab()

    tab.navigate(local)
    r.check("local page renders", r.wait_load(tab))
    r.check(
        "JavaScript executes",
        r.js(tab, "document.getElementById('h').textContent") == "rendered-by-js",
    )
    r.check("tab title follows the page", window.tabs.tabText(0).startswith("JS Test"),
            window.tabs.tabText(0))
    r.check("address bar shows the URL",
            window.nav_bar.address_bar.text().startswith(local),
            window.nav_bar.address_bar.text())

    tab.navigate(local + "second.html")
    r.wait_load(tab)
    r.check("URL updates on navigation", tab.url().toString().endswith("second.html"))
    r.check("back is available", tab.can_go_back())
    tab.back()
    r.wait_load(tab)
    r.check("back navigates", not tab.url().toString().endswith("second.html"))
    tab.forward()
    r.wait_load(tab)
    r.check("forward navigates", tab.url().toString().endswith("second.html"))
    tab.reload()
    r.check("reload works", r.wait_load(tab))

    second = window.tabs.new_tab(local)
    r.check("new tab opens", window.tabs.count() == 2, window.tabs.count())
    r.wait_load(second)
    r.check("tabs keep independent titles",
            window.tabs.tabText(0) != window.tabs.tabText(1))

    before = window.tabs.count()
    r.js(second, "document.getElementById('ext').click()")
    r.pump_until(lambda: window.tabs.count() > before, 10000)
    r.check("target=_blank opens a new tab", window.tabs.count() == before + 1)

    count = window.tabs.count()
    window.tabs.close_current_tab()
    r.check("tab closes", window.tabs.count() == count - 1)

    current = window.tabs.current_tab()
    if args.url:
        current.navigate(args.url)
        ok = r.wait_load(current, 60000)
        html = r.js(current, "document.documentElement.outerHTML") or ""
        r.check(f"real website loads ({args.url})", ok and len(html) > 2000,
                f"{len(html)} bytes")

    urls = [entry.url for entry in window.history.recent(50)]
    r.check("history written to SQLite", any("127.0.0.1" in u for u in urls), len(urls))
    r.check("history stores titles",
            any(entry.title for entry in window.history.recent(50)))

    window._toggle_bookmark()
    bookmarked = window.bookmarks.contains(current.url().toString())
    r.check("bookmark added", bookmarked)
    window._toggle_bookmark()
    r.check("bookmark removed", not window.bookmarks.contains(current.url().toString()))

    window._focus_address_bar()
    r.check("Ctrl+L focuses the address bar", window.nav_bar.address_bar.hasFocus())
    window.nav_bar.address_bar.setText(local.replace("http://", "") + "second.html")
    window.nav_bar.address_bar.returnPressed.emit()
    r.wait_load(window.tabs.current_tab())
    r.check("typed address navigates",
            window.tabs.current_tab().url().toString().endswith("second.html"),
            window.tabs.current_tab().url().toString())

    print(f"\n{len(r.passed)} passed, {len(r.failed)} failed")
    if r.failed:
        print("failed:", ", ".join(r.failed))
    db.close()
    return 1 if r.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
