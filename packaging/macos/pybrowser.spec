# PyInstaller spec for the macOS build of PyBrowser.
#
# Must be run ON macOS (PyInstaller does not cross-compile):
#   pyinstaller packaging/macos/pybrowser.spec --noconfirm
#
# See packaging/macos/README.md for the full build procedure, including
# turning dist/PyBrowser.app into a signed/notarized .dmg.

import os
import sys

# SPECPATH is PyInstaller's own global for this spec file's directory - not
# the file path itself, despite the name (confirmed the hard way: the extra
# dirname() this line used to have walked one directory too high and made
# `from spec_common import ...` fail with "No module named 'spec_common'"
# on the first real CI run).
sys.path.insert(0, os.path.abspath(os.path.join(SPECPATH, "..", "common")))
from spec_common import REPO_ROOT, DATAS, HIDDENIMPORTS, COLLECT_ALL, VERSION  # noqa: E402

# Analysis/PYZ/EXE/BUNDLE/COLLECT are injected into a spec file's namespace
# by PyInstaller itself - collect_all is not one of them and has to be
# imported explicitly (caught on the second real CI run: everything up to
# this point built cleanly, then died with "NameError: name 'collect_all'
# is not defined").
from PyInstaller.utils.hooks import collect_all  # noqa: E402

block_cipher = None

# collect_all() returns plain (dest, source) pairs meant for Analysis's own
# datas=/binaries= constructor arguments - NOT the 3-tuple (dest, src,
# typecode) TOC entries Analysis builds internally. Appending these to
# a.datas/a.binaries *after* Analysis() (as this spec used to) mixed 2-tuples
# into an otherwise all-3-tuple TOC list, which blew up inside COLLECT with
# "ValueError: not enough values to unpack (expected 3, got 2)" on the third
# real CI run (macOS run 34420258143) to get this far. Merging them into the
# constructor arguments instead means Analysis normalizes everything itself.
extra_datas: list = []
extra_binaries: list = []
extra_hiddenimports: list = list(HIDDENIMPORTS)
for pkg in COLLECT_ALL:
    collected = collect_all(pkg)
    extra_datas += collected[0]
    extra_binaries += collected[1]
    extra_hiddenimports += collected[2]

a = Analysis(
    [os.path.join(REPO_ROOT, "main.py")],
    pathex=[REPO_ROOT],
    binaries=extra_binaries,
    datas=DATAS + extra_datas,
    hiddenimports=extra_hiddenimports,
    # Overrides PyInstaller's own hook-PySide6.QtWebEngineCore.py with a
    # corrected copy (see pyinstaller_hooks/hook-PySide6.QtWebEngineCore.py
    # for the full story): the built-in hook picks the wrong
    # QtWebEngineCore.framework "version" directory on macOS, which makes
    # the QtWebEngineProcess helper end up somewhere Qt's own runtime search
    # never looks, aborting PyBrowser (SIGABRT) the instant it needs to show
    # a web view - a real crash confirmed on CI run 34423969649/34422919205.
    hookspath=[os.path.join(SPECPATH, "pyinstaller_hooks")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)

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
    # PyInstaller 6+ defaults to nesting bundled frameworks under
    # Contents/MacOS/_internal/ instead of the standard macOS
    # Contents/Frameworks/ layout. Qt WebEngine's own compiled-in helper
    # search only checks Contents/Frameworks/.../QtWebEngineProcess.app and
    # Contents/MacOS/QtWebEngineProcess - neither matches _internal, so the
    # browser process aborts (SIGABRT) the moment it needs a web view,
    # exactly as seen on a real CI run (macOS run 34422919205, the first
    # run where the smoke test's own diagnostics survived to actually show
    # this). "." restores the pre-6.0 flat layout BUNDLE expects.
    contents_directory=".",
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
