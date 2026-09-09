# Packaging PyBrowser

Real distributable builds for Windows and macOS, and the CI that produces
them. **No binary in this directory tree was actually built as part of this
change** - this session's environment is Linux, and PyInstaller does not
cross-compile. What's here is the packaging configuration, build scripts,
and documentation needed to produce a real build on the right OS, plus the
CI workflows that will do exactly that on GitHub's own Windows/macOS
runners. See each platform's README for what was inspected in the app
itself before writing its spec, and the checklist to run before calling a
build real.

```
packaging/
  common/
    spec_common.py       shared PyInstaller datas/hiddenimports, one place
    icons/               pi-badge.svg rendered to every size Windows/macOS need:
                          pybrowser.ico, pybrowser.iconset/ (for iconutil)
  windows/
    pybrowser.spec       PyInstaller spec (--onedir)
    installer.iss        Inno Setup installer script
    build.ps1             installs deps, runs PyInstaller, runs Inno Setup
    README.md             what was inspected, how to build, first-run checklist
  macos/
    pybrowser.spec       PyInstaller spec (--onedir -> .app bundle)
    make_icns.sh          builds pybrowser.icns from common/icons/*.iconset (macOS-only)
    build.sh               installs deps, runs PyInstaller, ad-hoc signs, builds .dmg
    README.md             what was inspected, how to build, first-run checklist
  SIGNING.md              exactly what Authenticode/Developer ID signing needs
  RELEASE_CHECKLIST.md    the steps for cutting an actual release
```

`.github/workflows/release-windows.yml` and `release-macos.yml` run these
same scripts on GitHub's native `windows-latest`/`macos-latest` runners, on
a manual trigger or a `v*` tag - never automatically on every push, and
never publishing anywhere on their own. See `packaging/SIGNING.md` for why
neither workflow signs its output yet.

## Why PyInstaller

Considered against the app's actual shape (a single-process PySide6 desktop
app with Qt WebEngine, not a web service or a CLI tool):

- **PyInstaller** - the default choice for PySide6 apps; has first-class,
  actively maintained hooks for Qt WebEngine specifically (via
  `pyinstaller-hooks-contrib`), which is the one dependency here that is
  genuinely hard to package by hand (the Chromium subprocess binary,
  locales, `.pak`/ICU resource files). This is what both specs use.
- **Nuitka** - compiles to a real binary rather than freezing bytecode,
  which is appealing, but its PySide6/Qt WebEngine support is less mature
  and less commonly exercised in the wild than PyInstaller's; not worth the
  risk for the one dependency (WebEngine) that most needs a well-trodden
  path.
- **briefcase/cx_Freeze** - both viable in general, but neither has
  PyInstaller's specific track record with Qt WebEngine's resource
  layout, which is the actual hard part of this particular app.

`--onedir` rather than `--onefile` on both platforms: a `--onefile` build
re-extracts the entire bundle (Chromium included) into a temp directory on
every launch, which is a real, measurable startup-time cost for an app this
size, and it complicates Qt's own resource path resolution. `--onedir` is
what the Inno Setup installer and the `.app` bundle both package.

## App data and secrets

- User data (history, bookmarks, downloads metadata, the Qt WebEngine
  profile) lives under the OS's own per-user data directory -
  `%LOCALAPPDATA%\PyBrowser` on Windows, `~/Library/Application
  Support/PyBrowser` on macOS (`app/config.py:user_data_dir`) - never inside
  the install directory, and never deleted by uninstalling the program.
- No API key or credential is bundled. `app/agent/keys.py` stores a
  configured provider key in the OS keychain (via `keyring`) the first time
  someone sets one up in Tools -> Configure AI Agent; there is nothing to
  embed and nothing a build script could leak.
