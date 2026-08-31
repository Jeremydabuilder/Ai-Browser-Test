# PyBrowser

A real desktop web browser written in Python. It renders actual websites with
Qt WebEngine (Chromium) — no Node.js, no Electron, no npm, no mocked pages.

**Phase 1:** tabbed browsing, navigation, persistent history and bookmarks in
SQLite, keyboard shortcuts.
**Phase 2:** a Claude-powered AI agent that operates web pages through the
browser's structured API — see [`docs/ai_agent.md`](docs/ai_agent.md).

> ⚠️ **This is an experimental AI browser, not a production one.** The agent
> mitigates prompt injection but does not solve it, and its sensitivity
> detection is heuristic. Read [`docs/ai_agent.md`](docs/ai_agent.md) §9 before
> using it anywhere that matters.

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
python -m unittest discover -s tests -v          # 187 tests
python scripts/smoke_test.py                     # headless end-to-end run
python scripts/smoke_test.py --url https://pypi.org   # also load a real site
python scripts/validate.py                       # full validation incl. real sites
python scripts/agent_smoke.py --url https://pypi.org   # agent against a real site
```

* **`tests/`** — 39 pure unit tests (URL parsing, SQLite stores, background
  writer, error mapping, navigation guard), 88 BrowserController tests, and 60
  agent tests. All deterministic: the browser is real, the fixture server is
  local (`tests/fixture_server.py`), and the model is scripted
  (`tests/fake_claude.py`), so the agent suite needs no API key and makes no
  network calls.
* **`smoke_test.py`** — boots the real window offscreen against a throwaway
  profile and asserts rendering, JS execution, tabs, back/forward,
  `target=_blank`, history and bookmarks.
* **`validate.py`** — the full pass: loads real websites, walks the whole UI
  (click a link, back, forward, reload, tabs, shortcuts, bookmarks), checks
  error handling, and restarts the app to verify persistence.

`validate.py` probes every host with `urllib` *before* the browser tries it. If
an unrelated HTTP client cannot reach a host either, it reports "not testable
on this network" rather than blaming the browser — and it only calls something
a browser bug when the real origin served another client successfully.

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
    web_page.py             QWebEnginePage subclass: window.open, TLS, permissions, auth
    tab.py                  BrowserTab - one web view + a small navigation API
    tab_manager.py          the tab strip; forwards the *current* tab's signals
    controller.py           BrowserController - the programmatic control surface
    page_script.js          DOM inspection, injected into an isolated JS world
    results.py              ActionResult / ActionError / error codes
    futures.py              BrowserFuture - the async primitive
    safety.py               sensitivity classification (advisory only)
    load_error.py           Chromium net error codes -> plain-English messages
  ui/                       widgets only; no SQL, no WebEngine internals
    main_window.py          chrome, menus, shortcuts, and all the wiring
    navigation_bar.py       toolbar + address bar (emits intent, never navigates)
    dialogs.py              history and bookmark managers
  utils/urls.py             "what the user typed" -> QUrl (or a search)
  agent/                    the Claude agent (no Qt, no DOM, tools only)
    claude_client.py        Anthropic SDK client, runs on a worker thread
    tools.py                18 tool schemas, validation, untrusted fencing
    session.py              the agent loop, state, cancellation, confirmation
    prompt.py               system prompt and the trust boundary
    keys.py                 API key from the OS keyring
    config.py               model and context limits
  ui/agent_panel.py         transcript, activity, input, Allow/Deny
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
and it's what the Phase 2 agent will read when it needs context. History writes
(one per page load) are queued to a background thread so a disk stall can never
block the UI; every read drains that queue first, so the asynchrony is
invisible. Bookmarks and settings stay synchronous because the UI reads them
back immediately.

**4. One programmatic control surface.** `BrowserController` (in
`browser/controller.py`) is the supported way to drive the browser from code —
a general-purpose automation API with no AI in it. Full reference:
**[`docs/browser_api.md`](docs/browser_api.md)**. Three properties define the
boundary, each enforced by tests:

* **Nothing Qt crosses it.** Every method takes and returns plain JSON-able
  data. Tabs are addressed by a stable `tab_id`, never by index, and ids are
  never reused.
* **No arbitrary JavaScript.** There is no `execute_script`. DOM inspection is
  implemented in *our* script, injected into Chromium's isolated
  ApplicationWorld — invisible to the page, and not something a caller can
  supply or influence.
* **Asynchronous, and honest about it.** Operations return a `BrowserFuture`
  resolving to a structured `ActionResult` that reports what the action
  actually caused: `navigated`, `dom_changed`, `opened_tab`.

Element references (`s3:e12`) are scoped to the snapshot that produced them and
resolve to the exact DOM node captured — not to whatever a re-derived selector
would match now. A node that was removed, or recycled for different content,
produces a specific recoverable error instead of a click on the wrong thing.

### Security posture

Set explicitly in `browser/profile.py` and `browser/web_page.py`, and readable
in one place on purpose:

| Area | Setting | Why |
|---|---|---|
| Certificates | Errors always rejected, no click-through | A "proceed anyway" button trains people to click it |
| Mixed content | `AllowRunningInsecureContent` off | Keeps the padlock honest |
| Permissions | Camera/mic/screen/location/notifications default-deny, prompt per site | Explicit consent; answers remembered per site |
| Cookies | `AllowPersistentCookies` | Persists real cookies; **session** cookies still die on quit, as sites intend |
| `file://` pages | Cannot read other local files or remote URLs | Sandboxing |
| Protocol handlers | `registerProtocolHandler` refused | Needs a considered UI, not a silent grant |
| Link auditing | `<a ping>` disabled | Privacy |
| Autoplay | Requires a user gesture | Matches Chrome |
| Clipboard | Write allowed, read behind a permission prompt | "Copy" buttons work; reads need consent |
| User agent | Qt's stock Chromium UA, unmodified | A custom token adds fingerprinting and compat risk for no benefit |
| Downloads | Accepted into `~/Downloads`, filename de-duplicated, notice shown | Qt cancels downloads you do not accept |

Nothing above is conditional on a hostname. There are no per-site rules
anywhere in this codebase.

### Error reporting

Qt reports load failures as a Chromium net error code.
`browser/load_error.py` maps those to a plain sentence plus a category
(`dns`, `network`, `certificate`, `blocked`, `http`, `content`). The user sees
"That address could not be found. Check the spelling of the site name." in a
dismissible notice bar with a Retry button; `ERR_NAME_NOT_RESOLVED (-105)`
goes in the tooltip. Stack traces are never shown. A certificate rejection
says plainly that the connection was blocked and offers no bypass.

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
- **`QUrl("http://")` is "valid".** Qt says yes; `setUrl()` on it renders a
  blank page. `BrowserTab._is_navigable()` additionally requires a host for
  schemes that need one.
- **A scripted click is not a user click.** Chromium's History Manipulation
  Intervention marks history entries created by script-initiated navigation
  with no user activation as skippable, so a later Back steps over them. Real
  mouse clicks are unaffected. This is Chromium behaving as designed — and it
  is worth knowing for Phase 2, since an agent clicking through injected JS
  will see it.
- **Back/forward may emit no load signals at all** when served from the
  back-forward cache. UI state has to follow `urlChanged`, not just
  `loadFinished`.

## Automation API

`BrowserController` exposes the browser to code without exposing Qt:

```python
browser.navigate("https://example.com").wait()
structure = browser.get_page_structure().wait().data["structure"]
field = structure.first(role="searchbox")
browser.type_text(field.ref, "cats", submit=True).then(handle_result)
```

See **[`docs/browser_api.md`](docs/browser_api.md)** for the page-structure
format, the element-reference lifecycle and staleness rules, the error codes,
the async model, and which operations should eventually require user
confirmation.

## The AI agent

Give the browser a task in English and it drives itself:

```
You: Open the second page and tell me its heading.
  → Reading the page
  → Clicking "Second page"
  → Reading the page
Claude: The heading is "Second page".
```

**Setup.** Get an API key from the [Anthropic Console](https://console.anthropic.com/),
then either:

* launch the browser and use **Tools → Configure AI Agent…** to store it in your
  OS keyring (recommended); or
* export it before launching:

  ```bash
  export ANTHROPIC_API_KEY="sk-ant-..."
  python main.py
  ```

Open the panel with **Ctrl+Shift+A** (or Tools → Show AI Agent). The key is
never written to the database, the repository, or any config file.

Sensitive actions — purchases, deletion, sending messages, credentials, payment
details, legal agreements, executable downloads — pause for an explicit
**Allow / Deny**, decided by the *browser*, not by the model.

See [`docs/ai_agent.md`](docs/ai_agent.md) for the architecture, the threading
model, context limits, and an honest account of the security limitations.

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
