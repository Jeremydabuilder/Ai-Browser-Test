# Phase 2 — AI agent architecture (design notes)

> **Superseded.** Phase 2 is implemented. This file is kept for the record
> of how it was designed before it was built; the description of what
> actually shipped lives in **[`ai_agent.md`](ai_agent.md)**, and the
> browser API it sits on is in **[`browser_api.md`](browser_api.md)**.
>
> The design below held up, with two changes worth noting:
>
> * `BrowserController` moved to `app/browser/` during the preparation
>   pass — it turned out to be a browser abstraction, not an agent one.
> * `app/agent/interfaces.py` (the Protocol sketches) was deleted once the
>   real implementation existed, rather than left to drift out of date.

This document records the design the Phase 1 code was built to accommodate, so
that adding the agent would be additive rather than a rewrite. It was.

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

## What already exists

Phase 1's hardening pass built the control surface, because it was needed for
the browser's own tests regardless of any AI:

See **[`browser_api.md`](browser_api.md)** for the full reference. In short,
`app/browser/controller.py` provides **`BrowserController`**, a plain browser
API with no AI in it:

```python
navigate(url)          go_back()      open_tab(url)     get_current_page()
reload()               go_forward()   close_tab(index)  get_page_structure(cb)
stop()                 select_tab(i)  list_tabs()       get_text(cb)
click(ref)             type_text(ref, text, submit)     scroll(direction)
```

`get_page_structure()` returns a `PageStructure`: URL, title, headings, forms,
readable text, scroll position, and a list of `PageElement`s (role, accessible
name, value, placeholder, disabled, visible, href, options…) each carrying a
snapshot-scoped reference like `s3:e12`. Actions take those references. A
caller never supplies a CSS selector, so it cannot invent one that does not
exist, and a reference to an element that was removed *or recycled for
different content* fails cleanly rather than clicking the wrong thing.

Also already built: structured `ActionResult`s with machine-readable error
codes, `BrowserFuture` for the async model, and `safety.py` /
`describe_action()`, which classify how consequential an action is **before**
it runs — the hook Phase 2's confirmation policy will read.

This is what the validation harness and the 88 controller tests use to drive
the browser.

## Modules Phase 2 still needs

```
app/agent/
  interfaces.py     PageSnapshot, PageElement, ConfirmationPolicy,     [exists]
                    AgentTransport
  tools.py          Claude tool definitions + dispatch to BrowserController
  session.py        the agent loop: message history, tool turns, cancellation
  claude_client.py  Anthropic API transport (streaming)
  confirm.py        turns requires_confirmation into an actual prompt
                    (classification itself already lives in app/browser/safety.py)
app/ui/
  agent_panel.py    chat transcript, input box, step list, confirm prompts
```

Note that `controller.py` moved out of `app/agent/` and into `app/browser/`:
it turned out to be a browser abstraction, not an agent one.

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

Each maps one-to-one onto an existing `BrowserController` method. The mapping
is deliberately mechanical — the agent layer adds a schema and a safety check,
not new browser capability:

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

## Gotcha inherited from Phase 1

`BrowserController.click()` clicks through injected JavaScript, which carries no
user activation. Chromium's History Manipulation Intervention marks the
resulting history entry as skippable, so one `go_back()` does **not** reliably
undo one `click()`. An agent doing multi-step navigation should track its own
trail of URLs rather than counting on back(). This is documented on the method
and was found by the Phase 1 validation pass.

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
