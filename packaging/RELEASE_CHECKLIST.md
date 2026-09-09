# Release checklist

Work through this in order when cutting an actual PyBrowser release. Nothing
here should be skipped by assumption - each step exists because it was
either a real gap found while building the packaging, or a standard release
step that has no shortcut.

## Before building

- [ ] Bump the version in `app/__init__.py` (`__version__`)
- [ ] Bump the version in `packaging/windows/installer.iss`
      (`MyAppVersion`) and `packaging/macos/pybrowser.spec`
      (`CFBundleShortVersionString`/`CFBundleVersion`) to match
- [ ] Full test suite green (`python -m pytest tests/ -q`) - both release
      workflows already gate the build on this; don't release a build the
      suite didn't pass on
- [ ] Write real release notes: what changed, any known issues, minimum OS
      versions supported

## Building

- [ ] Windows: run `packaging/windows/build.ps1` on Windows (or trigger
      `release-windows.yml`), then work through
      `packaging/windows/README.md`'s first-run checklist on a clean machine
- [ ] macOS: run `packaging/macos/build.sh` on macOS (or trigger
      `release-macos.yml`), then work through `packaging/macos/README.md`'s
      first-run checklist on a clean machine
- [ ] Confirm both builds actually ran the checklist against the packaged
      artifact, not a dev checkout - a dev environment having Python/keyring
      configured already hides exactly the bugs packaging exists to catch

## Signing (once certificates exist - see `packaging/SIGNING.md`)

- [ ] Windows installer signed with Authenticode (`signtool`), timestamped
- [ ] macOS `.app` signed with a Developer ID certificate, hardened runtime
      enabled
- [ ] macOS `.dmg` (or the `.app` inside it) notarized via `notarytool` and
      stapled
- [ ] Re-run each platform's first-run checklist against the *signed*
      artifact - signing can change what Gatekeeper/SmartScreen do, and
      that behavior needs to be seen once for real, not assumed

## Checksums

- [ ] Generate a SHA-256 checksum for every artifact (installer `.exe`,
      `.dmg`) and publish it alongside the download - lets anyone verify
      what they downloaded matches what was built
  ```bash
  sha256sum PyBrowserSetup-0.1.0.exe PyBrowser-0.1.0.dmg
  ```

## Publishing

- [ ] Create the GitHub Release, attach the installer/dmg and their
      checksums, paste in the release notes
- [ ] Only after this: update the website's download section to link the
      real artifacts (see `website/README.md` and the note in
      `website/index.html`'s Early Access section) - never point a download
      button at a file that doesn't exist yet or hasn't passed its
      first-run checklist
- [ ] Tag the commit (`git tag v0.1.0 && git push --tags`) if not already
      tagged (tagging is also what triggers both release workflows)

## Update mechanism

PyBrowser has no auto-update mechanism today. Until one exists:
- [ ] State the current version's expected lifetime/support window in the
      release notes
- [ ] Point users back to the GitHub Releases page (or the website) for the
      next version rather than implying the app will notify them

## Support path

- [ ] Confirm the GitHub repository's Issues tab is enabled and is the
      stated bug-report path (linked from the website's footer already)
- [ ] Confirm `README.md`'s system requirements section matches what was
      actually tested (OS versions, Python version if running from source)
