# PyInstaller spec for the macOS build of PyBrowser.
#
# Must be run ON macOS (PyInstaller does not cross-compile):
#   pyinstaller packaging/macos/pybrowser.spec --noconfirm
#
# See packaging/macos/README.md for the full build procedure, including
# turning dist/PyBrowser.app into a signed/notarized .dmg.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(SPECPATH)), "..", "common"))
from spec_common import REPO_ROOT, DATAS, HIDDENIMPORTS, COLLECT_ALL, VERSION  # noqa: E402

block_cipher = None

a = Analysis(
    [os.path.join(REPO_ROOT, "main.py")],
    pathex=[REPO_ROOT],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDENIMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)

for pkg in COLLECT_ALL:
    collected = collect_all(pkg)
    a.datas += collected[0]
    a.binaries += collected[1]
    a.hiddenimports += collected[2]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PyBrowser",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="PyBrowser",
)

app = BUNDLE(
    coll,
    name="PyBrowser.app",
    icon=os.path.join(REPO_ROOT, "packaging", "macos", "pybrowser.icns"),
    bundle_identifier="com.aibrowsertest.pybrowser",
    info_plist={
        "CFBundleName": "PyBrowser",
        "CFBundleDisplayName": "PyBrowser",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "NSHighResolutionCapable": True,
        # PyBrowser makes real HTTP(S) requests to whatever a person
        # navigates to, and to the AI provider they configure - both are
        # already HTTPS-only endpoints (see app/browser/profile.py and
        # app/agent/*), so App Transport Security's default (HTTPS-only,
        # no exceptions) is left in place rather than relaxed for it.
        "NSHumanReadableCopyright": "PyBrowser is an independent, "
                                    "open-source project.",
    },
)
