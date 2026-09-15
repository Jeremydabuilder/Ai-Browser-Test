"""Phase 22 Part 3: the update checker. Fetches a release manifest from a
configured URL (a GitHub Releases asset, or any static host), parses and
validates it (app.updater.manifest), and compares it against the running
build's own version/channel - never installs anything itself (see
app/ui/update_dialog.py for the UI that offers a download, and Part 5/6
for what happens after a download completes).

Channel isolation (Part 8): a stable build only ever compares itself
against a stable manifest, and a preview build only against a preview
manifest - the manifest URL itself is expected to be channel-specific
(see PYBROWSER_UPDATE_MANIFEST_URL / the per-channel defaults below), so
there is no cross-channel "silent" update path to guard against inside
this module; it simply never fetches the other channel's manifest.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass

from app.updater.manifest import ManifestError, ReleaseManifest, parse_manifest
from app.version import CHANNEL_PREVIEW, CHANNEL_STABLE, current_channel, current_version, is_newer

#: Where to fetch each channel's manifest from by default - a static file
#: this repo's own release workflow publishes (Part 17/18). Overridable
#: via PYBROWSER_UPDATE_MANIFEST_URL for testing or a self-hosted feed.
_DEFAULT_MANIFEST_URLS = {
    CHANNEL_STABLE: (
        "https://github.com/Jeremydabuilder/Ai-Browser-Test/releases/latest/download/manifest.json"),
    CHANNEL_PREVIEW: (
        "https://github.com/Jeremydabuilder/Ai-Browser-Test/releases/latest/download/manifest.preview.json"),
}
_ENV_MANIFEST_URL = "PYBROWSER_UPDATE_MANIFEST_URL"


class UpdateCheckError(Exception):
    """The update check itself failed - network error, bad response, or
    a malformed manifest. Distinct from "no update available", which is
    a normal, successful result."""


@dataclass(frozen=True)
class UpdateResult:
    available: bool
    current_version: str
    latest_version: str = ""
    channel: str = ""
    notes_url: str = ""
    download_url: str = ""
    sha256: str = ""
    size: int = 0


def platform_key() -> str:
    system = platform.system()
    if system == "Windows":
        return "windows"
    if system == "Darwin":
        return "macos"
    return system.lower()


def manifest_url(channel: str | None = None) -> str:
    override = (os.environ.get(_ENV_MANIFEST_URL) or "").strip()
    if override:
        return override
    return _DEFAULT_MANIFEST_URLS.get(channel or current_channel(), _DEFAULT_MANIFEST_URLS[CHANNEL_STABLE])


def evaluate_manifest(manifest: ReleaseManifest, *, current: str | None = None,
                      this_platform: str | None = None) -> UpdateResult:
    """Pure comparison logic, split out from the network fetch so it can
    be tested without any HTTP involved (see tests/test_updater.py)."""
    current = current or current_version()
    this_platform = this_platform or platform_key()
    if not is_newer(manifest.version, current):
        return UpdateResult(available=False, current_version=current)

    artifact = manifest.artifact_for(this_platform)
    if artifact is None:
        # A genuinely newer release exists, but it has no build for this
        # platform (yet, or ever) - honestly "nothing to offer", not an
        # error.
        return UpdateResult(available=False, current_version=current,
                            latest_version=manifest.version, channel=manifest.channel)

    return UpdateResult(
        available=True, current_version=current, latest_version=manifest.version,
        channel=manifest.channel, notes_url=manifest.notes_url,
        download_url=artifact.url, sha256=artifact.sha256, size=artifact.size)


def check_for_updates(*, timeout: float = 10.0) -> UpdateResult:
    """Fetch this channel's manifest and compare it to the running
    build. Raises UpdateCheckError on any network/parse failure - the
    caller decides how to surface that (typically: say "couldn't check
    right now", never crash the app over a failed update check)."""
    import httpx2 as httpx

    url = manifest_url()
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        raw = response.text
    except Exception as exc:  # noqa: BLE001 - any transport failure becomes one clean error
        raise UpdateCheckError(f"could not reach {url}: {exc}") from exc

    try:
        manifest = parse_manifest(raw)
    except ManifestError as exc:
        raise UpdateCheckError(f"release manifest at {url} is invalid: {exc}") from exc

    return evaluate_manifest(manifest)
