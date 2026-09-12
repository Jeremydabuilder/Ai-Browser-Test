# AI-browser capability audit

An internal audit of PyBrowser against five capabilities every "AI browser" is
now expected to have, done to separate what is actually implemented from what
is only implied by the pitch. Constraints this audit was run under: do not
invent features, do not claim memory is broader than it is, do not build
invasive background profiling, do not weaken MCP/agent safety, do not
duplicate the tab model, do not rewrite the agent architecture.

## Matrix

| Capability | Status | Existing implementation | Missing work | Tests |
|---|---|---|---|---|
| 1. Sidecar/Chatbot Assistant | Partially → mostly implemented | Persistent `AgentPanel` (`Ctrl+Shift+A`), one `AgentSession` per window whose message history survives navigation and panel hide/show; `browser_get_page`/`browser_get_page_text`/`browser_find_elements` all take an optional `tab_id`, and `browser_list_tabs` enumerates every open tab, so the model *can* read any background tab, not only the active one | No first-class "compare tabs" affordance for the *user* - only composable primitives the model already has. Added this pass: a UI action that hands a multi-tab selection to Py | `test_agent_panel.py`, `test_missions.py` (briefing), new `test_multi_tab_ask_py.py` |
| 2. Agent Mode/Task Automation | Fully implemented | Full click/scroll/type/submit/select/checkbox/navigate/tab tool set in `app/agent/tools.py` dispatched through `BrowserController`; every call is classified by `ToolRegistry.assess()` (fail-closed: unclassified → elevated) and gated by the same `ConfirmationRequest`/approval UI regardless of whether a Mission or a Routine replay is driving it | None identified as high-value and missing | `test_missions.py` (ToolSurfaceTests, BriefingSafetyTests), `test_routines.py`, `test_agent_panel.py` (ConfirmationBarTests), MCP Phase 2/3 suites |
| 3. Contextual Browser Memory | Partially implemented, by design | Mission is the memory unit: goal/status/progress/result/constraints persisted in SQLite (`MissionStore`), plus pages/findings/decisions/questions/actions; survives restart; Mission Library gives soft-delete, permanent delete, and restore | No cross-Mission memory (each Mission's briefing is scoped to itself only - deliberate, not a gap); no reading of history/bookmarks as implicit context (none existed, none added). Added this pass: an explicit, honest "what Py remembers" note with a link into the review/delete UI, so the scope is stated rather than assumed | `test_missions.py` (BriefingCompositionTests, DeletionTests), `test_mission_library.py`, new `test_mission_memory_note.py` |
| 4. Smart Tab and Workflow Management | Partially → mostly implemented | Horizontal/vertical tab layouts (Phase 2 of this workstream), tab search with title/domain filter | No grouping, pinning, or multi-select existed before this pass. Added this pass: multi-select in Search Tabs, duplicate-tab detection (same URL) with a one-click close, "Ask Py about selected tabs" (summarize/compare), "Start a Mission from these tabs". Not added: topic-based auto-grouping (would need a real classification step, not just a URL match - left as a next bet, see below) | `test_vertical_tabs.py`, `test_tab_search.py` (MultiTabActionsTests), `test_multi_tab_ask_py.py` |
| 5. Native AI Integration | Fully implemented | One dispatch loop (`AgentSession._next_tool` → `ToolRegistry.run`) for every tool, native and MCP alike; the model's tool schema list is built fresh each turn from `ToolRegistry.schemas()` + `McpConnectionManager.schemas()`, so connected MCP tools appear the same way native ones do; `browser_list_tabs`, the Mission briefing, and MCP tool exposure all give the model live state without any copy/paste step | None identified as high-value and missing | `test_mcp_integration.py`, `test_mcp_phase2.py`, `test_mcp_phase3.py` |

## What was already complete

- The agent tool layer was never actually restricted to the active tab -
  `tab_id` has been a parameter on every content-reading tool since Phase 1,
  and `browser_list_tabs` already exposes every open tab's id/title/url. The
  "sidecar can only see the current tab" limitation many AI browsers have
  did not exist here; it was just never surfaced as something the *user*
  could trigger directly.
- Automation approval is uniform: Missions and Routines do not get their own
  weaker confirmation path. `assess()`'s fail-closed default (an
  unclassified tool is "elevated", never silently allowed) was already in
  place and untouched by this pass.
- Mission memory was already explicit and locally reviewable (Mission
  Library: soft delete, permanent delete, restore) - the gap was that
  nothing *said* this out loud anywhere in the UI.
- The single dispatch loop and live tool-schema assembly (native + MCP in
  one list, rebuilt every turn) were already the actual architecture, not
  an aspiration.

## What was implemented this pass

Priority order followed: multi-tab Ask Py, smart tab grouping/summarization,
explicit memory controls, then workflow/native-context polish (no changes
needed there - see below).

1. **Multi-tab Ask Py** (`app/ui/tab_search.py`, `app/ui/main_window.py`) -
   Search Tabs now supports multi-select; "Ask Py about selected tabs…"
   hands the picked tabs to Py as a prompt naming each tab's stable
   controller `tab_id`, so Py summarizes (one tab) or compares (several) by
   calling `browser_get_page_text` itself. No new agent tool, no second tab
   store - it composes `browser_list_tabs` + `browser_get_page_text`
   (already available to the model) with one UI affordance.
2. **Duplicate-tab detection** (`app/ui/tab_search.py`) - tabs sharing the
   exact same URL (internal pages and `about:`/`data:` pages excluded) are
   flagged with a "Close duplicates" action that keeps the first and closes
   the rest.
3. **Turn selected tabs into a Mission** (`app/ui/main_window.py`) -
   `MissionService.start()` (already existed, used everywhere else a Mission
   starts) creates a Mission named after the picked tabs' titles; Py is then
   asked, through the ordinary `ask()` path, to read each tab and note
   useful sources - no bypass of the approval gate, since reading a page and
   recording a local note are the same tools/permissions they always were.
4. **Explicit memory note** (`app/ui/missions/mission_picker.py`) - the
   Mission-start screen now states plainly what Py remembers (Mission
   goals/findings/sources/decisions) and what it does not (ordinary
   browsing), with a link straight into the Mission Library's existing
   review/delete/restore controls.
5. **A genuine bug found and fixed while building the above**:
   `VerticalTabList._rebuild()` (from the tab-layout work earlier this
   workstream) scheduled old row widgets for `deleteLater()` without
   detaching them first, so a stale row could still be painted, overlapping
   the new one built at the same position, until the next event-loop turn.
   Fixed by hiding and reparenting immediately.

## What remains partial (and why it was left alone)

- **Topic-based auto-grouping** ("AI-suggested groups") - not implemented.
  Duplicate detection is a simple, honest same-URL match; grouping by topic
  would need an actual classification step (either a heuristic worth
  trusting or a model call per open tab), and doing that convincingly was
  judged lower value than shipping duplicate detection and the multi-tab
  actions cleanly. Left as the top candidate for a follow-up pass, not
  quietly skipped.
- **Cross-Mission memory** - deliberately absent, not partial. Each Mission's
  briefing draws only from its own findings; there is no shared memory
  object spanning Missions. This matches the "do not claim memory is
  broader than it is" constraint - branching a Mission copies its rows into
  a new one, it does not link them live.
- **Pinned tabs** - not implemented in this pass or the prior tab-layout
  pass; the tab model does not yet distinguish a pinned tab from an ordinary
  one, and adding that cleanly is a model change, not a UI-only one like the
  additions above.

## Screenshots

See the chat for: the Search Tabs dialog with multiple tabs selected and the
"Ask Py about selected tabs…" / "Start a Mission from these…" actions visible,
the duplicate-tabs notice with "Close duplicates", and the Mission-start
screen's memory note.

## Tests added this pass

- `tests/test_multi_tab_ask_py.py` (13 tests) - the multi-tab-ask-Py prompt
  content, tab-id resolution, and index-based tab closing.
- `tests/test_tab_search.py` - extended with `MultiTabActionsTests` (7 tests):
  multi-select gating, duplicate detection, and safe duplicate-closing.
- `tests/test_mission_memory_note.py` (3 tests) - the memory note's wording
  and its link into the Mission Library.

## Full suite result

1707 tests passing before this pass (MCP Phase 3 + tab-layout work); with
this pass's 23 new tests, run in full before considering this audit done -
see the chat for the final count.

## Updated product-readiness score

Carried over from the honest self-assessment this project has kept
throughout (see `docs/ai_agent.md` §9 and the website's own capability
audit): **not production-ready, and not claimed to be.** What changed this
pass is narrower and more concrete: the five marketed AI-browser categories
are now backed by real, tested code rather than partly aspirational, and the
one category that was genuinely thin (multi-tab reasoning as something a
user can actually trigger) has a real, tested affordance now. Native
integration and automation safety were already solid and remain untouched.
Memory is now honestly described in the product itself, not just in this
document.

## Top 3 next product bets

1. **Topic-based tab grouping**, built on the duplicate-detection groundwork
   already in `tab_search.py` - the highest-value gap this audit found that
   was deliberately not attempted here, because doing it as a real
   classification step (not a keyword hack) is a scoped project of its own.
2. **PyBrowser-as-MCP-server** (explicitly out of scope for this pass, per
   standing instruction) - the natural next step once tab/Mission/MCP
   foundations are this settled: let another AI (ChatGPT, another Claude
   session) drive PyBrowser the same way PyBrowser's own agent does, through
   the same `BrowserController`/confirmation gate.
3. **Pinned tabs**, as the one still-missing piece of "modern tab
   organization" that touches the shared tab model rather than only its
   views - worth doing once there is a clear answer for how a pin
   interacts with Mission-tied tabs and Routine-recorded tabs, so it does
   not get built twice.
