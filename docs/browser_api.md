# BrowserController — the browser automation API

`BrowserController` is the supported way to drive this browser from code. It is
a **general-purpose browser automation API**: it contains no AI, no model, no
network client, and no knowledge that a Phase 2 agent will ever exist.

```
    Browser UI  ─┐
                 ├─→ BrowserController ─→ Qt WebEngine / Chromium
    Automation  ─┘
```

The same API serves the window's own code, the test suite, and — eventually —
an agent. One audited surface, exercised by 88 tests, rather than three ways
of doing the same thing.

---

## 1. The boundary

Three properties define it, and each is enforced by a test in
`tests/test_browser_controller.py::ApiBoundaryTests`.

### Nothing Qt crosses it

Every method takes and returns plain data — strings, ints, dataclasses that
serialise to JSON. A caller never receives a `BrowserTab`, `QWebEngineView` or
`QWebEnginePage`, so it cannot reach around the API into Qt internals.

Tabs are addressed by a stable integer `tab_id`, **not** by index. Ids are
never reused: a stale id from a closed tab returns `UNKNOWN_TAB` rather than
silently acting on whatever tab now sits at that index — the same reasoning as
stale element references below.

### No arbitrary JavaScript

There is no `execute_script` method and there never should be. JavaScript *is*
how DOM inspection is implemented, but that script is ours
(`app/browser/page_script.js`), it is injected by the profile, and a caller can
neither supply nor influence it. Callers get semantic operations — click this
element, type into that field — not a shell on the page.

The script runs in Chromium's **ApplicationWorld**, an isolated JavaScript
world. It shares the DOM with the page but not the page's globals, so:

* a hostile page cannot read, call, replace or spy on our automation helpers,
  and cannot forge a page snapshot or make a click land somewhere else;
* we never stamp `data-*` marker attributes onto the page, so nothing the
  automation does is observable in the page's own DOM.

`BrowserTab.run_javascript()` still exists for the browser's own features and
for tests. It is not reachable through `BrowserController`.

### Everything is asynchronous and says so

Qt WebEngine is asynchronous end to end. The API does not pretend otherwise —
see §6.

---

## 2. Operations

Methods returning `BrowserFuture` are asynchronous; the rest read state Qt
already holds and return an `ActionResult` directly.

### Navigation

| Method | Returns | Notes |
|---|---|---|
| `navigate(url, tab_id=None)` | future | Resolves when the load finishes |
| `go_back(tab_id=None)` | future | `NO_HISTORY` if there is nowhere to go |
| `go_forward(tab_id=None)` | future | |
| `reload(tab_id=None)` | future | Invalidates all element references |
| `stop(tab_id=None)` | result | |

### Reading the page

| Method | Returns | Notes |
|---|---|---|
| `get_current_page(tab_id=None)` | result | Cheap: URL, title, loading, history, last error |
| `get_page_structure(...)` | future | The structured snapshot — see §3 |
| `get_page_text(tab_id=None, max_chars=…)` | future | Readable text only, no references |
| `inspect_element(ref, tab_id=None)` | future | Re-read one element; the cheap staleness check |

`get_page_structure()` takes `max_elements` (default 300), `max_text` (default
20 000) and `include_invisible` (default `False`). If the tab is mid-navigation
it waits for the load first, so a caller always gets the page that actually
ends up loaded rather than a race.

### Acting on the page

| Method | Returns | Notes |
|---|---|---|
| `click(ref, tab_id=None)` | future | Reports navigation / DOM change / new tab |
| `type_text(ref, text, submit=False, append=False, tab_id=None)` | future | Dispatches real `input`/`change` events |
| `submit(ref, tab_id=None)` | future | Submits the form containing `ref` |
| `set_checked(ref, checked=True, tab_id=None)` | future | Checkboxes, radios, switches |
| `select_option(ref, value, tab_id=None)` | future | Matches option label **or** value |
| `focus(ref, tab_id=None)` | future | |
| `scroll(direction, amount=None, tab_id=None)` | future | `up` / `down` / `top` / `bottom` |
| `scroll_to_element(ref, tab_id=None)` | future | |

### Waiting

| Method | Returns | Notes |
|---|---|---|
| `wait_for_load(tab_id=None, timeout_ms=30000)` | future | Immediate if already idle |
| `wait_for_element(role=…, name_contains=…, text_contains=…, timeout_ms=10000)` | future | For content that arrives late |

### Tabs

| Method | Returns | Notes |
|---|---|---|
| `open_tab(url=None, background=False)` | future | `effects.new_tab_id` names the new tab |
| `close_tab(tab_id=None)` | result | |
| `select_tab(tab_id)` | result | |
| `list_tabs()` | list of dicts | `tab_id`, `index`, `title`, `url`, `active`, `loading` |
| `tab_count()` | int | |

### Safety preview

| Method | Returns | Notes |
|---|---|---|
| `describe_action(action, ref=None, text="", url="")` | dict | Judges an action **without performing it** — see §7 |

### Signals

`action_completed(ActionResult)` fires for every completed operation — useful
for logging and, later, for showing agent activity in the UI.

---

## 3. Page structure format

Built from the DOM and ARIA roles — never from raw HTML, never from a
screenshot. **Raw HTML is not exposed by this API at all**: it is enormous,
mostly irrelevant, and it invites callers to write selectors instead of using
element references.

Fields are omitted rather than sent empty when they do not apply, so a checkbox
carries no `options` and a link carries no `placeholder`. The representation is
meant to be read by something with a token budget.

Real output, trimmed to five elements:

```json
{
  "url": "http://localhost:8000/",
  "title": "Fixture Home",
  "lang": "en",
  "snapshot_id": "s1",
  "tab_id": 1,
  "scroll": { "y": 0, "height": 2674, "viewport_height": 768, "at_bottom": false },
  "headings": [
    { "level": 1, "text": "Fixture Home" },
    { "level": 2, "text": "Controls" }
  ],
  "forms": [
    { "ref": "s1:f0", "name": "search-form",
      "action": "http://localhost:8000/results", "method": "get", "field_count": 8 }
  ],
  "elements": [
    { "ref": "s1:e0", "role": "link", "name": "Second page", "tag": "a",
      "visible": true, "in_viewport": true, "disabled": false,
      "href": "http://localhost:8000/second" },

    { "ref": "s1:e4", "role": "button", "name": "Clicked 0 times", "tag": "button",
      "visible": true, "in_viewport": true, "disabled": false },

    { "ref": "s1:e8", "role": "button", "name": "Disabled button", "tag": "button",
      "visible": true, "in_viewport": true, "disabled": true },

    { "ref": "s1:e13", "role": "searchbox", "name": "Search terms", "tag": "input",
      "visible": true, "in_viewport": true, "disabled": false,
      "value": "", "placeholder": "Search the fixtures",
      "input_type": "search", "field_name": "q", "form": 0 },

    { "ref": "s1:e16", "role": "combobox", "name": "Colour", "tag": "select",
      "visible": true, "in_viewport": true, "disabled": false,
      "value": "green", "field_name": "colour", "form": 0,
      "options": [
        { "label": "Red",   "value": "red",   "selected": false },
        { "label": "Green", "value": "green", "selected": true  },
        { "label": "Blue",  "value": "blue",  "selected": false }
      ] }
  ],
  "element_count": 21,
  "elements_truncated": false,
  "text": "Fixture Home Controls Second page …",
  "text_truncated": false
}
```

### Roles

Reported roles are semantic, not tag names: `link`, `button`, `textbox`,
`searchbox`, `textarea`, `combobox`, `listbox`, `checkbox`, `radio`, `switch`,
`slider`, `filepicker`, `heading`, `menuitem`, `tab`, `option`, `image`,
`generic`. An explicit ARIA `role` attribute always wins. `<input>` is mapped by
its `type`, with the specific type kept in `input_type`.

### Accessible names

Resolved in the practical accname order: `aria-labelledby` → `aria-label` →
associated `<label>` → button value → content → `title` → `placeholder` →
`name`. In the example above `"Search terms"` came from the field's `<label>`,
not from its placeholder.

### Python helpers

`PageStructure` is a dataclass with convenience accessors: `.links`,
`.buttons`, `.text_fields`, `.checkboxes`, `.radios`, `.selects`, plus
`.find(role=…, name_contains=…)`, `.first(…)` and `.by_ref(ref)`.
`.to_dict()` / `.to_json()` produce the form above.

### What is deliberately excluded

* Invisible elements, unless `include_invisible=True` (they are then marked
  `"visible": false`).
* Password values — always masked, with `"secret": true`, even after typing.
* Raw HTML, inline styles, scripts, and any element that is not interactive or
  a heading.

---

## 4. Element references and staleness

**This is the most important part of the design.** Web pages change under you;
an automation API that clicks the wrong element is worse than one that fails.

### Format

A reference looks like `s3:e12` — snapshot `s3`, element 12. Forms use
`s3:f0`. References are **scoped to the snapshot that produced them**, and each
`get_page_structure()` call mints a new snapshot id.

### How elements are tracked

At capture time the page script stores the **actual DOM node** in a registry in
the isolated world, together with a fingerprint of it. A reference resolves to
the exact node that was captured — never to whatever currently matches a
re-derived CSS selector. That distinction is the whole ball game: a selector
re-run after a re-render can match a different element, a stored node cannot.

The fingerprint is `tag + role + accessible name + type`. It deliberately
**excludes the element's value**, so typing into a field does not invalidate its
own reference.

### The five ways a reference can fail

Every failure mode is distinct, so a caller can react precisely:

| Code | Meaning | Recoverable |
|---|---|---|
| `STALE_SNAPSHOT` | The snapshot is unknown — the page was replaced, or it aged out of the registry (8 kept) | yes |
| `STALE_DOCUMENT` | The snapshot belongs to a different document — the page navigated | yes |
| `STALE_DETACHED` | The element was removed from the DOM | yes |
| `STALE_MUTATED` | The node still exists but now holds different content | yes |
| `UNKNOWN_REF` | No such index in that snapshot | yes |
| `INVALID_REF` | Malformed reference string — a caller bug | no |

`STALE_MUTATED` is the one that matters most. Consider the scenario from the
brief:

1. The caller inspects the page and receives `s1:e11` — "Removable target".
2. The page rewrites that button's label to "Completely different action".
3. The caller clicks `s1:e11`.

Nothing was removed, so a naive implementation would click it happily — and the
caller would have acted on something it never saw. This is exactly what
happens with recycled nodes in virtualised lists. Here, the fingerprint no
longer matches, so the click is refused:

```json
{
  "ok": false,
  "action": "click",
  "target": { "ref": "s1:e11", "role": "button", "name": "Removable target", "tag": "button" },
  "error": {
    "code": "STALE_MUTATED",
    "message": "That element now holds different content, so it is not the element that was captured. Inspect the page again.",
    "recoverable": true,
    "detail": ""
  },
  "effects": { "navigated": false, "dom_changed": false, "opened_tab": false, "…": null },
  "page": { "url": "http://localhost:8000/", "title": "Fixture Home", "loading": false,
            "can_go_back": false, "can_go_forward": false, "tab_id": 1, "load_error": null },
  "sensitivity": {},
  "duration_ms": 1
}
```

### Malformed references never reach the page

A reference is validated against `^s\d+:[ef]\d+$` in Python *before* any
JavaScript runs. Injection attempts like `"'; alert(1); //"` are rejected as
`INVALID_REF` without touching the page — and references are never string-
interpolated into script anyway; they are JSON-encoded arguments.

### Recovery

The contract is simple, and `result.should_reinspect` says when to apply it:

```python
result = browser.click(ref).wait()
if result.should_reinspect:          # any recoverable error
    structure = browser.get_page_structure().wait().data["structure"]
    # find the element again by role and name, then retry
```

References are **not** single-use. A snapshot stays valid while the document
and the elements are unchanged, so a caller can inspect once and perform
several actions.

---

## 5. Action results

Every operation returns the same `ActionResult` shape. A successful click that
changed the DOM without navigating:

```json
{
  "ok": true,
  "action": "click",
  "target": { "ref": "s1:e4", "role": "button", "name": "Clicked 0 times", "tag": "button" },
  "error": null,
  "effects": {
    "navigated": false,
    "dom_changed": true,
    "opened_tab": false,
    "url_before": "http://localhost:8000/",
    "url_after": "http://localhost:8000/",
    "new_tab_id": null,
    "scroll_before": null,
    "scroll_after": null
  },
  "page": {
    "url": "http://localhost:8000/",
    "title": "Fixture Home",
    "loading": false,
    "can_go_back": false,
    "can_go_forward": false,
    "tab_id": 1,
    "load_error": null
  },
  "sensitivity": { "level": "normal", "reasons": [], "requires_confirmation": false },
  "duration_ms": 225
}
```

`effects` is what makes the result actionable. `navigated` and `dom_changed`
answer "did my click go somewhere, redraw the page, or do nothing at all?"
without a screenshot or an HTML diff. A click that follows a link reports
`"navigated": true` with `url_after` set; a `target="_blank"` link reports
`"opened_tab": true` and a `new_tab_id`.

`page` is always present, on success and failure alike, so a caller never has
to make a second call to find out where it ended up.

### Error codes

Grouped by what a caller should do about them:

* **Re-inspect and retry** — `STALE_SNAPSHOT`, `STALE_DOCUMENT`,
  `STALE_DETACHED`, `STALE_MUTATED`, `UNKNOWN_REF`.
* **The element cannot do this** — `ELEMENT_DISABLED`, `ELEMENT_NOT_VISIBLE`,
  `ELEMENT_NOT_EDITABLE`, `ELEMENT_READONLY`, `ELEMENT_NOT_CHECKABLE`,
  `ELEMENT_NOT_SELECTABLE`, `OPTION_NOT_FOUND`, `NO_FORM`.
* **The request was impossible** — `INVALID_REF`, `INVALID_URL`, `NO_HISTORY`,
  `NO_TAB`, `UNKNOWN_TAB`.
* **The page or the timing failed** — `LOAD_FAILED`, `TIMEOUT`,
  `SCRIPT_FAILED`, `UNSUPPORTED`.

`error.recoverable` is the single most useful bit for an automation loop, and
`error.message` is a plain sentence — no `ERR_` codes, no stack traces. Any
Chromium detail goes in `error.detail`.

---

## 6. Asynchronous model

Every asynchronous operation returns a `BrowserFuture` that resolves **exactly
once**, and the caller picks how to observe it:

```python
browser.click(ref).then(lambda result: ...)      # callback  (preferred)
browser.click(ref).finished.connect(handler)     # Qt signal
result = browser.click(ref).wait()               # blocking — scripts and tests only
```

`wait()` spins a nested `QEventLoop`. That is right for a test or a one-off
script and wrong inside a GUI slot, where re-entering the event loop invites
reentrancy bugs — so it is documented as such rather than being the default.

Failures detected synchronously (an invalid URL, a malformed reference) still
return a future; it is simply already resolved. Callers need only one code path.

### How completion is decided

| Operation | Resolves when |
|---|---|
| `navigate`, `reload`, `open_tab(url)` | the load finishes (or fails) |
| `go_back`, `go_forward` | the load finishes **or** the URL changes and settles |
| `click`, `type_text`, `submit`, `set_checked`, `select_option` | the settle window closes — see below |
| `get_page_structure`, `get_page_text` | the page returns the data |
| `wait_for_element` | a poll matches, or the timeout fires |

Back/forward gets its own rule because a move served from Chromium's
back-forward cache emits **no load signals at all** — waiting only on
`loadFinished` would hang for ever.

### The settle window

A click has three possible outcomes and none of them is knowable at the moment
the click returns. So after acting we watch for up to `SETTLE_MAX_MS` (2.5 s),
resolving as soon as either a load completes or the DOM goes quiet for
`SETTLE_QUIET_MS` (220 ms):

* it started a navigation → wait for the load, report `navigated`;
* it changed the DOM in place → detected via a `MutationObserver` revision
  counter, report `dom_changed`;
* it opened a tab → detected from the engine's own "new window" signal, which
  fires *synchronously inside* `element.click()`, report `opened_tab`;
* it did nothing observable → both flags stay false.

**Every** future carries a timeout, so a page that never finishes loading
produces a `TIMEOUT` result rather than a caller that waits for ever.

### Content that arrives late

The settle window is short by design — it answers "what did my click do?", not
"has the site finished fetching?". For content that arrives later, use
`wait_for_element`, which polls a cheap predicate and creates no references:

```python
browser.wait_for_element(role="button", name_contains="Results", timeout_ms=8000)
browser.wait_for_element(text_contains="Order confirmed")
```

---

## 7. Sensitivity classification

`app/browser/safety.py` **only classifies**. It never blocks, prompts, or
gates anything — implementing confirmation is Phase 2 work, and baking the
policy in now would settle a question before the UI that has to present it
exists.

Three levels:

| Level | Meaning | `requires_confirmation` |
|---|---|---|
| `normal` | Reading, scrolling, ordinary navigation | `false` |
| `elevated` | Writes something, leaves a trace, spends nothing | `false` |
| `sensitive` | Money, identity, destruction, or legal consent | `true` |

`describe_action()` answers the question **before** the action runs — this is
the hook a future agent uses to decide whether to ask the user first:

```json
{
  "action": "click",
  "ref": "s1:e12",
  "target": { "ref": "s1:e12", "role": "button", "name": "Buy now", "tag": "button",
              "visible": true, "in_viewport": true, "disabled": false },
  "level": "sensitive",
  "reasons": ["may spend money or place an order"],
  "requires_confirmation": true
}
```

Every `ActionResult` also carries the assessment, so the record of what an
automated caller did is auditable after the fact.

### What should eventually require confirmation

Flagged `sensitive` today:

* **Spending money** — buy, purchase, order, checkout, pay, subscribe, donate, bid.
* **Destruction** — delete, remove, erase, deactivate, close account, cancel.
* **Publishing or sending** — send, post, publish, reply, comment, share, message.
* **Credentials and security settings** — password fields, `autocomplete` of
  `current-password` / `new-password` / `one-time-code`, 2FA, API keys, revoking
  permissions.
* **Payment and identity data** — `cc-number` / `cc-csc` autocomplete, fields
  named for card numbers, CVV, IBAN, SSN; and any text that passes a Luhn check.
* **Legal consent** — "I agree", accept terms, privacy policy, e-sign.
* **Moving money** — transfer, withdraw, wire, refund.
* **Downloads of executable files** — `.exe`, `.msi`, `.sh`, `.dmg`, `.apk`, and
  friends, or any link with a `download` attribute.

Flagged `elevated`: signing in or up, saving, updating, uploading, submitting a
form, typing into any field, changing a checkbox or dropdown.

Two honest caveats. The classifier is **heuristic**: matching English against an
accessible name will miss a localised "Kaufen" and will over-flag a link called
"Delete draft". It biases toward asking. And it is **not a security boundary** —
the real boundary is that a caller cannot execute arbitrary JavaScript, cannot
touch Qt widgets, and cannot grant a page a permission. Browser-level permission
prompts (camera, microphone, location) are not exposed to automation at all;
they remain the user's, handled in `app/browser/web_page.py`.

---

## 8. How a future agent would use this

**Pseudocode only. None of this is implemented, and no Claude/Anthropic code
exists anywhere in this repository.**

Task: *"Search Google for cats."*

```
# The agent's tool definitions map 1:1 onto controller methods.
# The agent never sees JavaScript, Qt, or a CSS selector.

navigate("https://www.google.com")
        → { ok: true, page: { url: "https://www.google.com/", title: "Google" } }

structure = get_page_structure()
        → { url, title, headings, forms,
            elements: [
              { ref: "s1:e7", role: "searchbox", name: "Search",
                placeholder: "Search", form: 0 },
              { ref: "s1:e9", role: "button", name: "Google Search", form: 0 },
              ...
            ] }

# The model picks a ref by role and accessible name from that list.
# It cannot invent a selector, because it was never given selectors.

preview = describe_action("type_text", ref="s1:e7", text="cats")
        → { level: "elevated", requires_confirmation: false }
# Not sensitive → proceed without asking.

type_text("s1:e7", "cats", submit=true)
        → { ok: true, effects: { navigated: true, url_after: ".../search?q=cats" } }

# effects.navigated is true, so every ref from s1 is now stale by contract.
wait_for_element(role="link", timeout_ms=8000)
structure = get_page_structure()          # fresh snapshot s2

# Read the answer out of the structure; scroll and re-inspect for more.
results = [e for e in structure.elements if e.role == "link"]
```

And the shape of the confirmation flow, for a task that touches money:

```
preview = describe_action("click", ref="s4:e22")     # "Place order"
        → { level: "sensitive",
            reasons: ["may spend money or place an order"],
            requires_confirmation: true }

if preview.requires_confirmation:
    ask_the_user(preview.reasons, preview.target.name)   # Phase 2 builds this
    if not approved: stop and explain

click("s4:e22")
```

The loop the agent runs is always the same:

1. `get_page_structure()` — see what is on the page.
2. Choose an element by role and accessible name.
3. `describe_action(...)` — confirm first if it comes back `sensitive`.
4. Act.
5. Read `effects`: navigated or DOM changed → **re-inspect**; `should_reinspect`
   → re-inspect and retry; otherwise continue.

---

## 9. What Phase 2 still needs

Not built, deliberately:

* `app/agent/tools.py` — tool schemas wrapping these methods.
* `app/agent/session.py` — the turn loop, message history, cancellation.
* `app/agent/claude_client.py` — the API transport, on a worker thread.
* `app/agent/confirm.py` — the policy that turns `requires_confirmation` into
  an actual prompt, and the record of what the user approved.
* `app/ui/agent_panel.py` — the panel; `MainWindow.set_side_panel()` is already
  there to receive it.
* Credential storage for an API key — OS keyring, **not** the SQLite file.

## 10. Tests

`tests/test_browser_controller.py` — 88 tests against the deterministic fixture
server in `tests/fixture_server.py`. No external site is involved, so the suite
is reproducible offline.

```bash
QT_QPA_PLATFORM=offscreen python -m unittest tests.test_browser_controller -v
```

Coverage: page inspection, roles and accessible names, forms, disabled and
invisible elements, element references, all five staleness modes, recovery
after staleness, clicking, typing, checkboxes, radios, dropdowns, form
submission, scrolling, navigation, redirects, back/forward, reload, dynamic DOM
changes, script-generated elements, delayed content, multiple tabs, tab
switching, tab identity after closes, `target="_blank"`, error handling,
sensitivity classification, the async model, and the API boundary itself.
