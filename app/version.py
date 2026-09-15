"""Phase 22: the one place version/channel/build-identity logic lives.

``app.__version__`` stays the single source of truth for the version
*string* (packaging specs and installers already import it - see
packaging/common/spec_common.py's own comment on why). This module adds
what a release process needs on top of that string: parsing it as
semver, comparing two versions, naming the build channel, and reporting
what commit a running build was built from - without introducing a
second place a build could disagree with itself about its own version.
"""

from __future__ import annotations

import os
import platform
import re
from dataclasses import dataclass

from app import APP_NAME, __version__

#: Build channels (Part 8). "stable" is the default for anyone running
#: from source or an unlabeled build; a packaged release sets
#: PYBROWSER_CHANNEL at build time (see the release workflows) so an
#: About dialog / update check never has to guess.
CHANNEL_STABLE = "stable"
CHANNEL_PREVIEW = "preview"
CHANNEL_NIGHTLY = "nightly"
CHANNELS = (CHANNEL_STABLE, CHANNEL_PREVIEW, CHANNEL_NIGHTLY)

_ENV_CHANNEL = "PYBROWSER_CHANNEL"
#: Set by CI to the release commit SHA (see the release workflows' "Read
#: the app version" step, extended in this phase to also export this) -
#: never assumed to exist, since most runs are a plain `python main.py`
#: from a working tree with no such env var.
_ENV_COMMIT = "PYBROWSER_BUILD_COMMIT"


def current_channel() -> str:
    value = (os.environ.get(_ENV_CHANNEL) or "").strip().lower()
    return value if value in CHANNELS else CHANNEL_STABLE


_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-.]+))?(?:\+(?P<build>[0-9A-Za-z-.]+))?$"
)


class InvalidVersion(ValueError):
    """A version string is not valid semver (major.minor.patch[-pre][+build])."""


@dataclass(frozen=True, order=False)
class Version:
    """A parsed semver version. Comparison follows semver 2.0.0's own
    precedence rules: numeric core first, then a prerelease tag makes a
    version *older* than the same core without one (1.0.0-rc.1 < 1.0.0),
    and build metadata (the ``+...`` suffix) never affects ordering at
    all - it is carried only for display."""

    major: int
    minor: int
    patch: int
    prerelease: str = ""
    build: str = ""

    @property
    def core(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def _prerelease_key(self) -> tuple:
        if not self.prerelease:
            return (1,)  # no prerelease sorts AFTER any prerelease of the same core
        parts = []
        for part in self.prerelease.split("."):
            if part.isdigit():
                parts.append((0, int(part)))
            else:
                parts.append((1, part))
        return (0, tuple(parts))

    def __lt__(self, other: "Version") -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return (self.core, self._prerelease_key()) < (other.core, other._prerelease_key())

    def __le__(self, other: "Version") -> bool:
        return self == other or self < other

    def __gt__(self, other: "Version") -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return other < self

    def __ge__(self, other: "Version") -> bool:
        return self == other or self > other

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            text += f"-{self.prerelease}"
        if self.build:
            text += f"+{self.build}"
        return text


def parse_version(text: str) -> Version:
    """Parse a semver string. Raises InvalidVersion for anything else -
    an update checker or a release manifest must never guess at a
    malformed version, only refuse it (Part 3/4's "reject mismatches"
    posture applies just as much to a garbled version number)."""
    match = _SEMVER_RE.match((text or "").strip())
    if match is None:
        raise InvalidVersion(f"not a valid semantic version: {text!r}")
    groups = match.groupdict()
    return Version(
        major=int(groups["major"]), minor=int(groups["minor"]), patch=int(groups["patch"]),
        prerelease=groups["prerelease"] or "", build=groups["build"] or "")


def compare_versions(a: str, b: str) -> int:
    """-1 if a < b, 0 if equal (ignoring build metadata), 1 if a > b."""
    va, vb = parse_version(a), parse_version(b)
    if va < vb:
        return -1
    if va > vb:
        return 1
    return 0


def is_newer(candidate: str, current: str) -> bool:
    """True if ``candidate`` is a newer release than ``current``."""
    return compare_versions(candidate, current) > 0


def current_version() -> str:
    return __version__


def build_commit() -> str:
    """The commit a packaged build was built from, or '' for a plain
    checkout with no such env var set (e.g. `python main.py` in dev)."""
    return (os.environ.get(_ENV_COMMIT) or "").strip()


@dataclass(frozen=True)
class BuildInfo:
    app_name: str
    version: str
    channel: str
    commit: str
    platform: str
    python_version: str

    def to_dict(self) -> dict:
        return {
            "app_name": self.app_name, "version": self.version, "channel": self.channel,
            "commit": self.commit, "platform": self.platform,
            "python_version": self.python_version,
        }

    def display_lines(self) -> list[str]:
        """Plain-text lines for an About dialog or a diagnostics report -
        never anything beyond what's already public in a build (Part 2:
        no secrets)."""
        lines = [f"{self.app_name} {self.version}", f"Channel: {self.channel}"]
        if self.commit:
            lines.append(f"Build: {self.commit[:12]}")
        lines.append(f"Platform: {self.platform}")
        lines.append(f"Python: {self.python_version}")
        return lines


def build_info() -> BuildInfo:
    return BuildInfo(
        app_name=APP_NAME, version=current_version(), channel=current_channel(),
        commit=build_commit(), platform=_platform_label(), python_version=platform.python_version())


def _platform_label() -> str:
    system = platform.system()
    if system == "Darwin":
        return f"macOS {platform.mac_ver()[0] or ''}".strip()
    if system == "Windows":
        return f"Windows {platform.release()}"
    return system or "Unknown"
