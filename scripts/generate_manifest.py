#!/usr/bin/env python3
"""Phase 22 Part 17: generate a release manifest from built artifacts.

Usage (called from the release workflow, after both platform artifacts
have been downloaded into one place - see .github/workflows/release-
manifest.yml):

    python scripts/generate_manifest.py \\
        --version 0.1.1 --channel preview \\
        --notes-url https://github.com/.../releases/tag/v0.1.1 \\
        --windows-url https://.../PyBrowser-Setup.exe --windows-file dist/PyBrowser-Setup.exe \\
        --macos-url https://.../PyBrowser.dmg --macos-file dist/PyBrowser.dmg \\
        --out manifest.json

Any artifact whose --*-file is omitted is simply left out of the
manifest (Part 17 doesn't require every release to ship both
platforms at once - see app.updater.checker's "no update for you"
handling of a manifest missing this platform).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.updater.manifest import Artifact, build_manifest  # noqa: E402
from app.updater.verify import sha256_of_file  # noqa: E402


def _artifact_from_file(url: str, file_path: str) -> Artifact:
    path = Path(file_path)
    return Artifact(url=url, sha256=sha256_of_file(path), size=path.stat().st_size)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--notes-url", default="")
    parser.add_argument("--windows-url")
    parser.add_argument("--windows-file")
    parser.add_argument("--macos-url")
    parser.add_argument("--macos-file")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    artifacts = {}
    if args.windows_url and args.windows_file:
        artifacts["windows"] = _artifact_from_file(args.windows_url, args.windows_file)
    if args.macos_url and args.macos_file:
        artifacts["macos"] = _artifact_from_file(args.macos_url, args.macos_file)

    manifest = build_manifest(
        version=args.version, channel=args.channel, artifacts=artifacts, notes_url=args.notes_url)

    out_path = Path(args.out)
    out_path.write_text(manifest.to_json(), encoding="utf-8")
    print(f"Wrote {out_path} ({len(artifacts)} artifact(s)):")
    print(manifest.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
