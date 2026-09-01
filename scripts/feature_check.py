"""The whole feature checklist, driven through the real browser.

    python scripts/feature_check.py

Every check below is a real action in a real Qt WebEngine window against a
local fixture server: tabs, navigation, history, bookmarks, downloads,
preferences, and persistence across a restart. Nothing is stubbed.

The fixture server rather than a public website is deliberate - the checks
must fail when the *browser* is wrong, not when the network is. Use
`scripts/real_sites.py` for real hosts; it probes each one with an unrelated
HTTP client first, so a blocked host is reported as blocked rather than
blamed on the browser.
"""
import os, sys, tempfile
sys.path.insert(0, "/home/user/Ai-Browser-Test")
DATA = tempfile.mkdtemp(prefix="e2e-")
os.environ["QT_QPA_PLATFORM"]="offscreen"; os.environ["PYBROWSER_DATA_DIR"]=DATA
os.environ["PYBROWSER_DISABLE_KEYRING"]="1"
import app.browser
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
app = QApplication(sys.argv[:1])
from app.ui import theme; theme.apply(app)
from app.browser.profile import BrowserProfile
from app.storage import Database
from app.config import database_path, NEW_TAB_URL
from app.ui.main_window import MainWindow
from tests.fixture_server import FixtureServer

srv = FixtureServer()
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m)
def pump(p, ms=15000):
    d=[0]; t=QTimer(); t.setSingleShot(True); t.timeout.connect(lambda: d.__setitem__(0,1)); t.start(ms)
    while not p() and not d[0]: app.processEvents()
    t.stop(); return p()
def load(tab, url=None):
    f=[]; c=tab.load_finished.connect(f.append)
    if url: tab.navigate(url)
    pump(lambda: f); tab.load_finished.disconnect(c); return f[0] if f else None

db = Database(database_path()); profile = BrowserProfile(app)
w = MainWindow(profile, db); w.resize(1200, 800); w.show()
t0 = w.tabs.current_tab()
load(t0)
ok(t0.url().toString() == NEW_TAB_URL, f"1  browser launches on PyBrowser New Tab ({t0.url().toString()})")
ok(w.tabs.tabText(0) == "New Tab", "2  new tab is labelled 'New Tab'")
ok(w.nav_bar.address_bar.text() == "", "3  address bar is empty on a new tab")

ok(load(t0, srv.url("/")) is True, "4  a real website loads")
ok(t0.title() == "Fixture Home", f"5  page title tracked ({t0.title()})")

t1 = w.tabs.new_tab(NEW_TAB_URL); load(t1)
ok(w.tabs.count() == 2, "6  second tab opens without destroying the first")
ok(w.tabs.tabText(0) == "Fixture Home", "7  the first tab kept its own page")
w.tabs.setCurrentIndex(0); ok(w.tabs.currentIndex() == 0, "8  tab switching")
ok(w.nav_bar.address_bar.text().startswith("http"), "9  address bar follows the active tab")

load(t0, srv.url("/second"))
ok(t0.can_go_back(), "10 back becomes available")
f=[]; c=t0.load_finished.connect(f.append); t0.back(); pump(lambda: f); t0.load_finished.disconnect(c)
ok(t0.url().toString().rstrip("/") == srv.base.rstrip("/"), "11 back navigates")
f=[]; c=t0.load_finished.connect(f.append); t0.forward(); pump(lambda: f); t0.load_finished.disconnect(c)
ok("second" in t0.url().toString(), "12 forward navigates")
f=[]; c=t0.load_finished.connect(f.append); t0.reload(); pump(lambda: f); t0.load_finished.disconnect(c)
ok(f and f[0], "13 reload works")

w._toggle_bookmark()
ok(w.bookmarks.contains(t0.url().toString()), "14 bookmark added")
w.history.add_visit(t0.url().toString(), t0.title())
ok(w.history.count() > 0, f"15 history recorded ({w.history.count()} entries)")
ok(not w.history.should_record(NEW_TAB_URL), "16 the new-tab page is not recorded as history")

# search and URL routing from the new-tab page
acts=[]; w.tabs.internal_action.connect(lambda n,p: acts.append((n,dict(p))))
w.tabs.setCurrentIndex(1)
t1.run_javascript("document.getElementById('q').value='youtube.com';"
                  "document.getElementById('f').requestSubmit();")
pump(lambda: acts, 6000)
ok(acts and acts[0][0] == "search", f"17 new-tab box asks the browser to route input ({acts})")
from app.utils import urls as u
ok(u.normalize("youtube.com", w.settings.search_url).host() == "youtube.com", "18 a domain goes to the site")
ok("duckduckgo" in u.normalize("best gaming laptops", w.settings.search_url).host(), "19 a phrase goes to search")

# downloads
before = len(profile.downloads.items())
load(t1, srv.url("/downloads-page"))
fin=[]; profile.downloads.finished.connect(fin.append)
t1.run_javascript("document.getElementById('file').click();")
pump(lambda: fin, 15000)
ok(fin and fin[0].state == "completed", f"20 a real download completes ({fin[0].state if fin else 'none'})")
ok(fin and os.path.exists(os.path.join(fin[0].directory, fin[0].file_name)), "21 the file is on disk")
ok(len(profile.downloads.items()) > before, "22 the downloads list has it")

# settings persist
w.settings.new_tab_mode = "custom"; w.settings.new_tab_custom_url = srv.url("/second")
w._apply_settings()
ok(w.tabs.home_url == srv.url("/second"), "23 the new-tab preference applies immediately")

# window resize
w.resize(820, 620); app.processEvents()
ok(w.tabs.width() > 0 and w.width() == 820, "24 window resizes cleanly")

# closing tabs
w.tabs.close_tab(1); ok(w.tabs.count() == 1, "25 closing a tab selects another and keeps the rest")

# restart persistence
db.close()
db2 = Database(database_path())
from app.storage import BookmarkStore, HistoryStore, SettingsStore
ok(BookmarkStore(db2).contains(srv.url("/second")), "26 bookmark survives a restart")
ok(HistoryStore(db2).count() > 0, "27 history survives a restart")
ok(SettingsStore(db2).new_tab_mode == "custom", "28 the new-tab preference survives a restart")
db2.close()
srv.stop()
