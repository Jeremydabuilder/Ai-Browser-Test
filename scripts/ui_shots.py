"""Capture the browser's screens, from the real application.

    python scripts/ui_shots.py OUTPUT_DIR [--dark]

Launches the actual PyBrowser window against the local fixture server and
photographs every surface that has to look right: the new-tab page, a real
webpage with the chrome around it, several tabs, the AI panel mid-task, and an
approval prompt. Also captures a deliberately small window, because that is
where layout problems appear first.

Kept in the repo because a visual regression is invisible to a test suite. The
only way to know the UI still looks right is to look at it.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["PYBROWSER_DATA_DIR"] = tempfile.mkdtemp(prefix="pybrowser-shots-")
os.environ.setdefault("PYBROWSER_DISABLE_KEYRING", "1")

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


def settle(app, ms: int) -> None:
    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(ms)
    while not expired[0]:
        app.processEvents()


def wait_for(app, predicate, ms: int = 20000) -> bool:
    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(ms)
    while not predicate() and not expired[0]:
        app.processEvents()
    timer.stop()
    return predicate()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--dark", action="store_true",
                        help="capture the dark palette instead of the light one")
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)

    app = QApplication(sys.argv[:1])
    if args.dark:
        # Qt takes its light/dark cue from the palette; setting a dark window
        # colour is how a headless run reaches the dark theme.
        palette = app.palette()
        palette.setColor(QPalette.ColorRole.Window, palette.color(
            QPalette.ColorRole.Window).darker(400))
        app.setPalette(palette)

    from app.browser.controller import BrowserController
    from app.browser.profile import BrowserProfile
    from app.config import NEW_TAB_URL, database_path
    from app.storage import Database
    from app.ui import theme
    from app.ui.main_window import MainWindow

    theme.apply(app)
    database = Database(database_path())
    profile = BrowserProfile(app)

    from tests.fixture_server import FixtureServer
    server = FixtureServer()

    window = MainWindow(profile, database)
    window.resize(1280, 840)
    window.show()

    # Content, so nothing is photographed empty by accident.
    for url, title in (("https://docs.python.org/3/", "Python 3 documentation"),
                       ("https://news.ycombinator.com/", "Hacker News"),
                       ("https://en.wikipedia.org/wiki/Tide", "Tide - Wikipedia")):
        window.history.add_visit(url, title)
    window.bookmarks.add("https://doc.qt.io/qtforpython-6/", "Qt for Python")
    window.bookmarks.add("https://pypi.org/", "PyPI")

    suffix = "-dark" if args.dark else ""

    def shot(widget, name: str) -> None:
        path = os.path.join(args.out, f"{name}{suffix}.png")
        widget.grab().save(path, "PNG")
        print(f"saved {path}")

    tab = window.tabs.current_tab()
    loaded = []
    tab.load_finished.connect(loaded.append)
    tab.reload()
    wait_for(app, lambda: loaded)
    settle(app, 500)
    shot(window, "1-new-tab")

    # A real page, with the chrome around it.
    loaded.clear()
    tab.navigate(server.url("/research/one"))
    wait_for(app, lambda: loaded)
    window._toggle_bookmark()
    settle(app, 400)
    shot(window, "2-webpage")

    # Several tabs, including one still loading and one long title.
    for path in ("/research/two", "/research/three", "/frames", "/second", NEW_TAB_URL):
        window.tabs.new_tab(path if path.startswith("pybrowser") else server.url(path))
    settle(app, 1500)
    shot(window, "3-tabs")

    # The AI panel, mid-task, then holding an approval.
    from app.agent.config import AgentConfig
    from app.agent.session import AgentSession
    from app.ui.agent_panel import AgentPanel
    from tests.fake_claude import ScriptedClaude, calls, find_ref, says

    window.tabs.setCurrentIndex(0)
    loaded.clear()
    window.tabs.current_tab().navigate(server.base)
    wait_for(app, lambda: loaded)

    def buy(messages):
        return calls("browser_click", {"ref": find_ref(messages, "button",
                                                       name_contains="buy")})

    script = [calls("browser_get_page_text"), calls("browser_list_tabs"),
              calls("browser_get_page"), buy, says("Waiting for you.")]
    session = AgentSession(window.controller, ScriptedClaude(script), AgentConfig())
    panel = AgentPanel(session, window)
    window.set_side_panel(panel)
    window._agent_action.setChecked(True)
    settle(app, 200)
    shot(window, "4-ai-empty")

    asked = []
    session.confirmation_required.connect(asked.append)
    panel.input.setPlainText("Compare my tabs, then buy the thing on this page.")
    panel._send()
    wait_for(app, lambda: asked, 30000)
    settle(app, 300)
    shot(window, "5-approval")
    shot(panel, "6-panel-only")

    # A small window is where layout problems show up first.
    session.cancel()
    settle(app, 300)
    window.resize(900, 620)
    settle(app, 600)
    shot(window, "7-small")

    window.set_side_panel(None)
    window.resize(760, 560)
    settle(app, 600)
    shot(window, "8-narrow")

    session.shutdown()
    server.stop()
    database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
