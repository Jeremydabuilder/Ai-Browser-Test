# Phase 2 — AI agent architecture (design only)

Nothing described here is implemented. This document records the design the
Phase 1 code was built to accommodate, so that adding the agent is additive
rather than a rewrite.

## Target layout

```
┌─────────────────────────────────────────────┐
│ ←  →  ↻  ⌂  [ address bar ]              ☆ │
├─────────────────────────────┬───────────────┤
│                             │  AI AGENT     │
│         WEB PAGE            │               │
│       (TabManager)          │  What can I   │
│                             │  do for you?  │
│                             │  [ message ]  │
└─────────────────────────────┴───────────────┘
```

`MainWindow.set_side_panel(widget)` already exists and installs a widget into
the right half of the existing `QSplitter`. That is the only Phase 1 code the
UI work needs to touch.

## Proposed modules

```
app/agent/
  interfaces.py     PageSnapshot, PageElement, BrowserController,      [exists]
                    ConfirmationPolicy, AgentTransport
  controller.py     BrowserController implemented over BrowserTab
  page_reader.py    injected JS that builds the snapshot
  tools.py          Claude tool definitions + dispatch to the controller
  session.py        the agent loop: message history, tool turns, cancellation
  claude_client.py  Anthropic API transport (streaming)
  safety.py         risk classification + ConfirmationPolicy implementation
app/ui/
  agent_panel.py    chat transcript, input box, step list, confirm prompts
```

## How the agent sees a page

**DOM and accessibility first, screenshots only if needed.** A small JavaScript
snippet is injected via the existing `BrowserTab.run_javascript()` and returns a
`PageSnapshot`: URL, title, readable text, and a list of interactive elements —
each with a role, an accessible name, and a short opaque handle (`e12`).

Why handles instead of CSS selectors: the model can only act on elements that
actually exist, prompts stay small (a few hundred tokens instead of a megabyte
of HTML), and there is no selector-guessing failure mode. Element handles are
stored in a per-snapshot map on the Python side; a stale handle after a page
change is a clean, catchable error rather than a mis-click.

Screenshots (`QWebEngineView.grab()`) stay available as an optional extra for
genuinely visual questions ("which of these is the sale badge?").

## Tools exposed to Claude

Each maps one-to-one onto a `BrowserController` method, which in turn is a thin
wrapper over `BrowserTab`:

| Tool | Effect | Risk |
|---|---|---|
| `read_page` | return the current `PageSnapshot` | safe |
| `navigate(url)` | load a URL | safe / elevated when leaving the site |
| `click(ref)` | click an element by handle | depends on the element |
| `type_text(ref, text, submit)` | fill an input, optionally submit | elevated |
| `scroll(delta_y)` | scroll the viewport | safe |
| `go_back()` | history back | safe |
| `extract(instruction)` | pull structured data out of the page | safe |

## Confirmation before sensitive actions

`ConfirmationPolicy` is a single, auditable place that classifies an action as
`SAFE`, `ELEVATED` or `SENSITIVE` and can block the loop until the user answers
in the panel. Purchases, payments, deletions, sending messages and account
changes are `SENSITIVE` and always require an explicit click. The agent never
talks to the UI directly — it asks the policy — so the rule cannot be
accidentally bypassed by a new tool.

## Threading

The Claude API call is network I/O and must not block the Qt event loop. Plan:
run the transport on a `QThread` (or `QNetworkAccessManager`) and marshal
results back with queued signals. All WebEngine interaction must happen on the
GUI thread, so the agent loop posts actions to it rather than calling into Qt
from the worker.

## Example task

> "Find me the cheapest laptop under $800."

1. `navigate` to a shopping site
2. `read_page` → snapshot with the search box as `e4`
3. `type_text(e4, "laptop", submit=True)`
4. `read_page` → result list
5. `extract("product names and prices")`
6. possibly `scroll` and repeat for more results
7. compare in the model, present the answer with links

No purchase happens without a `SENSITIVE` confirmation.

## Open questions

- Where the API key lives (env var vs. OS keyring — **not** the SQLite file).
- Per-tab agent sessions versus one session for the window.
- Token budget: how aggressively to truncate page text on large pages.
- Whether the agent gets its own tab so it cannot disturb the user's browsing.
