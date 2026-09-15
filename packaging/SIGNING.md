# Code signing and notarization

Nothing here is faked or applied. This documents exactly what public
distribution needs on each platform, so a real certificate can be dropped in
later without redesigning the build.

## Windows: Authenticode

**What's needed:**
- A code-signing certificate (OV or EV) from a CA Windows trusts - e.g.
  DigiCert, SSL.com, Sectigo. EV certificates get SmartScreen reputation
  faster; OV certificates are cheaper but the executable accumulates
  SmartScreen trust more slowly (based on download volume over time).
- The certificate as a `.pfx`/`.p12` file, or (for EV) a hardware token/HSM -
  EV certificates typically cannot be exported as a plain file.
- `signtool.exe` (ships with the Windows SDK) to apply it:
  ```powershell
  signtool sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 `
      /f MyCert.pfx /p $env:CERT_PASSWORD `
      dist\PyBrowser\PyBrowser.exe
  ```
  Sign the installer `.exe` too, after Inno Setup builds it.
- In CI: `secrets.WINDOWS_CERT_BASE64` (the `.pfx`, base64-encoded) and
  `secrets.WINDOWS_CERT_PASSWORD`, decoded to a temp file for the
  `signtool` step in `.github/workflows/release.yml`'s `build-windows`
  job, then deleted. **The step exists and is wired up, but is
  conditional on those secrets being present** (its "Check for signing
  credentials" step gates it) - a run with no certificate configured
  simply skips it and uploads an unsigned build, which that job's final
  "Report signing status" step states plainly in the run's log.

**SmartScreen**: an unsigned or newly-signed executable will still show a
"Windows protected your PC" warning until it accumulates enough download
reputation (EV certificates mostly skip this; OV certificates build it up
over time). This is expected, not a packaging bug - do not try to work
around it by disabling SmartScreen or asking users to.

## macOS: Developer ID, hardened runtime, notarization, stapling

**What's needed:**
- An active **Apple Developer Program** membership ($99/year) and a
  **Developer ID Application** certificate generated from it.
- Signing with the **hardened runtime** enabled (required for notarization):
  ```bash
  codesign --force --deep --options runtime \
      --sign "Developer ID Application: Your Name (TEAMID)" \
      dist/PyBrowser.app
  ```
  PyBrowser doesn't need special entitlements beyond the runtime's
  defaults - it doesn't use the camera/mic, JIT, or unsigned executable
  memory, so no `.entitlements` file is required unless testing turns up
  otherwise.
- **Notarization** - submit the signed `.app` (or the `.dmg` built from it)
  to Apple and wait for approval:
  ```bash
  xcrun notarytool submit PyBrowser-0.1.0.dmg \
      --apple-id "you@example.com" --team-id TEAMID \
      --password "app-specific-password" --wait
  ```
  The password is an **app-specific password** generated at
  appleid.apple.com, not the Apple ID's real password.
- **Stapling** - attach the notarization ticket so Gatekeeper can verify
  offline:
  ```bash
  xcrun stapler staple PyBrowser-0.1.0.dmg
  ```
- In CI: `secrets.MACOS_CERT_BASE64` (the Developer ID `.p12`, base64-
  encoded), `secrets.MACOS_CERT_PASSWORD`, `secrets.MACOS_SIGNING_IDENTITY`
  (the exact "Developer ID Application: Name (TEAMID)" string), and for
  notarization `secrets.APPLE_NOTARIZE_API_KEY`/`_KEY_ID`/`_ISSUER_ID` -
  all read by the conditional "Sign with Developer ID", "Notarize and
  staple" steps in `.github/workflows/release.yml`'s `build-macos` job.
  **Wired up and conditional on those secrets being present**: absent
  them, the job falls back to a local, throwaway ad-hoc signature
  (`codesign --sign -`) so the CI-built `.app` can still be smoke-tested
  on the runner - that signature satisfies nothing on a real user's Mac,
  and the job's final "Report signing/notarization status" step states
  the actual outcome (unsigned / signed-not-notarized / signed+notarized)
  plainly in the run's log every time.

## What this means today

As of this writing, no signing/notarization secrets are configured in
this repository, so every build the `release.yml` workflow produces is
still:
- **Windows**: a SmartScreen warning ("Windows protected your PC" -> More
  info -> Run anyway).
- **macOS**: Gatekeeper refusing to open the app at all
  ("PyBrowser.app is damaged and can't be opened" or similar) unless the
  person right-clicks -> Open, or the app is signed with at least an ad-hoc
  signature first.

This is the correct, honest state before a certificate exists - it is not a
build defect to fix by disabling these checks. Once real credentials are
added as the repository secrets named above, the very next run signs (and,
for macOS, notarizes) automatically - no workflow changes needed.
