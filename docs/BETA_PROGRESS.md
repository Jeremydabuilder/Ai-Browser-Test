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
6. Replay: step timeout, run-generation guard, declines always stop, skipped steps announced.
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
- Optional: pin the replay tab id at start; OCR for scanned PDFs (would be a new dependency).
