"""Phase 22 Part 17: the release manifest - a small, machine-readable
JSON document describing one release's artifacts, so an update checker
can consume structured data instead of scraping a GitHub Releases page's
HTML (Part 3's "compare semantic versions... show release notes... show
download button" needs a stable shape to read from).

Schema (see docs/release_manifest.md for the human-readable version):

    {
      "version": "0.1.1",
      "channel": "preview",
      "notes_url": "https://github.com/.../releases/tag/v0.1.1",
      "artifacts": {
        "windows": {"url": "...", "sha256": "...", "size": 123},
        "macos": {"url": "...", "sha256": "...", "size": 456}
      }
    }

``notes_url`` and each artifact's fields beyond ``sha256``/``size`` are
optional in the sense that a malformed *document* is rejected outright
(ManifestError) but a manifest missing this platform's own artifact is a
normal, valid "nothing to offer you" result - see checker.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.version import CHANNELS, InvalidVersion, parse_version

PLATFORM_WINDOWS = "windows"
PLATFORM_MACOS = "macos"
PLATFORMS = (PLATFORM_WINDOWS, PLATFORM_MACOS)


class ManifestError(ValueError):
    """A release manifest is malformed - never partially trusted, only
    rejected outright (the same posture as a bad checksum: reject
    mismatches, do not guess at what was meant)."""


@dataclass(frozen=True)
class Artifact:
    url: str
    sha256: str
    size: int

    def to_dict(self) -> dict:
        return {"url": self.url, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class ReleaseManifest:
    version: str
    channel: str
    artifacts: dict[str, Artifact]
    notes_url: str = ""

    def artifact_for(self, platform_key: str) -> Artifact | None:
        return self.artifacts.get(platform_key)

    def to_dict(self) -> dict:
        return {
            "version": self.version, "channel": self.channel, "notes_url": self.notes_url,
            "artifacts": {key: artifact.to_dict() for key, artifact in self.artifacts.items()},
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


def _require_str(obj: dict, key: str, *, context: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{context}: {key!r} must be a non-empty string")
    return value


def parse_manifest(raw: str | bytes | dict) -> ReleaseManifest:
    """Parse and validate a release manifest. Raises ManifestError for
    anything malformed - bad JSON, a missing/invalid version, an unknown
    channel, or an artifact missing a required field. Never returns a
    partially-valid manifest."""
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ManifestError(f"invalid JSON: {exc}") from exc
    else:
        data = raw
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")

    version = _require_str(data, "version", context="manifest")
    try:
        parse_version(version)
    except InvalidVersion as exc:
        raise ManifestError(f"manifest version: {exc}") from exc

    channel = data.get("channel", "stable")
    if channel not in CHANNELS:
        raise ManifestError(f"manifest channel must be one of {CHANNELS}, got {channel!r}")

    notes_url = data.get("notes_url", "")
    if notes_url and not isinstance(notes_url, str):
        raise ManifestError("manifest notes_url must be a string")

    raw_artifacts = data.get("artifacts", {})
    if not isinstance(raw_artifacts, dict):
        raise ManifestError("manifest artifacts must be an object")

    artifacts: dict[str, Artifact] = {}
    for platform_key, entry in raw_artifacts.items():
        if not isinstance(entry, dict):
            raise ManifestError(f"artifact {platform_key!r} must be an object")
        context = f"artifact {platform_key!r}"
        url = _require_str(entry, "url", context=context)
        sha256 = _require_str(entry, "sha256", context=context)
        if len(sha256) != 64 or not all(c in "0123456789abcdefABCDEF" for c in sha256):
            raise ManifestError(f"{context}: sha256 must be a 64-character hex digest")
        size = entry.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ManifestError(f"{context}: size must be a non-negative integer")
        artifacts[platform_key] = Artifact(url=url, sha256=sha256.lower(), size=size)

    return ReleaseManifest(version=version, channel=channel, artifacts=artifacts, notes_url=notes_url or "")


def build_manifest(*, version: str, channel: str, artifacts: dict[str, Artifact],
                   notes_url: str = "") -> ReleaseManifest:
    """Construct (and validate, via a round-trip through parse_manifest)
    a manifest for the release workflow to write out - see
    scripts/generate_manifest.py."""
    manifest = ReleaseManifest(version=version, channel=channel, artifacts=artifacts, notes_url=notes_url)
    return parse_manifest(manifest.to_dict())
