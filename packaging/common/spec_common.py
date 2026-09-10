"""Shared PyInstaller configuration for both platform specs.

Kept in one place so a dependency risk found on one platform (a missing
keyring backend, a provider SDK's lazy import) is fixed for both, instead of
drifting between two hand-maintained spec files.

Import this from windows/pybrowser.spec and macos/pybrowser.spec:

    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(SPECPATH), "..", "common"))
    from spec_common import REPO_ROOT, DATAS, HIDDENIMPORTS, COLLECT_ALL

PyInstaller execs .spec files with ``SPECPATH`` predefined as the directory
containing the spec - that's how each platform spec locates this module and
the repo root without hardcoding an absolute path.
"""

from __future__ import annotations

import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# The one place a build reads PyBrowser's version from - app/__init__.py.
# Importing it (rather than re-parsing the file with a regex) means a spec
# and the app can never quietly disagree about what version they are.
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
from app import __version__ as VERSION  # noqa: E402

# -- data files ---------------------------------------------------------
# Py's artwork and the app icon are resolved at runtime relative to
# app/ui/__file__ (see app/ui/mascot.py and app/config.py:icon_path) - both
# read from app/ui/assets/, so bundling that one directory covers both.
#
# app/browser/profile.py:_install_automation_script also loads a sibling
# file this way (Path(__file__).with_name("page_script.js")) - missed by
# the original __file__-relative-path grep before this spec was written,
# because PyInstaller only bundles .py sources into the frozen app (as
# bytecode in the PYZ archive); a .js file sitting next to profile.py on
# disk is invisible to it unless listed here explicitly. Without this, a
# frozen build's BrowserProfile.__init__ raises FileNotFoundError on
# startup - a real crash caught on a real macOS CI run (run 34425593612)
# once the app got past every earlier packaging bug and actually tried to
# launch. Templates and generated pages elsewhere in the app are inline
# Python string constants, not files on disk, so this is the only other
# case.
DATAS = [
    (os.path.join(REPO_ROOT, "app", "ui", "assets"), "app/ui/assets"),
    (os.path.join(REPO_ROOT, "app", "browser", "page_script.js"), "app/browser"),
]

# -- hidden imports ---------------------------------------------------------
# Three real risk areas found by inspecting the app before writing this spec:
#
# 1. keyring's backend selection (app/agent/keys.py -> keyring) happens via
#    importlib.metadata entry points at runtime, not a static import
#    PyInstaller's analysis can see - without this, a frozen build silently
#    has no working OS-keychain backend and every "Configure AI Agent" save
#    fails. collect_all (below) pulls in the backend modules AND their
#    dist-info entry-point metadata, which is what keyring actually reads.
# 2. `import anthropic` in app/agent/claude_client.py is inside a function
#    (lazy, so the agent works before any provider is configured), which
#    PyInstaller's AST scan still finds - but the SDK's own optional/lazy
#    imports are safer covered with collect_all too.
# 3. `import httpx2 as httpx` (app/agent/openai_compatible.py, used for the
#    Groq/OpenRouter/Gemini OpenAI-compatible paths) is a real separate PyPI
#    package, not an alias for `httpx` - collected explicitly so it is not
#    mistaken for the more commonly-hooked `httpx`.
HIDDENIMPORTS = [
    "keyring.backends",
]

COLLECT_ALL = [
    "keyring",
    "anthropic",
    "httpx2",
]

# PySide6's Qt WebEngine (the Chromium process binary, locales, .pak/ICU
# resources) is handled by PyInstaller's own built-in Qt hooks as long as
# PySide6.QtWebEngineCore / QtWebEngineWidgets is importable from the build
# environment - no manual datas needed for it. This has NOT been verified
# end-to-end here because doing so requires actually running the frozen
# build on the target OS (see packaging/windows/README.md and
# packaging/macos/README.md) - it is the first thing the release checklist
# asks a real Windows/macOS build to confirm.
