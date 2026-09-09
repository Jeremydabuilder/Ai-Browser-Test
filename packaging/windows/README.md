# PyBrowser for Windows

## Status

**Not built or run on a real Windows machine.** This session's environment
is Linux; PyInstaller does not cross-compile, so a Windows `.exe` cannot be
produced or verified here. What exists here is a spec, an installer script,
and a build script, all written from inspecting the app's actual entry
point and dependencies (see "What was inspected" below) - not assumed.

Treat the first real run of `build.ps1` on Windows as a test of the build
itself, and work through the checklist below before calling any resulting
installer real.

## What was inspected before writing this

- **Entry point**: `main.py` - constructs `QApplication`, applies the theme,
  opens a `Database` and a `BrowserProfile`, shows `MainWindow`.
- **Dependencies** (`requirements.txt`): `PySide6` (ships Qt WebEngine /
  Chromium - no separate Qt install needed), `anthropic`, `keyring`,
  `httpx2`.
- **Data/profile directories** (`app/config.py`): already OS-aware -
  `%LOCALAPPDATA%\PyBrowser` on Windows, created on demand, never inside the
  install directory. Nothing to change here for packaging.
- **Credential storage** (`app/agent/keys.py` via `keyring`): resolves its
  backend (Windows Credential Locker, here) through `importlib.metadata`
  entry points at runtime - invisible to PyInstaller's static import
  analysis. Handled with `collect_all("keyring")` in
  `packaging/common/spec_common.py`; without it, "Configure AI Agent" would
  silently fail to save a key in a frozen build.
- **Provider SDKs**: `anthropic` is imported lazily (inside a function, in
  `app/agent/claude_client.py`) so the agent works with no key configured;
  `httpx2` (imported as `httpx` in `app/agent/openai_compatible.py`) is a
  real separate package, not an alias. Both are in `collect_all` too.
- **Mascot/icon assets**: `app/ui/mascot.py` resolves Py's artwork relative
  to its own `__file__`, under `app/ui/assets/mascot/`; the app icon
  (`app/config.py:icon_path`) lives alongside it under
  `app/ui/assets/icons/`. Both are covered by bundling `app/ui/assets/`
  wholesale as PyInstaller `datas` - this was the only place in the codebase
  that resolves a sibling file this way (verified by grep; everything else
  that looks like a "template" - the new-tab and Mission pages - is an
  inline Python string, not a file on disk).
- **Dynamic imports**: none found (`grep` for `importlib`/`__import__` in
  `app/` returned nothing) - a normal PyInstaller static analysis should see
  every other import PyBrowser makes.
- **App icon**: `QIcon.fromTheme("web-browser")` (the previous behaviour in
  `main.py`) only resolves on a Linux desktop with a matching icon theme -
  on Windows it silently returns a null icon. Fixed in this same change:
  `main.py` now loads the real bundled `app/ui/assets/icons/pybrowser.ico`
  first, falling back to `fromTheme` only where that still makes sense.

## Building

1. On a Windows machine, with Python 3.11+ installed and this repo checked
   out:
   ```powershell
   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
   ```
2. This installs build dependencies into whatever Python/venv is active,
   runs PyInstaller against `packaging/windows/pybrowser.spec`, and (if
   [Inno Setup 6](https://jrsoftware.org/isinfo.php)'s `iscc.exe` is on
   `PATH`) compiles `packaging/windows/installer.iss` into
   `packaging\windows\output\PyBrowserSetup-<version>.exe`.
3. Without Inno Setup installed, you still get a runnable app at
   `dist\PyBrowser\PyBrowser.exe` (the `--onedir` PyInstaller output) - just
   no single-file installer.

## First-run checklist (do this before calling a build real)

Run through this against a machine that has **never** had Python or this
repo on it, using only the installer output (or the `dist\PyBrowser\`
folder) - not a dev checkout:

- [ ] Installer runs without admin rights (per-user install)
- [ ] App launches from the Start Menu / desktop shortcut
- [ ] First-run dialog appears; new tab shows the PyBrowser page and Py
- [ ] Address bar navigation loads a real website
- [ ] Opening/closing/switching tabs works
- [ ] Ask Py panel opens (Tools -> shows "not configured" without a key,
      which is correct - see `docs/ai_agent.md`)
- [ ] Configure AI Agent dialog opens and a key can be saved (exercises the
      `keyring` Windows Credential Locker backend specifically)
- [ ] Starting a Mission from the new-tab page works
- [ ] Tools -> Settings opens and Save persists a change
- [ ] Dark mode (matching Windows' own light/dark setting) renders correctly
- [ ] A download completes and appears in Tools -> Downloads
- [ ] History and Bookmarks dialogs open and show real entries
- [ ] Closing and reopening the app preserves history/bookmarks/settings
      (data survives in `%LOCALAPPDATA%\PyBrowser`, confirmed **not** to be
      inside the install directory - uninstalling must not delete it)
- [ ] Uninstalling removes the program files only, not user data

## Known gaps

- No code-signing certificate is applied - see `packaging/SIGNING.md`. An
  unsigned installer **will** trigger a Windows SmartScreen warning; that is
  expected, not a bug in the packaging.
- PyInstaller's built-in Qt hooks are relied on to copy Qt WebEngine's
  Chromium process binary, locales, and `.pak`/ICU resources. This is the
  standard, well-supported path for PySide6 + `pyinstaller-hooks-contrib`,
  but it has not been exercised against this exact app in this session -
  it is the first thing the checklist above tests.
