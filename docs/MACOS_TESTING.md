# macOS: status and volunteer checklist

## Status (honest)
- A macOS `.app`/`.dmg` is **built in CI** (`.github/workflows/release.yml`, macOS job) on a free GitHub runner and
  smoke-started for a few seconds. That shows the packaging step completes and the process starts on that runner.
  It does **not** show that the app installs, looks right, stores keys in Keychain, or behaves for a person on a real Mac.
- The app is **not signed and not notarized** (no Apple Developer ID exists). Gatekeeper will refuse to open it by
  double-click; the workaround is per-file: right-click → Open (or System Settings → Privacy & Security → *Open Anyway*).
  Do not disable Gatekeeper globally.
- The developer has no Mac. No macOS-specific fix has been made without evidence, and none should be.
- Windows is the priority platform; this does not block a Windows preview.

## What a volunteer with a Mac can check (about 20 minutes)
Record: macOS version `_____`, Intel or Apple silicon `_____`, build/commit `_____`.
- [ ] Download the `.dmg` from the workflow artifacts or a pre-release; compare its SHA-256 (`shasum -a 256`).
- [ ] Open it, drag PyBrowser to Applications, open it (right-click → Open). Note exactly what macOS says.
- [ ] The window opens; ordinary browsing works (3 sites, tabs, downloads, bookmarks, find).
- [ ] The Dock icon and app menu say PyBrowser. Cmd+T/W/L/F shortcuts work (not Ctrl).
- [ ] AI is off by default; turn it on, set up a provider (Gemini/Groq free tier, or Ollama), press **Test Connection**.
      Does the key survive quitting and reopening (stored in Keychain)? Does a Keychain prompt appear?
- [ ] Run one AI Team *Compare products* with 2–3 open tabs and requirements; check the result's sources and citations.
- [ ] Quit during a run, reopen, Resume.
- [ ] Dark mode follows macOS appearance. VoiceOver can reach the toolbar buttons (note anything unnamed).
- [ ] Where is data stored (`~/Library/Application Support/PyBrowser`)? Does deleting the app leave it (it should)?

Report each failure with the step, a screenshot, and `~/Library/Application Support/PyBrowser/logs/pybrowser.log`.
