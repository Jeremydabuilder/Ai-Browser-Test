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
#
# Found during the macOS Friend Preview Build Checkpoint audit (all three
# are also lazy, function-body imports, same shape as the three above -
# PyInstaller's static analysis finds the bare `import`/`from...import`
# fine, but that only pulls in the .py modules, not each package's own
# data/entry-point metadata):
# 4. `from pypdf import PdfReader` (app/browser/pdf_context.py) - pypdf
#    itself imports `cryptography` for encrypted-PDF support, which is the
#    exact lazy-import-triggers-a-native-extension risk the `cffi` note in
#    requirements.txt already documents for this same code path; collected
#    here for the same reason cffi is declared there.
# 5. `import docx` (app/browser/file_context.py, python-docx) - the
#    package ships a `default.docx` template and other package data under
#    its own install directory that a plain hiddenimport would not carry
#    into the frozen build.
#
# Found during the Phase 22 packaging audit (Phases 16-21: collaboration,
# encrypted sync, knowledge graph, local providers, automation, security,
# MCP client/server):
# 6. `from cryptography.hazmat...` (app/sync/crypto.py, Phase 20/21) -
#    cryptography's hazmat backends are themselves a cffi-based native
#    extension resolved through the same kind of dynamic backend loading
#    keyring uses - the exact risk collect_all exists for. It was already
#    present transitively (pypdf's own optional dependency on it, per the
#    cffi note above), which is precisely why it had gone unnoticed as a
#    *direct* dependency needing its own explicit collection.
COLLECT_ALL_AUDITED = frozenset({
    "keyring", "anthropic", "httpx2", "pypdf", "cffi", "docx", "cryptography",
})
#: Everything else inspected in this audit (automation recorder, security/
#: redaction, MCP client+server, local model providers, knowledge graph,
#: sync's non-crypto modules, collaboration) uses only stdlib or modules
#: already covered above - no further hidden imports or lazy native
#: extensions found (verified by grepping app/ for `importlib`,
#: `__import__`, and every `import`/`from ... import` inside a function
#: body, not just at module scope).
HIDDENIMPORTS = [
    "keyring.backends",
]

COLLECT_ALL = sorted(COLLECT_ALL_AUDITED)

# PySide6's Qt WebEngine (the Chromium process binary, locales, .pak/ICU
# resources) is handled by PyInstaller's own built-in Qt hooks as long as
# PySide6.QtWebEngineCore / QtWebEngineWidgets is importable from the build
# environment - no manual datas needed for it. This has NOT been verified
# end-to-end here because doing so requires actually running the frozen
# build on the target OS (see packaging/windows/README.md and
# packaging/macos/README.md) - it is the first thing the release checklist
# asks a real Windows/macOS build to confirm.
