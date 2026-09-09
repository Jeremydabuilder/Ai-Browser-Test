# PyBrowser release-candidate checklist

Run this in full, on both platforms, before calling any version stable.
Fill in the version and check off each line as it's actually verified - not
as it's assumed to work.

## PyBrowser 0.x.x RC1

**Windows** (tested on: _______________, e.g. "Windows 11 23H2, clean VM")
- [ ] Installer runs without admin rights
- [ ] Clean install (no prior Python/PyBrowser on the machine)
- [ ] App launches from Start Menu / desktop shortcut
- [ ] π/PyBrowser icon renders (taskbar, title bar, Start Menu, `.exe`
      Properties -> Details tab shows the right version)
- [ ] Normal browsing (address bar, tabs open/close/switch, back/forward)
- [ ] Ask Py panel opens
- [ ] Configure AI Agent opens and saves a key without freezing
- [ ] Settings opens and Save persists
- [ ] History and Bookmarks show real entries
- [ ] A download completes and shows in Tools -> Downloads
- [ ] Starting a Mission from the new-tab page works; Research UI renders
- [ ] Py's mascot artwork loads (not a blank box)
- [ ] Dark mode matches the OS setting
- [ ] Restart preserves history/bookmarks/settings/Missions
- [ ] User data confirmed under `%LOCALAPPDATA%\PyBrowser`, not the install dir
- [ ] Uninstall removes only the program files, not `%LOCALAPPDATA%\PyBrowser`
- [ ] SmartScreen/antivirus behavior noted (expected until signed - see
      `packaging/SIGNING.md`)

**macOS** (tested on: _______________, e.g. "macOS 14.5 Sonoma, real Mac")
- [ ] Clean install (drag `.app` from `.dmg` to `/Applications`, no prior
      Python/PyBrowser on the machine)
- [ ] App launches from Finder
- [ ] π/PyBrowser icon renders (Dock, `Cmd+Tab`, Finder)
- [ ] Normal browsing (address bar, tabs, back/forward)
- [ ] Ask Py panel opens
- [ ] Configure AI Agent opens and saves a key without freezing (exercises
      the macOS Keychain backend specifically)
- [ ] Settings opens and Save persists
- [ ] History and Bookmarks show real entries
- [ ] A download completes and shows in Tools -> Downloads
- [ ] Starting a Mission works; Research UI renders
- [ ] Py's mascot artwork loads
- [ ] Dark mode matches the OS appearance setting
- [ ] Quit and relaunch preserves history/bookmarks/settings/Missions
- [ ] User data confirmed under `~/Library/Application Support/PyBrowser`
- [ ] Gatekeeper behavior noted (expected to refuse an unsigned `.app`
      opened via Finder without a right-click -> Open, until notarized -
      see `packaging/SIGNING.md`)

**Both platforms**
- [ ] Website's download links (once added - see
      `website/index.html`'s Early Access section) actually point at these
      exact artifacts
- [ ] SHA-256 checksums generated and published for both artifacts
- [ ] Version number agrees everywhere it appears: `app/__init__.py`,
      the installer, the `.exe`'s own file properties, the `.app`'s
      `Info.plist`, and the artifact filenames themselves
- [ ] CI workflow runs are green (`release-windows.yml`,
      `release-macos.yml`), including their smoke-test steps

Do not mark this RC stable until every box above is checked against a real
run on a clean machine - not inferred from the packaging configuration
existing, and not inferred from CI's automated smoke test alone (CI proves
the app *starts*; it cannot click through the UI).
