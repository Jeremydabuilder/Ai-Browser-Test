# PyInstaller spec for the Windows build of PyBrowser.
#
# Build with (from a Windows machine, inside the project's venv):
#   pyinstaller packaging/windows/pybrowser.spec --noconfirm
#
# See packaging/windows/README.md for the full build procedure and the
# first-run checklist this build must pass before it is considered real.

import os
import sys

# SPECPATH is PyInstaller's own global for this spec file's directory - not
# the file path itself, despite the name (see packaging/macos/pybrowser.spec
# for how this was actually caught: the extra dirname() this line used to
# have walked one directory too high on the first real CI run).
sys.path.insert(0, os.path.abspath(os.path.join(SPECPATH, "..", "common")))
from spec_common import REPO_ROOT, DATAS, HIDDENIMPORTS, COLLECT_ALL, VERSION  # noqa: E402

# Analysis/PYZ/EXE/BUNDLE/COLLECT are injected into a spec file's namespace
# by PyInstaller itself - collect_all is not one of them and has to be
# imported explicitly (see packaging/macos/pybrowser.spec for how this was
# actually caught).
from PyInstaller.utils.hooks import collect_all  # noqa: E402

block_cipher = None

# Windows' own "Details" tab (right-click PyBrowser.exe -> Properties) reads
# this, so it should say the same version as everything else rather than a
# separate hardcoded number nobody remembers to update.
from PyInstaller.utils.win32.versioninfo import (  # noqa: E402
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo,
    VarStruct, VSVersionInfo,
)

_version_tuple = tuple(int(p) for p in VERSION.split(".")) + (0, 0, 0, 0)
_version_tuple = _version_tuple[:4]
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_version_tuple, prodvers=_version_tuple),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "AiBrowserTest"),
            StringStruct("FileDescription", "PyBrowser"),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", "PyBrowser"),
            StringStruct("OriginalFilename", "PyBrowser.exe"),
            StringStruct("ProductName", "PyBrowser"),
            StringStruct("ProductVersion", VERSION),
        ])]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

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
    icon=os.path.join(REPO_ROOT, "packaging", "common", "icons", "pybrowser.ico"),
    version=version_info,
)

# --onedir, not --onefile: a --onefile build re-extracts the entire Chromium
# runtime into a temp directory on every launch, which measurably slows
# startup for a Qt WebEngine app this size and complicates keeping a stable
# on-disk path for Qt's own resource lookups. --onedir starts faster and is
# what the Inno Setup installer (installer.iss) packages.
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="PyBrowser",
)
