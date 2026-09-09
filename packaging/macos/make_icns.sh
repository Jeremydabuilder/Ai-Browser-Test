#!/usr/bin/env bash
# Builds packaging/macos/pybrowser.icns from the pre-rendered iconset in
# packaging/common/icons/pybrowser.iconset/. Must run on macOS - iconutil is
# a macOS-only tool, which is why this .icns is not checked into the repo.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ICONSET="$HERE/../common/icons/pybrowser.iconset"
OUT="$HERE/pybrowser.icns"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "make_icns.sh must run on macOS (iconutil is not available elsewhere)." >&2
    exit 1
fi

if [[ ! -d "$ICONSET" ]]; then
    echo "Missing $ICONSET - it should already be in the repo." >&2
    exit 1
fi

iconutil -c icns "$ICONSET" -o "$OUT"
echo "Wrote $OUT"
