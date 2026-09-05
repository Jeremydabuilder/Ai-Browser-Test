"""Real, measured numbers for startup time, memory per tab, and a large page.

Not a benchmark suite - a small honest instrument. Run it and read what it
prints; nothing here is asserted against a target, because the point is to
know the current numbers, not to pass a threshold with no basis.

    python scripts/perf_check.py

Runs offscreen, so it works over SSH and in CI. Startup time is measured as a
subprocess (a fresh Python interpreter, cold imports) since import time is
most of what "startup" means for a script this size; memory and the
large-page check run in a second, already-warm process because they need to
inspect a live MainWindow.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _rss_kb() -> int | None:
    """This process's resident set size, in KB. Linux only - /proc has no
    equivalent on macOS or Windows, and this is a developer instrument, not
    something an end user's machine needs to run."""
    try:
        with open("/proc/self/status") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _measure_startup() -> float | None:
    """Wall-clock time for a fresh interpreter to import the app and show a
    MainWindow with one tab loaded, end to end. Run out-of-process so the
    number includes cold imports, not just widget construction."""
    tmp = tempfile.mkdtemp(prefix="pybrowser-perf-")
    script = f"""
import os, sys, time
sys.path.insert(0, {ROOT!r})
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["PYBROWSER_DATA_DIR"] = {tmp!r}
start = time.monotonic()
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from app.browser.profile import BrowserProfile
from app.config import database_path
from app.storage import Database
from app.ui.main_window import MainWindow

app = QApplication(sys.argv[:1])
db = Database(database_path())
profile = BrowserProfile(app)
window = MainWindow(profile, db, start_urls=["about:blank"])
window.show()

done = []
tab = window.tabs.current_tab()
if tab is not None:
    tab.load_finished.connect(lambda ok: (done.append(ok), app.quit()))
deadline = QTimer()
deadline.setSingleShot(True)
deadline.timeout.connect(app.quit)
deadline.start(15000)
app.exec()
elapsed = time.monotonic() - start
print("ELAPSED", elapsed if done else -1)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    for line in result.stdout.splitlines():
        if line.startswith("ELAPSED"):
            value = float(line.split()[1])
            return value if value >= 0 else None
    sys.stderr.write(result.stderr)
    return None


def _measure_memory_and_large_page() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ["PYBROWSER_DATA_DIR"] = tempfile.mkdtemp(prefix="pybrowser-perf-mem-")

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from app.browser.profile import BrowserProfile
    from app.config import database_path
    from app.storage import Database
    from app.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    db = Database(database_path())
    profile = BrowserProfile(app)
    window = MainWindow(profile, db, start_urls=["about:blank"])
    window.show()
    app.processEvents()

    def pump(predicate, timeout_ms=15000):
        expired = [False]
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: expired.__setitem__(0, True))
        timer.start(timeout_ms)
        while not predicate() and not expired[0]:
            app.processEvents()
        timer.stop()
        return predicate()

    baseline = _rss_kb()
    print(f"RSS with 1 tab (window shown, no page loaded): "
          f"{baseline / 1024:.1f} MB" if baseline else "RSS unavailable (not Linux)")

    # Open several tabs at about:blank - the cheapest possible page - to see
    # the per-tab cost of the browser's own machinery (a WebEngine profile,
    # a BrowserTab, tab-bar chrome) separated from any one page's content.
    for _ in range(5):
        window.tabs.new_tab("about:blank")
    app.processEvents()
    after_tabs = _rss_kb()
    if baseline and after_tabs:
        print(f"RSS with 6 tabs at about:blank: {after_tabs / 1024:.1f} MB "
              f"({(after_tabs - baseline) / 1024 / 5:.1f} MB/tab average)")

    # A large page: a megabyte-plus of repeated text in one element, the
    # shape that exercises browser_get_page_text's size cap (see
    # ContextLimits in app/agent/config.py) rather than any real page's
    # actual size.
    big_html = ("<html><body><div>" + ("lorem ipsum dolor sit amet " * 40000)
                + "</div></body></html>")
    import base64
    data_url = "data:text/html;base64," + base64.b64encode(big_html.encode()).decode()
    tab = window.tabs.current_tab()
    started = time.monotonic()
    finished = []
    tab.load_finished.connect(finished.append)
    tab.navigate(data_url)
    ok = pump(lambda: bool(finished), 20000)
    load_time = time.monotonic() - started
    after_big_page = _rss_kb()
    print(f"Loading a {len(big_html) / 1_000_000:.1f} MB page: "
          f"{'loaded' if ok else 'TIMED OUT'} in {load_time:.2f}s"
          + (f", RSS now {after_big_page / 1024:.1f} MB" if after_big_page else ""))

    from app.browser.controller import BrowserController
    controller = BrowserController(window.tabs)
    text_result = controller.get_page_text().wait()
    text_len = len(text_result.data.get("text", "")) if text_result.data else 0
    print(f"browser_get_page_text on that page returned {text_len} chars "
          f"(capped, not the full {len(big_html)} char page)")

    window.close()


if __name__ == "__main__":
    print("=== PyBrowser performance check ===")
    print("(measured on this machine, right now - not a portable benchmark)\n")

    elapsed = _measure_startup()
    if elapsed is not None:
        print(f"Cold startup (fresh interpreter -> window shown, 1 tab loaded): "
              f"{elapsed:.2f}s")
        print("  (a headless/offscreen container with no GPU falls back to "
              "software rendering, which inflates this; re-measure on a real "
              "desktop for a true baseline)")
    else:
        print("Cold startup: could not measure (see stderr)")
    print()
    _measure_memory_and_large_page()
