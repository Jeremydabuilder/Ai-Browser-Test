# Release manifest

A small, machine-readable JSON document one release publishes alongside
its installers, so the in-app update checker (Tools/Help → Check for
Updates, `app/updater/checker.py`) can read structured data instead of
scraping a GitHub Releases page's HTML.

## Schema

```json
{
  "version": "0.1.1",
  "channel": "preview",
  "notes_url": "https://github.com/Jeremydabuilder/Ai-Browser-Test/releases/tag/v0.1.1",
  "artifacts": {
    "windows": {
      "url": "https://github.com/.../PyBrowser-Windows-Setup-0.1.1.exe",
      "sha256": "<64-char hex digest>",
      "size": 123456
    },
    "macos": {
      "url": "https://github.com/.../PyBrowser-macOS-0.1.1.dmg",
      "sha256": "<64-char hex digest>",
      "size": 654321
    }
  }
}
```

* `version` must be valid semver (`major.minor.patch`, with optional
  `-prerelease` and `+build` suffixes) - see `app/version.py`.
* `channel` is one of `stable`, `preview`, `nightly`. A stable build only
  ever checks a stable-channel manifest URL, and a preview build only a
  preview one (`app/updater/checker.py`'s `_DEFAULT_MANIFEST_URLS`) - a
  channel is never silently crossed.
* An `artifacts` entry missing your platform is a normal, valid
  "nothing to offer you" result, not an error - a release doesn't have to
  ship every platform every time.
* `sha256` must be the full 64-character lowercase hex SHA-256 digest of
  the artifact file. This is what `app/updater/verify.py` checks a
  downloaded file against before anything is offered to run - a mismatch
  is always rejected outright, never a warning to click past.

## Generating one

`scripts/generate_manifest.py` builds and validates a manifest from local
build artifacts (computing their SHA-256/size itself, rather than trusting
a hand-typed value):

```bash
python scripts/generate_manifest.py \
    --version 0.1.1 --channel preview \
    --notes-url https://github.com/.../releases/tag/v0.1.1 \
    --windows-url https://.../PyBrowser-Setup.exe --windows-file dist/PyBrowser-Setup.exe \
    --macos-url https://.../PyBrowser.dmg --macos-file dist/PyBrowser.dmg \
    --out manifest.json
```

See `.github/workflows/release-manifest.yml` for how the release
workflow calls this after both platform builds have finished.

## Parsing/validating one

`app.updater.manifest.parse_manifest(raw)` parses and validates a
manifest, raising `ManifestError` for anything malformed - bad JSON, an
invalid version, an unknown channel, or an artifact missing a required
field or carrying a malformed checksum. A malformed manifest is always
rejected outright; there is no "best effort" partial parse.
