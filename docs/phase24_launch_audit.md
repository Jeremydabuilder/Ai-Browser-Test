# Phase 24 — Final UX, Performance, Reliability & Launch Audit

This is a consolidation audit across all 23 prior phases, not a new
feature phase. It documents what was found, what was fixed, what was
measured, and an honest launch recommendation. Where a claim below is
backed by a measurement or a specific test file, that is named; where it
is a code-review judgment rather than a live measurement (real Windows/
macOS hardware, a screen reader, real users), that is stated plainly.

## Part 1 — Product capability matrix

| Capability | Status | UX quality | Reliability | Remaining risk |
|---|---|---|---|---|
| Normal browsing (tabs, nav, history, bookmarks, downloads) | Production-ready | Good | High | Low |
| Tabs: groups/pins | Production-ready | Good | High | Low |
| Vertical tabs | Preview-ready | Good | High | Low - narrower audience, less real-world mileage |
| Ask Py (single-tab agent) | Production-ready | Good | High | Provider outages (handled, see Part 10) |
| Multi-tab Ask Py / @Context | Preview-ready | Good | Good | Depends on provider context limits |
| Missions (single-agent research) | Preview-ready | Good | Good | Long-running Mission + crash interaction (see Part 11) |
| Multi-agent Missions / DAG execution | Experimental | Fair | Fair | Newest, least battle-tested subsystem; coordinator complexity |
| MCP client (connect to external tools) | Preview-ready | Fair | Good | Discoverability - buried under Tools ▸ (menu now demoted from top-level, see Part 4) |
| MCP server (expose PyBrowser to external AI) | Experimental | Fair | Good | Off by default (correct), but low real-world testing surface |
| External AI clients (OpenAI/Groq/OpenRouter/Gemini via compat layer) | Preview-ready | Good | Good | Per-provider quirks less tested than Anthropic path |
| PDFs/files/images in context | Production-ready | Good | High | Low |
| Highlights | Production-ready | Good | High | Low |
| Skills library | Preview-ready | Fair | Good | Discoverability |
| Workflow recorder | Experimental | Fair | Fair | Real-world site variability (selectors drifting) |
| Scheduling (Task Center) | Preview-ready | Good | Good | Low |
| Watches | Preview-ready | Fair | Good | Low |
| Semantic history / local RAG | Preview-ready | Good | Good | Opt-in (correct default) |
| Knowledge Graph | Experimental | Fair | Good | New in Phase 19, limited mileage |
| Visual computer-use fallback | Experimental | Fair | Fair | Vision-provider-gated, fallback path only |
| Privacy firewall / injection defense | Production-ready | Good | High | Re-audited in Part 12 below |
| Local models (Ollama/LM Studio/generic) | Preview-ready | Fair | Fair | Depends entirely on the user's local setup |
| Workspaces / profile isolation | Preview-ready | Good | Good | Newer, moderate mileage |
| Encrypted sync | Experimental | Fair | Fair | Folder-based, no real multi-device field testing |
| Collaboration | Experimental | Fair | Fair | Advisory-only role model (disclosed), newest subsystem |
| Updater / release system | Preview-ready | Good | Good | No real signed/notarized build yet (disclosed) |
| Extension support | Not built | — | — | Phase 23 was never run in this session - no code exists |

**Overall reading:** the core browser and the single-agent AI loop (Ask Py, Missions, PDFs/context, Highlights) are genuinely solid and ready for real daily use. Everything built from roughly Phase 16 onward (automation recorder, multi-agent DAG, MCP server, Knowledge Graph, sync, collaboration) is real, tested code but has had far less exposure than the core - "Experimental"/"Preview-ready" is the honest label, not "Production-ready."

## Part 2 — First-run experience (audited, not changed)

Traced the actual code path: `main.py` → `app.startup_checks.run_all()` (Phase 22 - fails fast with a friendly dialog only on a genuinely broken environment) → `MainWindow` construction → `show_first_run_if_needed()`. The first-run dialog (`app/ui/onboarding.py`) is three screens (Welcome → optional provider setup → try a Mission), each skippable, and does not gate on MCP, sync, or local models at all - a user can start browsing immediately by clicking Skip on screen one. This already matches Part 2's target flow; no changes needed.

## Part 3 — Onboarding (audited, not changed)

The onboarding copy says "the browser that finishes internet tasks" and covers exactly two concepts (Ask Py, Missions) across its three screens - not five. It does not mention Connected Tools (MCP) at all, which is correct per the phase's own instruction ("Advanced systems stay discoverable later"). No changes needed.

## Part 4/19 — Information architecture (fixed)

**Settings dialog** (`app/ui/settings_dialog.py`) already has only 5 tabs - General, Connected Tools, External AI Access, Privacy/Memory/Knowledge, Privacy & Security - nowhere near the "30 tabs" risk the phase warns about. Sync and Collaboration are their own dialogs (not Settings tabs), which is reasonable since they are optional, occasional-use flows.

**Tools menu was the real problem**: 15 flat top-level items after 23 phases (Configure AI Agent, Agent Diagnostics, 3 "add to context" actions, 4 library dialogs, Sync, Collaboration, Skills, Task Center, Record Workflow, Watches, Run as Multi-Agent Mission, Teach Py). Fixed by grouping into four submenus - **Add to Context**, **Libraries**, **Automation**, **Collaboration** - cutting the top-level Tools menu to 7 items (Settings, Show AI Agent, Configure AI Agent, Agent Diagnostics, plus the four submenus). No action, shortcut, or slot changed - purely a menu reorganization, verified against the existing menu-dependent tests (`test_shortcuts_help`, `test_mission_library`, `test_task_center_ui`, `test_automation`, `test_multi_agent_integration`, `test_highlights_integration` - 150 tests, all still green).

## Part 5 — Command / quick-action surface (evaluated, not built)

A dedicated `Ctrl+Shift+K` "Search Tabs" dialog already exists and is the closest thing to a command palette today. A true unified command palette ("Start Mission", "Run Skill", "Switch Workspace", "Search Knowledge" all in one launcher) does not exist. Per the phase's own instruction ("do not add one purely for trendiness"), and given this is a consolidation phase, this was evaluated and left as a **backlog item**, not built now - the individual entry points (Tab Search, Mission Library, Skills Library, Workspace switcher, Knowledge search) already exist and are reachable, just not unified.

## Part 6/7/8 — Performance & memory benchmarks (measured)

Measured directly in this session's Linux sandbox (offscreen Qt platform, software rendering, `QTWEBENGINE_DISABLE_SANDBOX=1` - a container-specific requirement, not representative of a real desktop's exact numbers, but directionally real and reproducible):

| Metric | Measured |
|---|---|
| Python + Qt/PySide6/app import | 0.40s |
| Database + BrowserProfile construction | 0.05s |
| MainWindow construction (1 tab) | 0.13s |
| **Warm construct-to-window total** | **~0.18s** |
| Peak RSS after MainWindow (1 tab) | 277 MB |
| Peak RSS after 10 tabs (`about:blank`) | 283 MB (+6 MB for 9 more tabs) |

**Caveats stated honestly**: this is the *main process's* RSS only (`RUSAGE_SELF`); Chromium's actual per-tab renderer processes run as separate OS processes not captured here, so real total system memory for 10 real (non-blank) tabs will be meaningfully higher than +6MB - this benchmark demonstrates the main process itself is lightweight and that opening tabs doesn't balloon *it*, not a full system memory budget. A real measurement on Windows/macOS with the Task Manager/Activity Monitor watching every `QtWebEngineProcess` is the next real step and is called out in Known Limitations.

**Startup work audit** (code review, confirms Part 8's requirements are already met): semantic history indexing is opt-in and off by default; MCP servers are not auto-connected at startup (connections are user-initiated); the Knowledge Graph is never rebuilt at startup, only incrementally updated on save events; inactive workspace tabs are lazily recreated on switch (Phase 17), not loaded eagerly; sync is off by default and, even when on, runs on a timer tick, not at startup. No eager-loading regressions found.

## Part 9 — Database performance (measured, no changes needed)

Benchmarked with synthetic data (5,000 history rows, 300 Missions with findings) against the real stores:

| Operation | Measured |
|---|---|
| Insert 5,000 history rows | 0.37s |
| `history.search()` × 20 (5,000 rows) | 0.05s (2.5ms/call) |
| `history.recent(200)` × 20 | 0.13s |
| Create 300 Missions + 1 finding each | 0.07s |
| `missions.recent(50)` × 20 | 0.016s |

`EXPLAIN QUERY PLAN` on `history.search()`'s `LIKE '%term%'` query shows a full scan (a leading wildcard cannot use a B-tree index by design) - but at a realistic personal-browser scale this is sub-3ms per call, not a real bottleneck. Per the phase's own instruction not to "blindly index every column," no index was added for this; a real fix (SQLite FTS5) would be a new feature, not a polish item, and is noted as a backlog item for if/when history search becomes visibly slow at 10x+ this scale.

## Part 10 — AI responsiveness (audited, not changed)

Streaming, a cancel button, and specific `AuthenticationError`/`RateLimitError`/`APIConnectionError` handling with friendly messages were already built (an earlier session task, "Provider rate-limit / error UX overhaul"). Confirmed these except-clauses exist in `app/agent/claude_client.py` and are exercised by existing tests. No UI-freezing code path was found (requests run off the render thread via the existing async/callback pattern already in place).

## Part 11 — Mission reliability (audited, not changed)

Crash-mid-Mission recovery already exists from Phase 10: `app/storage/mission_graph.py`'s `recover_after_restart` is explicitly "corrected, not resumed" - it never auto-replays an uncertain in-flight DAG action, matching this phase's own instruction exactly. Covered by `tests/test_mission_graph_coordinator.py` and `tests/test_mission_graph_store.py`. No new gaps found in this pass.

## Part 12 — Security red team (re-audited, no regressions found)

Re-checked the fencing/provenance mechanism (`app.security.provenance`/`app.security.firewall`) is actually applied at every content-ingestion point added since Phase 15:
- Agent tools (`app/agent/tools.py`): 12 `wrap_untrusted`/`Provenance` usages across web pages, MCP results, knowledge retrieval, and (Phase 21) collaborator comments.
- Knowledge Graph builder: 21 usages, correctly distinguishing `TRUSTED_APP_STATE` (locally-typed) from `COLLABORATOR_CONTENT` (synced from a peer) and web-derived provenance.
- Collaboration service: comments are fenced as `COLLABORATOR_CONTENT` before ever reaching a tool result (Phase 21's own dedicated test: `test_collaborator_comment_is_fenced_not_authoritative`).

No new prompt-injection, RAG-poisoning, or forged-approval surface was found in the newer subsystems (sync/collaboration/updater) - they don't introduce new tool-authority paths, only new *content* paths, which route through the same fencing every other untrusted source already uses. This matches the existing red-team suite (`tests/test_security_firewall.py` and the Phase 15 fixtures) and no gap was found that needed a new test.

## Part 13 — Privacy review (audited, not changed)

Confirmed the honest-disclosure pattern established each phase already covers this: Settings' Privacy & Security and Privacy/Memory/Knowledge tabs explain the firewall/redaction defaults; the Sync/Collaboration dialogs' own module docstrings and the collaboration role model explicitly disclose what's local vs. shared and that role enforcement is advisory, not cryptographic. No misleading copy found.

## Part 14 — Error UX (audited, not changed)

Spot-checked the named failure modes: provider offline/bad key (Part 10, handled with specific exception types), MCP unavailable (`app/mcp/connection_manager.py`'s per-server independent-failure design, documented not to block the others or startup), sync folder unavailable (`SyncResult(status=ERROR, ...)`, never an unhandled exception - Phase 20), corrupted DB (`Database._open_or_recover` quarantines and recreates rather than crashing - pre-existing), PDF parse error (`pypdf` failures already reported as "no extractable text," not a crash - Phase 3). All were already handled with a friendly path before this phase; none needed a fix.

## Part 15 — Accessibility (audited, real gap found and disclosed)

Only 6 explicit `setAccessibleName()` calls exist across the entire UI (`agent_panel.py`, `mascot.py`, `navigation_bar.py`, `workspace_switcher.py`). 32 `setToolTip()` calls provide a secondary, weaker signal (tooltips are visible to sighted mouse users and are read as a fallback by some screen readers, but are not equivalent to a proper accessible name/description on icon-only buttons). **This is a real, disclosed gap**: keyboard navigation and focus order rely on Qt's standard tab order (present, not specially audited), but explicit accessible naming for icon-only toolbar buttons is thin. Not fixed in this pass (would require a systematic pass over every icon button, which is a larger effort than this consolidation phase's budget) - listed as a known limitation and top post-launch priority.

## Part 16 — Light/dark mode (audited, no bugs found)

Checked every dialog added since Phase 20 (`about_dialog.py`, `update_dialog.py`, `collaboration_ui.py`, `sync_settings.py`) for hardcoded colors or per-widget stylesheets: **none found**. All four use plain `QDialog`/`QPushButton`/`QLabel`/`QListWidget`/`QLineEdit`/`QComboBox` with no local `setStyleSheet()` calls, which means they correctly inherit the app-wide theme stylesheet (`app/ui/theme.py`'s `apply()`, confirmed to style every one of those widget classes for both light and dark). No fix needed - this was a real risk worth checking and it came back clean.

## Part 17 — Small-window / resizing (audited via layout code, not visually)

`MainWindow` uses a `QSplitter` for the tab/side-panel layout and `app/ui/vertical_tabs.py` has explicit min/max width bounds (180-360px, 48px collapsed) from Phase 17 - both already designed for narrow layouts. A true visual regression pass at multiple window sizes on a real display was not performed (this session's environment is headless/offscreen); this is disclosed as a limitation, not claimed as verified.

## Part 18 — Copy polish (audited, no changes needed)

Grepped UI-facing strings for inconsistent terminology; `Py`, `Mission`, `Skill`, `Workspace`, and `Connected Tool`/`Collaboration` were already used consistently across the menus, dialogs, and onboarding copy touched in this and prior phases. No jargon leaks (e.g., "DAG", "provenance", "firewall") were found in user-facing strings - those terms are correctly confined to code comments and this audit's own report.

## Part 20 — Website audit (checked, no overclaiming found)

Compared `website/index.html`'s claims against shipped code. It already discloses "unsigned preview build" status for both platforms and does not claim any feature that doesn't exist (checked specifically for "realtime collaboration," "auto-update," and "extension support" language - none present). It does not yet mention encrypted sync, collaboration, or local-model support at all - an **under-promotion**, not an overclaim, and is listed as a backlog item rather than fixed in this pass (a content-writing task, not a code fix, and out of this phase's "no major new capabilities" scope to expand significantly).

## Part 21 — Documentation (one stale section fixed)

`README.md` had a leftover "## Phase 2 readiness" heading describing the AI panel's layout *before* it was built - now describing something that has existed for 20+ phases under a confusing label. Retitled to "## Layout: browser + AI side panel" and updated the ASCII diagram's "(Phase 2)" label to "(Ask Py)". No other stale documentation was found; `packaging/windows/README.md`, `packaging/macos/README.md`, `packaging/SIGNING.md`, `docs/release_manifest.md`, and `docs/preview_builds.md` (all from Phase 22) remain accurate.

## Part 22 — Release checklist

- [x] Full test suite green (see Part 25)
- [ ] Windows package - not built (see Part 23; no Windows machine available in this session)
- [ ] macOS package - not built (see Part 23; no macOS machine available in this session)
- [ ] Signing/notarization - not configured (disclosed honestly in `packaging/SIGNING.md`; the CI pipeline is wired up and conditional on secrets, see Phase 22)
- [ ] Checksum / update manifest - the mechanism exists and is tested (Phase 22), but no real release has been cut to generate one
- [x] Website download section exists and is honest about unsigned status
- [x] Install/setup/troubleshooting docs exist (`packaging/*/README.md`, `docs/preview_builds.md`)

## Part 23 — Real packaged builds

**Not performed.** This session's environment is Linux-only; PyInstaller does not cross-compile, and there is no Windows or macOS machine available here to build or launch a packaged app on. This is the same, consistently-disclosed limitation stated in every prior packaging phase (`packaging/windows/README.md`, `packaging/macos/README.md`) - it is not new to this phase, and this report does not claim otherwise. The `.github/workflows/release.yml` workflow (Phase 22) is the mechanism by which a real build would be produced and smoke-tested on GitHub's actual Windows/macOS runners; triggering it is a step outside this session's own reach (it requires either pushing a tag or a manual `workflow_dispatch`, both real repository actions).

## Part 24 — Friend/preview test checklist

A concise, non-exhaustive checklist for an external tester (kept short per the phase's own instruction not to ask testers to cover "100 advanced features"):

1. **Install** - did the installer/DMG run without confusion? Any unexpected OS warnings (SmartScreen/Gatekeeper)? Were they clear enough to get past (see `docs/preview_builds.md`)?
2. **First launch** - did the welcome flow make sense? Could you start browsing without setting up AI?
3. **Browsing** - open a few real sites, use multiple tabs, try History/Bookmarks/Downloads.
4. **AI** - configure a provider (or skip), try "Ask Py" on a page, try one Mission with a real everyday goal.
5. **Crashes** - did anything crash or freeze? If so, when, and did PyBrowser offer to restore your session on the next launch?

Report back: what confused you, what broke, and whether you'd keep using it.

## Part 25 — Test flake audit

The one recurring test-infrastructure issue, already characterized in Phases 21/22 and re-confirmed in this phase: running many `QWebEngineView`-heavy test modules together in one `unittest` process occasionally segfaults **after** all tests have already reported a clean `OK` summary - a Qt WebEngine GPU/compositor context-loss crash during CPython interpreter teardown, not a test failure. Re-confirmed in this phase by splitting the affected batch and re-running both halves in isolation: both report a clean `OK` with zero `FAIL`/`ERROR` lines. The macOS release workflow (`release.yml`) already has an explicit, narrowly-scoped rule for this exact pattern (only forgives a nonzero exit when the log shows a clean `OK` summary with no `FAIL:`/`ERROR:` lines) - this is intentionally narrow so it can never mask a real failure. No change was needed this phase; this is disclosed rather than hidden, per the phase's own instruction.

## Tests added this phase

None manufactured for count. The Tools-menu restructuring is a pure reorganization (no behavior change), verified by re-running the existing menu-dependent test suites (150 tests) rather than by writing new tests for a change with no new logic to test. No bugs were found during this audit that warranted a new regression test - this is reported honestly rather than padded.

## Full suite

2,864 tests (established in Phase 22) + re-verified green after the Phase 24 menu restructuring, batched to work around the known Qt teardown segfault pattern (Part 25) - see the commit's full-suite confirmation.

## Final scores (1-10, not inflated)

| Dimension | Score | Why |
|---|---|---|
| Technical architecture | 8 | Consistent layering (service/store/UI), reused abstractions across 23 phases (sync engine reused for collaboration, provenance reused everywhere, one Mission model throughout) - a few newer subsystems (multi-agent DAG, collaboration) add real complexity |
| Core browser | 8 | Tabs, history, bookmarks, downloads, vertical tabs, workspaces are solid and well-tested |
| AI capabilities | 7 | Ask Py and Missions are strong; multi-agent/DAG/Knowledge Graph are real but new and less proven |
| Safety/privacy | 8 | Consistent fencing/provenance/firewall discipline maintained across every new content-ingestion point through 23 phases; advisory-only collaboration trust model is honestly disclosed, not hidden |
| UX/polish | 6 | Good visual design and theming; real menu-overload found and fixed this phase; accessibility naming is thin (disclosed) |
| Reliability | 7 | Crash-recovery, Safe Mode, DB corruption recovery, Mission DAG recovery all real; one known benign test-teardown flake, well-contained |
| Distribution readiness | 4 | Signing/notarization/real packaged builds not done - the mechanism is real and tested, the actual artifacts are not |
| **Overall product readiness** | **6.5** | A genuinely capable, well-engineered product held back specifically by distribution readiness, not by the product itself |

## Launch classification

**Friends & family preview.**

Not "Public preview/beta" because: no signed/notarized build exists yet, no real Windows/macOS build has ever been produced or launch-tested (only Linux-sandbox equivalents), and website/download claims currently describe a build that has not been cut in this session.

Not "Internal alpha" because: the core product (browsing, Ask Py, Missions, privacy firewall, error handling, crash recovery) is well past that bar - it's tested, reliable, and usable by someone who isn't a developer, provided they're told upfront it's unsigned.

**What "friends & family preview" means concretely**: get a real Windows and macOS build produced and smoke-tested on real hardware (Part 23), hand it to a small number of testers with the Part 24 checklist, expect and accept OS security warnings (documented), and do not point the general public at the download link yet.

## Exact top priorities after launch (in order)

1. Produce and smoke-test real Windows and macOS packaged builds on actual hardware (this session cannot do this - it requires triggering `.github/workflows/release.yml` and manually verifying the artifacts).
2. Get a real Windows code-signing certificate and Apple Developer ID + notarization credentials into CI secrets - the mechanism is ready and waiting (Phase 22).
3. Accessibility pass: explicit accessible names on icon-only toolbar buttons (Part 15's disclosed gap).
4. Website: add honest sections for encrypted sync, collaboration, and local-model support (currently under-promoted, not overclaiming).
5. Get real multi-user field mileage on the newest subsystems (multi-agent Missions, Knowledge Graph, collaboration, sync) before calling them anything more than "Experimental."
