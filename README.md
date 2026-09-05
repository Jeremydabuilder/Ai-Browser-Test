# PyBrowser

**The browser that finishes internet tasks.** Browse normally, or give Py a
goal and let it research, compare, and act across the web - as a Mission it
keeps track of, checks with you before anything real, and picks back up
whenever you return.

A real desktop web browser written in Python. It renders actual websites with
Qt WebEngine (Chromium) — no Node.js, no Electron, no npm, no mocked pages.

**Phase 1:** tabbed browsing, navigation, persistent history and bookmarks in
SQLite, keyboard shortcuts.
**Phase 2:** a Claude-powered AI agent that operates web pages through the
browser's structured API — see [`docs/ai_agent.md`](docs/ai_agent.md).
**Missions:** a goal survives across tabs and restarts as a Mission - its
pages, findings, decisions and progress kept together and resumable - see
the Mission Library sections of [`ARCHITECTURE.md`](ARCHITECTURE.md).

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
python -m unittest discover -s tests -v          # 1276 tests
python scripts/smoke_test.py                     # headless end-to-end run
python scripts/feature_check.py                  # 28-point feature checklist
python scripts/agent_demo.py                     # the research demo, offline
python scripts/smoke_test.py --url https://pypi.org   # also load a real site
python scripts/validate.py                       # full validation incl. real sites
python scripts/api_preflight.py                  # a real conversation per model
python scripts/real_sites.py                     # browser + agent vs. real sites
```

Every test in the suite scripts the model, which proves the agent loop and
proves nothing about whether the API accepts what the browser sends — that gap
is how a `thinking` parameter went out to a model that rejects it and broke
every AI feature. `scripts/api_preflight.py` closes it: for each model in the
picker it walks a whole conversation through the real client — the opening
request, a tool_use turn echoed back with a tool_result answering it, and a
follow-up after the assistant's text turn — and prints the API's own words on
any refusal. It needs a real credential and costs a few thousand tokens per
model. Run it after touching anything in `app/agent/claude_client.py` or the
model catalogue.

* **`ARCHITECTURE.md`** — how the layers fit together, the permission tiers,
  and where each unbuilt AI capability would go.
* **`tests/test_e2e_missions.py`** — whole Missions end to end against a real
  multi-page fixture site: a multi-source research/comparison mission that
  ends in a structured result, a "type routine fields, then stop for
  approval before submitting" mission, and a mission that hits a hostile
  page partway through and keeps only the real finding. Wired the same way
  `main_window.py` wires a live session (the action log and blocker state
  included), not just a single mocked tool call.
* **`tests/`** — 39 pure unit tests (URL parsing, SQLite stores, background
  writer, error mapping, navigation guard), 88 BrowserController tests, 60
  agent tests, 52 Phase 3 tests (shadow DOM, element targeting, multi-step
  tasks, prompt injection, find-in-page), 28 credential and key tests, and 37
  cost tests (request caching shape, token accounting, snapshot pruning), 28
  new-tab tests, 14 download tests and 9 AI-panel tests. All deterministic: the
  browser is real, the fixture server is local (`tests/fixture_server.py`), and
  the model is scripted (`tests/fake_claude.py`), so the agent suite needs no
  API key and makes no network calls.
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

## Performance

```bash
python scripts/perf_check.py     # measures the numbers below, on your machine
```

Real numbers, not estimates — measured with this script in the container this
was developed in (headless, offscreen, no GPU, software rendering; expect a
real desktop to start faster):

| What | Measured |
|---|---|
| Cold start (fresh interpreter → window shown, one tab loaded) | ~11s in this container; dominated by Qt/QtWebEngine's software-rendering fallback where there is no GPU — re-measure on a real desktop for a true baseline |
| Memory with one tab open | ~234 MB |
| Marginal memory per additional tab (5 more, all `about:blank`) | ~6 MB/tab |
| Loading a 1 MB single-page (one large text node) | ~1.7s, +~155 MB RSS |
| `browser_get_page_text` on that same page | returns the capped 20,000 chars, not the full 1,080,000 — the agent's context-limit cap (`ContextLimits`, `app/agent/config.py`) is doing its job, not silently dumping the whole DOM to the model |

The one number worth watching is the per-page memory jump on a large page —
QtWebEngine's own renderer process, not anything PyBrowser adds, but it is
real and worth knowing about before assuming "just open more tabs" is free.
Tab overhead itself is cheap: six tabs cost barely more than one.

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
| `Ctrl+F` | Find in page | | `Ctrl+G` / `Ctrl+Shift+G` | Find next / previous |
| `Ctrl+Shift+A` | Show AI agent | | `Ctrl+J` | Downloads |
| `Ctrl+,` | Settings | | | |
| `Ctrl+±` / `Ctrl+0` | Zoom | | `F11` | Full screen |

## Py

Py is PyBrowser's companion: a character whose state *is* the agent's state —
idle, reading, thinking, working, waiting for your approval, finished, or
stuck. Py appears in the agent panel header, with a line saying the same thing
in words, and at the top of the new-tab page, where clicking Py opens the
panel.

Py never celebrates a task that was stopped or that failed; those show `stuck`
("Looks like I got stuck.") rather than the finished face. What Py says is
chosen by state alone, so nothing from a web page can put words in Py's mouth.

The artwork is replaceable without touching any code: drop files named after
those states into [`app/ui/assets/mascot/`](app/ui/assets/mascot/). Only `idle`
is required, animated formats are supported, and the stand-in artwork lives in
a separate `placeholder/` folder so it cannot be confused for the real thing.
See [that folder's README](app/ui/assets/mascot/README.md).

## Appearance

PyBrowser follows your desktop's light or dark preference automatically. The
whole interface is generated from one small design system in
[`app/ui/theme.py`](app/ui/theme.py): a colour palette named by role
(`surface`, `line`, `muted`, `accent`), a 4px spacing scale, three corner
radii and a handful of control heights. Icons are drawn from
[`app/ui/icons.py`](app/ui/icons.py) rather than taken from the desktop's icon
theme, so PyBrowser looks the same on every machine.

Set `PYBROWSER_REDUCED_MOTION=1` (or `QT_REDUCED_MOTION=1`) to turn off the
sidebar animation.

```bash
python scripts/ui_shots.py /tmp/shots          # photograph every screen
python scripts/ui_shots.py /tmp/shots --dark   # ...in the dark theme
```

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

## The new tab page

New tabs open **PyBrowser New Tab**, a real page served from `pybrowser://newtab/`
inside the browser — instant, works offline, sends nothing anywhere. It carries
the search box, recent pages, bookmarks, a Recent Missions column and an entry
point to the AI panel.

The search provider is where searches *go*; it is not the home page. Change
either in **Tools → Settings** (`Ctrl+,`): new tabs can open PyBrowser New Tab,
your search provider's home page, a custom address, or a blank page.

Before a first Mission has been started, the page also shows a one-time
explainer card - four lines on what PyBrowser is, a "Try a demo mission"
button, and a dismiss. It never appears again once either happens.

## Missions

Give Py a goal, and it keeps the pages, findings and decisions for that goal
together as a Mission - resumable later, paused without losing anything, and
browsable as a real page at `pybrowser://missions/`. A Mission tracks a
current-stage progress label, an optional structured result (rendered as a
table or list where the text is shaped like one, not just prose), and a log
of what Py actually did - all of it persisted, so closing the browser costs
nothing. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for how the pieces fit
together.

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
  missions/                 goal-based workspaces: model, store, live service
  ui/agent_panel.py         transcript, activity, input, Allow/Deny
  ui/missions/              the mission card and the "start a mission" state
tests/                      unit tests (no GUI)
scripts/api_preflight.py    a real conversation per model, exactly as the browser sends it
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

**Setup — you do not need an API key.** Any of these works; the first two store
no secret in this browser at all:

```bash
# 1. Sign in with the Anthropic CLI (recommended - nothing is stored here)
ant auth login && python main.py

# 2. Use cloud credentials you already have
PYBROWSER_AGENT_BACKEND=bedrock AWS_REGION=us-east-1 python main.py
PYBROWSER_AGENT_BACKEND=vertex GOOGLE_CLOUD_PROJECT=my-project python main.py

# 3. A bearer token, or an API key, from the environment
export ANTHROPIC_AUTH_TOKEN="..."   # or ANTHROPIC_API_KEY="sk-ant-..."
python main.py
```

Or paste an API key into **Tools → Configure AI Agent…**, which stores it in
your OS keyring. That dialog shows every option and which one is currently in
use. Nothing is ever written to the database, the repository, or any config
file. Details: [`docs/ai_agent.md`](docs/ai_agent.md) §3.

**"Identity-linked" API keys.** Some Anthropic API keys are scoped to a
person rather than a workspace, and the API refuses a request from one of
those with a 400 unless it is told which workspace the request acts in. If
that happens, the browser says so plainly; add the workspace id (not a
secret — the same dialog, **Tools → Configure AI Agent…**) and it is sent as
the `anthropic-workspace-id` header on every request. An ordinary key needs
none of this and nothing changes for it. `ANTHROPIC_WORKSPACE_ID` sets the
same thing from the environment.

**Testing for free.** Anthropic is the default, but **Tools → Configure AI
Agent…** also offers **Groq** and **OpenRouter**, both of which have a free
tier — pick a provider from the dropdown, paste that provider's key, and
choose a model from its live model list. The agent loop (tools, Missions,
the approval gate) behaves identically no matter which provider is active;
see [`docs/ai_agent.md`](docs/ai_agent.md) §3a for how. A model that cannot
reliably support tool calling is flagged rather than silently offered, and
**Test Connection** in that same dialog runs one real request to prove a key
and model actually work before you start a task with them.

Open the panel with **Ctrl+Shift+A** (or Tools → Show AI Agent).

Sensitive actions — purchases, deletion, sending messages, credentials, payment
details, legal agreements, executable downloads — pause for an explicit
**Allow / Deny**, decided by the *browser*, not by the model.

### How cautious Py is

**Tools → Configure AI Agent…** also has an autonomy setting, independent of
provider or model:

- **Read-only** — Py can browse, read and compare, but anything that would
  change something is refused outright, never offered for approval.
- **Ask before every action** — Py asks before anything that writes, clicks
  or submits, not just purchases and deletions.
- **Standard** (default) — Py asks only before something sensitive and
  handles routine clicks and typing on its own; this is the behaviour the
  browser has always had.

This sits on top of the browser's own judgement of how consequential an
action is (`app/browser/safety.py`), not inside it - the classifier stays a
plain "how risky is this?" question with no notion of preference; autonomy
is the policy layered on top of that answer. `PYBROWSER_AGENT_AUTONOMY`
(`read_only` / `ask_always` / `standard`) sets the same thing from the
environment.

The agent also stops and explains itself if it gets stuck rather than
grinding on: repeating the exact same action back to back, clicking the same
broken element over and over, or opening far more tabs than one task should
need each end the task with a plain reason, the same recovery affordances
(Retry / Continue mission / Try another approach) a failed task always gets.

### Fixing a field instead of declining outright

A confirmation for typing text carries the proposed value in an editable box,
not just a description of the action. Approve/Deny still exist, but there's a
third option: correct the one thing that's wrong ("that's the wrong dates")
and let Py continue with the correction, instead of declining and re-explaining
the whole request from scratch in the chat. This is the one field the browser
knows is safe to hand back to you for editing — a password or payment value is
never put in that box, because the confirmation prompt is not a place to echo
a secret back to whoever just typed it.

### Keeping the cost down

An agent re-sends the whole conversation on every turn, so a multi-step task can
get expensive quickly. Three things are done about it, cheapest first:

1. **Prompt caching, on by default.** The tool schemas and system prompt are
   cached with a one-hour lifetime, and the growing conversation is cached
   automatically. Cached input bills at about a tenth of the normal rate and
   nothing about the answers changes. Anthropic measures this at a 2.5×–3.7×
   reduction in agent-loop cost. There is no setting for it because there is no
   reason to want it off.
2. **Effort**, defaulting to `medium` — in Anthropic's measurements that matched
   the model's own default accuracy at 70–85% of the cost. `low` is cheaper
   again and gives up a little accuracy.
3. **Model choice**, last, because a cheaper model is cheaper by being less
   capable. Claude Opus 5 (default), Sonnet 5, Haiku 4.5 and Fable 5 are
   offered, and the dialog says plainly what each gives up — Haiku 4.5 costs
   about a tenth of Opus 5 per question and answers 63% of them correctly
   against 92%, which suits short checkable tasks rather than long browsing
   sessions.

Model and effort live in **Tools → Configure AI Agent…**, or:

```bash
PYBROWSER_AGENT_MODEL=claude-sonnet-5 PYBROWSER_AGENT_EFFORT=low python main.py
```

The panel shows what the task in progress has cost — tokens, the share served
from cache, and a rough dollar estimate for models with a published price.
Token counts are exact; the money figure is labelled an estimate, and no figure
is shown at all for a model whose price is not published rather than a guessed
one.

To check that caching is really working against the live API (it fails
silently when it fails):

```bash
python scripts/cache_probe.py     # spends a few cents; exits non-zero on a miss
```

Full reasoning in [`docs/ai_agent.md`](docs/ai_agent.md) §10a.

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
