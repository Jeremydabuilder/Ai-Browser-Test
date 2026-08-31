# PyBrowser

A real desktop web browser written in Python. It renders actual websites with
Qt WebEngine (Chromium) — no Node.js, no Electron, no npm, no mocked pages.

**Phase 1 (this repo, working):** tabbed browsing, navigation, persistent
history and bookmarks in SQLite, keyboard shortcuts.
**Phase 2 (designed, not implemented):** a Claude-powered agent panel — see
[`docs/phase2_ai_architecture.md`](docs/phase2_ai_architecture.md).

---

## Requirements

- Python 3.11 or newer
- PySide6 6.6+ (ships Qt WebEngine / Chromium — installed by pip, no separate Qt install)
- Windows, macOS, or Linux

## Install and run

```bash
git clone https://github.com/Jeremydabuilder/Ai-Browser-Test.git
cd Ai-Browser-Test

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
python main.py
```

Open specific pages at startup:

```bash
python main.py https://github.com https://news.ycombinator.com
```

### Linux system packages

The pip wheel bundles Qt itself but relies on a few system libraries. On
Debian/Ubuntu:

```bash
sudo apt-get install -y libegl1 libgl1 libxkbcommon-x11-0 libfontconfig1 \
    libdbus-1-3 libnss3 libxcomposite1 libxdamage1 libxrandr2 libxtst6 \
    libxi6 libxcursor1 libxrender1 libasound2t64
```

If you see `libEGL.so.1: cannot open shared object file`, that list is what's
missing. On Fedora the equivalents are `mesa-libEGL mesa-libGL libxkbcommon-x11
nss alsa-lib`.

## Tests

```bash
python -m unittest discover -s tests -v          # 18 unit tests, no GUI needed
python scripts/smoke_test.py                     # headless end-to-end run
python scripts/smoke_test.py --url https://pypi.org   # also load a real site
```

`smoke_test.py` boots the real window offscreen against a throwaway profile,
serves a small JS page locally, and asserts that rendering, JS execution,
tabs, back/forward, `target=_blank`, history and bookmarks all work.

## Keyboard shortcuts

| Shortcut | Action | | Shortcut | Action |
|---|---|---|---|---|
| `Ctrl+T` | New tab | | `Ctrl+L` / `Alt+D` / `F6` | Focus address bar |
| `Ctrl+W` | Close tab | | `Ctrl+R` / `F5` | Reload |
| `Ctrl+N` | New window | | `Ctrl+Shift+R` | Reload ignoring cache |
| `Ctrl+Q` | Quit | | `Esc` | Stop loading |
| `Ctrl+Tab` | Next tab | | `Alt+←` / `Alt+→` | Back / Forward |
| `Ctrl+1`…`9` | Jump to tab (9 = last) | | `Ctrl+D` | Bookmark this page |
| `Ctrl+H` | History | | `Ctrl+Shift+O` | Bookmarks |
| `Ctrl+±` / `Ctrl+0` | Zoom | | `F11` | Full screen |

## Where your data lives

One SQLite file plus a Chromium profile directory:

| OS | Path |
|---|---|
| Linux | `~/.local/share/PyBrowser/` |
| macOS | `~/Library/Application Support/PyBrowser/` |
| Windows | `%LOCALAPPDATA%\PyBrowser\` |

`browser.sqlite3` holds history, bookmarks and settings; `profile/` and
`cache/` hold Chromium's cookies, local storage and HTTP cache. Set
`PYBROWSER_DATA_DIR` to relocate everything (the tests use this).

Inspect it with any SQLite client:

```bash
sqlite3 ~/.local/share/PyBrowser/browser.sqlite3 \
  "SELECT title, url, visited_at FROM history ORDER BY id DESC LIMIT 10;"
```

## Architecture

```
main.py                     entry point: QApplication, profile, database, window
app/
  config.py                 all filesystem paths and defaults in one place
  storage/
    database.py             SQLite connection + schema (one file, one connection)
    history.py              HistoryStore   - visit log, search, autocomplete
    bookmarks.py            BookmarkStore  - one row per unique URL
    settings.py             SettingsStore  - key/value (home page, search engine)
  browser/                  everything that touches Qt WebEngine
    profile.py              the one shared QWebEngineProfile (cookies, cache, downloads)
    web_page.py             QWebEnginePage subclass: window.open, TLS errors, JS dialogs
    tab.py                  BrowserTab - one web view + a small navigation API
    tab_manager.py          the tab strip; forwards the *current* tab's signals
  ui/                       widgets only; no SQL, no WebEngine internals
    main_window.py          chrome, menus, shortcuts, and all the wiring
    navigation_bar.py       toolbar + address bar (emits intent, never navigates)
    dialogs.py              history and bookmark managers
  utils/urls.py             "what the user typed" -> QUrl (or a search)
  agent/interfaces.py       Phase 2 contracts only - not wired into the app
tests/                      unit tests (no GUI)
scripts/smoke_test.py       headless end-to-end test
docs/                       Phase 2 design
```

### The three decisions that shape the code

**1. Signals flow up, commands flow down.** A `BrowserTab` re-broadcasts its web
view's signals; `TabManager` forwards only the *current* tab's signals; and
`MainWindow` listens to `TabManager`. So the window never connects and
disconnects handlers when you switch tabs, and the toolbar never navigates by
itself — it emits `navigate_requested` and the window decides what that means.
One place to change behaviour, one place to look when something is wrong.

**2. One profile, many tabs.** `QWebEngineProfile` owns cookies, cache and local
storage. All tabs share a single persistent instance, which is why logging into
a site in one tab logs you in everywhere and why you're still logged in after a
restart. The profile must outlive every page using it, so the application owns
it and passes it down.

**3. Storage is separate from the engine.** Chromium keeps its own cookie/cache
store; *our* history and bookmarks live in a plain SQLite file we control. That
keeps the data inspectable, testable without a GUI, and trivially exportable —
and it's what the Phase 2 agent will read when it needs context.

### Notes on the tricky parts

- **`createWindow`** in `web_page.py` is what makes `target="_blank"` links and
  `window.open()` work. A page subclass that ignores it silently drops those
  navigations, which is the most common way a hand-rolled Qt browser feels
  broken on real sites.
- **Certificate errors are rejected, deliberately.** There is no "proceed
  anyway" button. Offering one is the easiest way to make a browser unsafe, so
  adding it should be a conscious decision with a proper interstitial.
- **Downloads must be accepted explicitly.** Qt cancels a download unless
  `downloadRequested` calls `accept()`; `profile.py` does that into `~/Downloads`.
- **`localhost:8000` is not a URL scheme.** The address bar parser checks for
  `://` before trusting a scheme, and defaults loopback/private addresses to
  `http` rather than `https` — same as Chrome.

## Phase 2 readiness

`MainWindow` lays its content out in a horizontal `QSplitter` with the tab area
on the left and `set_side_panel()` reserved for the right. Dropping in the AI
panel needs no structural change:

```
┌─────────────────────────────────────────────┐
│ ←  →  ↻  ⌂  [ address bar ]              ☆ │
├─────────────────────────────┬───────────────┤
│                             │               │
│         WEB PAGE            │   AI AGENT    │
│      (TabManager)           │  (Phase 2)    │
│                             │               │
└─────────────────────────────┴───────────────┘
```

The agent will drive pages through `BrowserTab.navigate()` /
`run_javascript()` — the same API the tests use — reading the DOM and
accessibility tree rather than screenshots. See
[`docs/phase2_ai_architecture.md`](docs/phase2_ai_architecture.md).
