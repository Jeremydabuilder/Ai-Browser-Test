# Friend / preview builds

A simple, documented path for getting a build to a friend or tester,
without pretending it is a signed, notarized public release.

## Triggering a build

From the GitHub repo → Actions → **Release build** → **Run workflow**:

1. Pick a **channel** - `preview` for a friend-test build (the default),
   `stable` only for an actual release, `nightly` for a throwaway build
   from the current branch tip.
2. Run it. This builds both Windows and macOS, runs the full test suite
   first (a build never ships from a red suite), smoke-tests each
   packaged app for 15 seconds, and generates a release manifest.

Pushing a `v*` tag (e.g. `v0.1.1`) triggers the same workflow
automatically, defaulting to the `preview` channel.

## What you get

Three workflow artifacts, downloadable from the completed run's summary
page:

- `PyBrowser-Windows-Setup-<version>` - the Inno Setup installer `.exe`
- `PyBrowser-macOS-<version>` - the `.dmg`
- `release-manifest` - `manifest.json` (see `docs/release_manifest.md`),
  with the SHA-256/size of each artifact actually built in that run

Each job's log states plainly whether that platform's build is signed:
look for the `Report signing status` step's output. Absent real signing
credentials (see `packaging/SIGNING.md`), both builds are **unsigned
preview builds** - this is the expected, honest default, not a bug.

## Install instructions for a tester

**Windows:**

1. Download and run the `.exe` installer.
2. Windows SmartScreen will likely say "Windows protected your PC" - this
   is expected for an unsigned/newly-signed build (see
   `packaging/SIGNING.md`). Click **More info → Run anyway**.
3. Verify the download's checksum against `manifest.json`'s
   `artifacts.windows.sha256` if you want to confirm nothing was altered
   in transit - PyBrowser's own in-app update checker does this
   automatically for updates it downloads (see
   `app/updater/verify.py`), but a first install needs to be checked by
   hand.

**macOS:**

1. Open the `.dmg` and drag `PyBrowser.app` to Applications.
2. Gatekeeper will likely refuse to open it ("PyBrowser.app is damaged
   and can't be opened", or similar) unless it was Developer-ID signed
   and notarized in that build. Right-click the app → **Open** →
   **Open** again in the confirmation dialog.
3. Same checksum note as Windows, against `artifacts.macos.sha256`.

## What this is not

This is not a store listing, not an auto-update channel a regular user
should be pointed at, and not a promise of ongoing support for that
build. It is the fastest honest way to get a real, tested artifact into
a friend's hands for feedback.
