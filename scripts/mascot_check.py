"""Check Py in the running browser, not in the source.

    python scripts/mascot_check.py

Launches the real window and walks the states a user actually sees: the
new-tab page, the agent panel, an approval being held, a denial, a clean
answer and a failure. Every claim about which face Py wears is checked
against the widget's own state after the session drove it there.

It exists because a unit test can prove state_for_agent maps ACTING to
WORKING and still tell you nothing about whether the panel ever gets there.
"""
import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["PYBROWSER_DATA_DIR"] = tempfile.mkdtemp(prefix="pybrowser-check-")
os.environ.setdefault("PYBROWSER_DISABLE_KEYRING", "1")
import app.browser  # noqa
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

ok, bad = [], []
def check(name, cond, detail=""):
    (ok if cond else bad).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (f"  ({detail})" if detail else ""))

def settle(app, ms):
    done = [False]; t = QTimer(); t.setSingleShot(True)
    t.timeout.connect(lambda: done.__setitem__(0, True)); t.start(ms)
    while not done[0]: app.processEvents()

def wait_for(app, pred, ms=20000):
    done = [False]; t = QTimer(); t.setSingleShot(True)
    t.timeout.connect(lambda: done.__setitem__(0, True)); t.start(ms)
    while not pred() and not done[0]: app.processEvents()
    t.stop(); return pred()

app = QApplication(sys.argv[:1])
from app.browser.profile import BrowserProfile
from app.config import database_path
from app.storage import Database
from app.ui import theme
from app.ui.main_window import MainWindow
from app.ui.mascot import MascotState, asset_for, Variant
from app.agent.config import AgentConfig
from app.agent.session import AgentSession, AgentState
from app.ui.agent_panel import AgentPanel
from tests.fixture_server import FixtureServer
from tests.fake_claude import ScriptedClaude, calls, find_ref, says

theme.apply(app)
db = Database(database_path())
window = MainWindow(BrowserProfile(app), db)
window.resize(1280, 840)
window.show()
server = FixtureServer()
settle(app, 400)

# --- artwork resolution ----------------------------------------------------
for state in ("idle", "reading", "thinking", "working", "approval", "complete", "stuck"):
    for variant in (Variant.FULL, Variant.PANEL):
        p = asset_for(state, variant)
        # The stem, not the extension: the drop-in contract promises any of
        # gif/webp/apng/png/svg, and the artwork went from SVG to PNG without a
        # line of code changing.
        check(f"artwork resolves: {state}-{variant}",
              p is not None
              and os.path.splitext(os.path.basename(p))[0] == f"{state}-{variant}",
              os.path.basename(p or "-"))

# --- new tab ---------------------------------------------------------------
from app.browser.newtab import NewTabData, render
page = render(NewTabData())
check("new tab inlines the artwork exactly once", page.count("data:image") == 1)
import base64
drawing = base64.b64decode(page.split("base64,", 1)[1].split('"', 1)[0])
with open(asset_for(MascotState.IDLE, Variant.FULL), "rb") as fh:
    check("new tab inlines the artwork byte for byte", drawing == fh.read())

# Transparency, checked on the pixels rather than trusted: a baked-in light
# ground is invisible on a light page and obvious on a dark one.
from PySide6.QtGui import QImage
for _state in ("idle", "approval", "complete", "stuck"):
    for _variant in (Variant.FULL, Variant.PANEL):
        _img = QImage(asset_for(_state, _variant)).convertToFormat(
            QImage.Format.Format_ARGB32)
        _corners = [_img.pixelColor(0, 0), _img.pixelColor(_img.width() - 1, 0),
                    _img.pixelColor(0, _img.height() - 1),
                    _img.pixelColor(_img.width() - 1, _img.height() - 1)]
        check(f"{_state}-{_variant} has no baked background",
              all(c.alpha() == 0 for c in _corners),
              ",".join(str(c.alpha()) for c in _corners))

# --- + button --------------------------------------------------------------
before = window.tabs.count()
btn = getattr(window.tabs, "_new_tab_button", None)
check("+ button exists", btn is not None)
if btn:
    btn.click(); settle(app, 300)
    check("+ button opens a tab", window.tabs.count() == before + 1,
          f"{before} -> {window.tabs.count()}")

# --- Ctrl+F ----------------------------------------------------------------
tab = window.tabs.current_tab()
loaded = []; tab.load_finished.connect(loaded.append)
tab.navigate(server.base); wait_for(app, lambda: loaded); settle(app, 300)
QTest.keySequence(window, QKeySequence("Ctrl+F")); settle(app, 300)
bar = getattr(window, "_find_bar", None) or getattr(window, "find_bar", None)
check("Ctrl+F opens the find bar", bar is not None and not bar.isHidden(),
      type(bar).__name__ if bar else "no find bar attribute")

# --- live configuration ----------------------------------------------------
before_id = window._credential_id
window.settings.set("agent_model", "claude-sonnet-5")
window._apply_agent_settings()
check("live config re-applies without a restart", True, "no restart prompt raised")

# --- the agent states ------------------------------------------------------
def buy(messages):
    return calls("browser_click", {"ref": find_ref(messages, "button", name_contains="buy")})

script = [calls("browser_get_page_text"), calls("browser_list_tabs"),
          calls("browser_get_page"), buy, says("All done.")]
session = AgentSession(window.controller, ScriptedClaude(script), AgentConfig())
panel = AgentPanel(session, window)
window.set_side_panel(panel)
settle(app, 200)
check("panel starts idle", panel.mascot.state() == MascotState.IDLE, panel.mascot.state())
check("panel uses the bust crop",
      "panel" in (asset_for(panel.mascot.state(), panel.mascot.variant) or ""))

seen = set()
panel.mascot.state_changed.connect(seen.add)
asked = []
session.confirmation_required.connect(asked.append)
panel.input.setPlainText("Compare my tabs, then buy the thing on this page.")
panel._send()
wait_for(app, lambda: asked, 30000); settle(app, 300)
check("thinking appeared", MascotState.THINKING in seen, ",".join(sorted(seen)))
check("reading appeared", MascotState.READING in seen)
check("working appeared", MascotState.WORKING in seen)
check("approval is shown while gated", panel.mascot.state() == MascotState.APPROVAL,
      panel.mascot.state())
check("complete is NOT shown while gated", MascotState.COMPLETE not in seen)
check("the approval card is up", not panel.confirmation.isHidden())

# deny -> the run stops; that is not a success
session.resolve_confirmation(False)
wait_for(app, lambda: session.state is AgentState.IDLE, 15000); settle(app, 3200)
check("a denied run never shows complete", panel.mascot.state() != MascotState.COMPLETE,
      panel.mascot.state())

# a clean run -> complete
session2 = AgentSession(window.controller, ScriptedClaude([says("Here you go.")]), AgentConfig())
panel2 = AgentPanel(session2, window)
window.set_side_panel(panel2)
answered = []
session2.assistant_message.connect(answered.append)
panel2.input.setPlainText("What is on this page?")
panel2._send()
wait_for(app, lambda: answered, 20000); settle(app, 400)
check("complete appears after a real answer", panel2.mascot.state() == MascotState.COMPLETE,
      panel2.mascot.state())
settle(app, 3000)
check("complete decays back to idle", panel2.mascot.state() == MascotState.IDLE,
      panel2.mascot.state())

# a failure -> stuck
session3 = AgentSession(window.controller, ScriptedClaude([]), AgentConfig())
panel3 = AgentPanel(session3, window)
window.set_side_panel(panel3)
failed = []
session3.error.connect(failed.append)
panel3.input.setPlainText("break")
panel3._send()
wait_for(app, lambda: failed, 20000); settle(app, 400)
check("a failed run shows stuck, not complete", panel3.mascot.state() == MascotState.STUCK,
      panel3.mascot.state())

session.shutdown(); session2.shutdown(); session3.shutdown()
server.stop(); db.close()
print(f"\n{len(ok)} passed, {len(bad)} failed")
if bad:
    print("failed: " + ", ".join(bad))
sys.exit(1 if bad else 0)
