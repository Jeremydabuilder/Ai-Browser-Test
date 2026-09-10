# Corrected copy of PyInstaller's own hook-PySide6.QtWebEngineCore.py, wired in
# via this macOS spec's hookspath so it takes precedence over the built-in one.
#
# Root cause (found by reading PyInstaller 6.22.2's own
# PyInstaller/utils/hooks/qt/__init__.py, QtLibraryInfo.collect_qtwebengine_files):
# on macOS it picks the QtWebEngineCore.framework "version" directory by sorting
# os.listdir(.../Versions) and taking the alphabetically-last entry that isn't
# "Current". PySide6's PyPI wheel ships a stray "Resources" directory directly
# under Versions/ alongside the real version directory "A" - and "Resources" sorts
# after "A", so the hook picks "Resources" as if it were the framework version. It
# then collects the Helpers directory (which contains the QtWebEngineProcess helper
# app PyBrowser needs to render any web page) into Versions/Resources/Helpers - a
# path Qt's own Versions/Current symlink (which always points at the *real* version,
# "A") never resolves to. At runtime, PyBrowser can never find its own WebEngine
# helper process and aborts (SIGABRT) the instant it needs to show a web view -
# i.e. immediately on launch. Confirmed on a real CI run (macOS run 34423969649):
#
#   The following paths were searched for Qt WebEngine Process:
#     .../Contents/Frameworks/PySide6/Qt/lib/QtWebEngineCore.framework/Helpers/QtWebEngineProcess.app/Contents/MacOS/QtWebEngineProcess
#     .../Contents/MacOS/QtWebEngineProcess
#   but could not find it.
#
# Fixed by monkeypatching os.listdir for the duration of the one call that walks
# Versions/, filtering out "Resources" so the real version ("A") is the only
# candidate left - scoped as tightly as possible (only touches listings of a
# directory literally named "Versions") since this patches a stdlib function.
from PyInstaller.utils.hooks.qt import \
    add_qt6_dependencies, pyside6_library_info

if pyside6_library_info.version is not None:
    if pyside6_library_info.version < [6, 2, 2]:
        raise SystemExit("ERROR: PyInstaller's QtWebEngine support requires Qt6 6.2.2 or later!")

    hiddenimports, binaries, datas = add_qt6_dependencies(__file__)

    import os

    _real_listdir = os.listdir

    def _listdir_without_spurious_resources_version(path):
        entries = _real_listdir(path)
        if os.path.basename(os.path.normpath(path)) == "Versions" and "Resources" in entries:
            entries = [entry for entry in entries if entry != "Resources"]
        return entries

    os.listdir = _listdir_without_spurious_resources_version
    try:
        webengine_binaries, webengine_datas = pyside6_library_info.collect_qtwebengine_files()
    finally:
        os.listdir = _real_listdir

    binaries += webengine_binaries
    datas += webengine_datas

    hiddenimports += ['PySide6.QtPrintSupport']
