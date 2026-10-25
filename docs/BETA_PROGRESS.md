# Public-beta hardening: progress record

Resume point for the next developer. Authoritative status is `CAPABILITY_MATRIX.md`.
Branch: `claude/python-desktop-browser-n20ht5`. Tests: `python tests/run.py [module...]`
(unittest; `QT_QPA_PLATFORM=offscreen QTWEBENGINE_DISABLE_SANDBOX=1`).

## Done in this pass (each a separate commit with tests)

1. Accessibility: `app/ui/a11y.py`, names/focus across dialogs, `tests/test_accessibility.py`.
2. Command palette: `app/ui/command_palette.py`, wired in `main_window.py`.
3. Updater: `app/updater/download.py` (bounded, cancellable, keeps `.exe`), https-only manifest URLs, channel match.
4. Sync: engine tolerates damaged/partial/wrong-key records (`SyncResult.failed`, token held), offline mid-pass, tombstone ordering; `LocalFolderProvider` atomic writes + self-healing manifest.
5. Collaboration: fixed MainWindow handing the Mission *service* instead of the store; role enforcement local + on receipt; dialog states advisory limits.
6. Replay: tab following (pinned working tab, follows one new tab, stops on several/lost), step timeout, run-generation guard, declines always stop, skipped steps announced.
7. Team research: PDF extraction; `claimcheck.py` + "Citation check (automatic)" appendix.
8. MCP server follows the AI master switch.
9. Docs: capability matrix, release-checklist/packaging README corrections.

## Not done / blocked

- Clean-machine Windows/macOS install, signing, notarization (external).
- Live provider / live search / real cloud-drive / screen-reader verification (external).
- Extensions (see matrix).
- Stale historical docs still carry old "production-ready" wording; they now point here.

## Next candidates

- Real two-machine sync trial; real NVDA pass; tester feedback from the pre-release build.
- Optional: OCR for scanned PDFs (would be a new dependency).

## Focused-workflow pass (comparison -> evidence -> resume)

Added after the list above; same branch.
10. Team/AI Mode follow the provider set up for the assistant (all providers, local included); Configure AI Agent starts on a free-tier option.
11. Requirements are a first-class, verbatim, persisted part of a Team run; final-answer prompt separates requirement / sourced fact / judgement and demands "not stated" over invented figures.
12. Mission -> Team hand-off (AI > Continue Mission in AI Team...) and explicit Save summary to Mission (`app/team/handback.py`).
13. Offline sample result at `pybrowser://demo/` (labelled; reachable from AI Mode's no-provider state and the AI menu).
14. `evals/`: 24 tasks, fixtures, rubric, opt-in runner, record/score/report, competitor sheets. **No live run made.**
15. `docs/WINDOWS_CLEAN_MACHINE_CHECKLIST.md`, `docs/MACOS_TESTING.md`, updated tester guide.

Still to do by a person: run the Windows checklist; run the eval smoke subset with a real (free-tier or local) model and score it;
decide what to do about the unsigned-build friction (see SIGNING.md - nothing free removes it).
