# PyInstaller spec for the Windows build of PyBrowser.
#
# Build with (from a Windows machine, inside the project's venv):
#   pyinstaller packaging/windows/pybrowser.spec --noconfirm
#
# See packaging/windows/README.md for the full build procedure and the
# first-run checklist this build must pass before it is considered real.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(SPECPATH)), "..", "common"))
from spec_common import REPO_ROOT, DATAS, HIDDENIMPORTS, COLLECT_ALL  # noqa: E402

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
    icon=os.path.join(REPO_ROOT, "packaging", "common", "icons", "pybrowser.ico"),
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
