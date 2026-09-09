# PyBrowser for macOS

## Status

**Not built on a real Mac.** This session's environment is Linux.
PyInstaller does not cross-compile, `.icns` creation needs `iconutil`
(macOS-only), and `.dmg` creation needs macOS disk-image tooling - none of
that can be produced or verified here. What exists here is a spec, an
icon source, and build scripts, written from inspecting the app's actual
entry point and dependencies (the same audit as the Windows build - see
`packaging/windows/README.md#what-was-inspected-before-writing-this`, which
applies identically since the app code is the same on both platforms).

**No `.app` or `.dmg` file in this repository should be taken as a real
macOS build.** None was produced. `packaging/macos/build.sh` must be run on
an actual Mac to produce one, then verified against the checklist below.

## Building

On a Mac, with Python 3.11+ and this repo checked out:

```bash
bash packaging/macos/build.sh
```

This builds `packaging/macos/pybrowser.icns` from the pre-rendered iconset
at `packaging/common/icons/pybrowser.iconset/` (via `iconutil`, macOS-only -
that's why the `.icns` itself isn't checked into the repo), runs PyInstaller
against `packaging/macos/pybrowser.spec`, and - if
[`create-dmg`](https://github.com/create-dmg/create-dmg) is installed
(`brew install create-dmg`) - packages `dist/PyBrowser.app` into
`packaging/macos/output/PyBrowser-<version>.dmg`.

The resulting `.app` is **unsigned and not notarized**. Gatekeeper will
refuse to open it on another Mac without at least an ad-hoc signature
(`codesign --force --deep --sign - dist/PyBrowser.app` - a local, throwaway
signature, not one that satisfies Gatekeeper elsewhere). Real distribution
needs a paid Apple Developer ID and notarization - see `packaging/SIGNING.md`.

## First-run checklist (do this before calling a build real)

Run through this on a Mac that has never had this repo or a dev Python
environment on it, using only `PyBrowser.app` (or the mounted `.dmg`):

- [ ] App launches from Finder (double-click, or drag from the `.dmg` to
      `/Applications` first)
- [ ] First-run dialog appears; new tab shows the PyBrowser page and Py
- [ ] Address bar navigation loads a real website
- [ ] Opening/closing/switching tabs works
- [ ] Ask Py panel opens
- [ ] Configure AI Agent dialog opens and a key can be saved (exercises the
      `keyring` macOS Keychain backend specifically - the same
      metadata-driven backend-discovery risk noted in the Windows README,
      just a different OS backend)
- [ ] Starting a Mission from the new-tab page works
- [ ] Tools -> Settings opens and Save persists a change
- [ ] Dark mode (matching macOS' own appearance setting) renders correctly
- [ ] A download completes and appears in Tools -> Downloads
- [ ] History and Bookmarks dialogs open and show real entries
- [ ] Quitting and relaunching preserves history/bookmarks/settings (data
      lives in `~/Library/Application Support/PyBrowser`, per
      `app/config.py:user_data_dir` - confirm it survives a
      drag-to-Applications reinstall, since that replaces the `.app` bundle
      but must never touch this directory)

## Known gaps

- No Developer ID signature, no hardened runtime entitlements, no
  notarization, no stapling - see `packaging/SIGNING.md` for exactly what
  each of those requires and why they're not fakeable from here.
- `create-dmg` background/branding is left at its defaults; a real release
  would want a proper background image, but that is cosmetic and not a
  blocker.
- PyInstaller's built-in Qt hooks are relied on to bundle Qt WebEngine
  correctly inside the `.app` (the Chromium helper process, locales,
  `.pak`/ICU resources, and the `Info.plist` entries WebEngine's helper
  process needs). This is standard for PySide6 + `pyinstaller-hooks-contrib`
  but has not been exercised against this exact app - it is the first thing
  the checklist above tests.
