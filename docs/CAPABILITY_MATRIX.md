# PyBrowser capability matrix (the one authoritative status list)

Every other audit in `docs/` (`phase24_launch_audit.md`, `ai_browser_capabilities.md`,
`phase2_ai_architecture.md`) is **historical** and may be stale; this file wins.
Keep it current when behaviour changes. Do not upgrade a row without naming the
evidence.

**Status words**

| Status | Meaning |
|---|---|
| **Tested** | Automated tests drive the real code path (real Qt WebEngine, SQLite, local HTTP server, shared folder) in the Linux/offscreen environment and CI. |
| **Tested with fakes** | Logic is tested, but the external service (a model provider, a search engine, a cloud-drive client) is replaced by a fake. Says nothing about the real service. |
| **Unverified** | Needs something not available to the developer so far: a clean Windows/macOS machine, a live API key, a signing certificate, a screen reader. Code may exist. |
| **Not supported** | Deliberately absent. |

"Tested" is never "production-ready". Nothing here has had a clean-machine
install, live-provider, or screen-reader pass; see *What no test can show* below.

## Browser core

| Capability | Status | Evidence | Remaining / limits |
|---|---|---|---|
| Tabs, navigation, history, bookmarks, downloads, find-in-page, zoom | Tested | `tests/test_*` against real WebEngine | Windows/macOS behaviour covered only by CI runs of the same suite |
| Pinned tabs, tab groups, vertical tabs, iframes | Tested | tab/vertical-tab/iframe suites | — |
| Workspaces and isolated profiles | Tested | workspace suites | Isolated profile is never synced (tested) |
| Settings persistence, session restore | Tested | settings/session suites | — |
| Command palette (Ctrl+Shift+P) | Tested | `tests/test_command_palette.py` (15) | Launcher over existing menu actions/handlers only; AI entries hidden while AI is off |
| Extensions | **Not supported** | see *Extensions* below | Qt WebEngine's extension API could not be exercised here |

## AI

| Capability | Status | Evidence | Remaining / limits |
|---|---|---|---|
| AI master switch (off by default; off = plain browser) | Tested | `tests/test_ai_switch.py` | Includes: MCP server does not run while AI is off, and the person's preference survives |
| AI Mode (sourced answers) | Tested with fakes | `tests/test_aimode*.py` | No live provider/search run |
| Agent (Ask Py): tool loop, approvals, prompt-injection fencing | Tested with fakes | agent/session suites | Live-model quality unverified; injection is mitigated, not solved |
| Provider switching, streaming, cancellation, rate-limit/5xx auto-retry | Tested with fakes / local servers | agent + provider suites | Live providers **unverified**. Retry re-sends only the model request, never a tool call |
| Local models (Ollama/LM Studio/compatible) | Tested with fakes | `tests/test_local_providers.py` | Depends on the user's local server |
| Missions (single-agent), saved results | Tested | mission suites | — |
| Multi-agent Missions (DAG) | Tested with fakes | coordinator/graph suites | Experimental. Restart recovery moves interrupted work to *needs review* when a write was attempted; it never silently re-runs it |
| Scheduled tasks / watches | Tested | task-runner suites | Same restart rule as above |
| Visual fallback | Tested with fakes | `tests/test_visual_*` | Experimental; always asks for confirmation |
| Knowledge Graph | Tested | graph suites | Experimental |
| AI Team (planning, handoffs, review, templates, follow-ups, steering) | Tested with fakes | `tests/test_team_*` | Real-model quality, real search unverified; three-run lifetime cap in place |
| Team/AI Mode use the provider you set up (Anthropic, OpenAI, Gemini, Groq, OpenRouter, Ollama, LM Studio, local OpenAI-compatible) | Tested with fakes (client class + config built; no live call) | `tests/test_team_provider.py` | Previously only Groq/OpenAI/OpenRouter/Gemini worked here. Local-model quality for team runs is unverified |
| **Requirements survive** planning, review and the final result (user-stated, verbatim, labelled in the criteria check) | Tested with fakes | `tests/test_comparison_workflow.py` | Whether a real model *honours* them in prose is unmeasured - see the eval pack |
| Evidence in the result: URL, opened-page vs snippet, retrieval date, conflicts, unreadable sources, "not stated" figures | Tested with fakes (structure and appendix); prompt guidance for conflicts/uncertainty is untested against a real model | `tests/test_comparison_workflow.py`, `test_team_e2e_missions.py` | Model prose may still smooth over a conflict; the automatic check flags only figure/wording mismatches |
| Mission -> Team hand-off and **Save summary to Mission** (explicit, bounded) | Tested (real window + real stores; scripted model) | `tests/test_comparison_ui.py` | Link by id only: the browsing Mission's agent and the Team do not share live state. Mission pages that are not open in a tab are listed, not fetched |
| Resume after a failure or restart without redoing finished work | Tested with fakes | `test_team_*`, `test_comparison_workflow.py` | No real-process-kill test; the interrupted state is simulated by saving and reloading |
| Offline sample result (`pybrowser://demo/`) | Tested (served, script-free, labelled) | `tests/test_first_run.py` | Hand-written with fictional products; never presented as model output |
| Optional live evaluation pack (24 tasks, rubric, results/competitor sheets) | Harness tested; **no live run has been made** | `evals/`, `tests/test_eval_harness.py` | Run results are the only evidence of model quality; none exist yet |
| Team web research: HTML and **PDF** text, SSRF protection | Tested | `test_team_webfetch*.py`, `test_team_pdf_claims.py` | JS-rendered pages and sign-in walls are reported as unreadable, never bypassed; scanned PDFs need OCR (not done) |
| Team citations: existence check + **claim-to-source check** | Tested | `app/team/claimcheck.py`, `test_team_pdf_claims.py` | Lexical only: finds whether wording and figures appear in the held text. Not a fact check; cannot judge truth, paraphrase, or entailment. Snippet-only support is labelled |
| MCP client (external tools) | Tested with fakes | `test_mcp_*` | Third-party servers vary |
| MCP server (external AI drives PyBrowser) | Tested | `test_mcp_server_*` | Off by default; stops with the AI switch; every call goes through the same approval pipeline |

## Data, sync, collaboration, automation

| Capability | Status | Evidence | Remaining / limits |
|---|---|---|---|
| Encrypted sync via a shared folder | Tested | `test_encrypted_sync.py`, `test_sync_resilience.py` (12, real folder, two independent device stores) | **No real multi-device or cloud-drive field test.** Transport is whatever syncs your folder (Dropbox, iCloud, OneDrive, a network drive); PyBrowser writes encrypted files only. Credentials/API keys are never synced (scanned). Damaged/partial/wrong-key records are reported and retried, not applied; the folder index self-heals when two devices overwrite it |
| Collaboration (shared Missions) | Tested | `test_collaboration*.py` | **Roles are advisory**: everyone shares one key, there are no accounts or signatures. Enforced: viewers are refused local writes; incoming changes claiming a known viewer/removed participant are not applied. Not enforceable: a malicious key holder; recalling data already downloaded. Collaborator content is untrusted and never approves actions |
| Workflow recording & replay | Tested (fakes + real page) | `test_automation.py`, `test_replay_safety.py` | Records agent actions, not manual browsing. Works in one tab (the active one at start): follows exactly one tab opened by a click/typing/open-tab step, stops if several open or the tab is closed. Targets re-resolved by role/name each run; ambiguity/missing/timeout/decline stop the run; every step re-asks approval; optional-step skips are announced |
| Updater | Tested (local HTTP) | `test_release_infra.py`, `test_update_download.py` | https-only manifest URLs, channel match, size-bounded verified download. Never self-overwrites. **No signed build exists to update to** |

## Distribution

| Capability | Status | Evidence | Remaining / limits |
|---|---|---|---|
| Windows installer build (PyInstaller + Inno Setup) | Built in CI | `release.yml`, `windows-tester-build.yml` | **Clean-machine install unverified**; unsigned (SmartScreen warning expected) |
| macOS app/dmg | Built in CI | `release.yml` | Unsigned, not notarized; unverified on a real Mac |
| Code signing / notarization | **Unverified** | `packaging/SIGNING.md` | Needs an Authenticode certificate and an Apple Developer ID — external |
| Release publishing | Manual | — | No workflow publishes a stable release; the tester workflow publishes a labelled pre-release only when run |

## Checklists

| Platform | Document | State |
|---|---|---|
| Windows (priority) | `docs/WINDOWS_CLEAN_MACHINE_CHECKLIST.md` | Written; **never run on a clean machine** |
| macOS | `docs/MACOS_TESTING.md` | CI builds an unsigned `.app`/`.dmg`; **nothing verified on a real Mac**; volunteer checklist provided |

## Accessibility

| Capability | Status | Evidence | Remaining / limits |
|---|---|---|---|
| Names for every control on audited surfaces, visible focus, focus restoration | Tested (code/automated audit) | `tests/test_accessibility.py` (16) | **No real screen-reader (NVDA/JAWS/VoiceOver) or visual high-contrast pass.** Web page content accessibility is Chromium's |

## Extensions

Qt 6.11's PySide6 exposes `QWebEngineExtensionManager` / `QWebEngineExtensionInfo`.
In this environment, loading an unpacked extension returned, but enabling it
(`setExtensionEnabled`) crashed the process (offscreen Linux), so nothing was
shipped. The API's supported Chrome-extension surface was not confirmed from Qt's
primary documentation (no network access to it in this pass) and it was not
tried on Windows. PyBrowser therefore offers **no extension support**, and no
script-injection feature is presented as one. Revisit with a real Windows/macOS
desktop session and the Qt documentation for the pinned Qt version.

## What no test can show

- A clean-machine Windows or macOS install and first run.
- Behaviour against live model providers, live search, and the real web at scale.
- A real cloud-drive client syncing the encrypted folder between two machines.
- Screen-reader and visual-theme usability.
- Whether signed builds pass SmartScreen/Gatekeeper.
