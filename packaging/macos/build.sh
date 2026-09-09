#!/usr/bin/env bash
# Builds PyBrowser.app for macOS, then (if create-dmg is installed) a .dmg.
#
# Must run ON macOS - PyInstaller does not cross-compile, and .icns/.dmg
# creation both depend on macOS-only tools (iconutil, hdiutil/create-dmg).
#
# Run from the repository root:
#   bash packaging/macos/build.sh
#
# This script has NOT been run on a real Mac as part of this change - it was
# written from inspecting the app's dependencies and entry point, not
# verified end-to-end. Treat a first real run as a test of the script
# itself; see packaging/macos/README.md for what to check afterward.
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This must be run on macOS - PyInstaller does not cross-compile." >&2
    exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
cd "$REPO_ROOT"

echo "== Building the .icns app icon =="
bash "$HERE/make_icns.sh"

echo "== Installing build dependencies =="
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
python3 -m pip install "pyinstaller>=6.0" "pyinstaller-hooks-contrib>=2024.0"

echo "== Cleaning previous build output =="
rm -rf build dist

echo "== Running PyInstaller =="
pyinstaller packaging/macos/pybrowser.spec --noconfirm

echo "== dist/PyBrowser.app built =="
ls -ld dist/PyBrowser.app

echo ""
echo "PyBrowser.app is unsigned and not notarized - see packaging/SIGNING.md."
echo "Gatekeeper will refuse to open it without at least ad-hoc signing:"
echo "  codesign --force --deep --sign - dist/PyBrowser.app"
echo "(That is a LOCAL, throwaway signature - it does not satisfy Gatekeeper"
echo "on another Mac. Real distribution needs a Developer ID + notarization.)"
echo ""

if command -v create-dmg >/dev/null 2>&1; then
    echo "== Building the .dmg with create-dmg =="
    mkdir -p packaging/macos/output
    create-dmg \
        --volname "PyBrowser" \
        --window-size 540 380 \
        --icon-size 96 \
        --icon "PyBrowser.app" 140 160 \
        --app-drop-link 400 160 \
        "packaging/macos/output/PyBrowser-0.1.0.dmg" \
        "dist/PyBrowser.app" || {
            echo "create-dmg failed - dist/PyBrowser.app is still usable on its own." >&2
        }
else
    echo "create-dmg not found (brew install create-dmg) - skipping .dmg."
    echo "dist/PyBrowser.app is still a runnable app on its own."
fi

echo ""
echo "Next: work through packaging/macos/README.md's first-run checklist"
echo "against dist/PyBrowser.app before calling this build real."
