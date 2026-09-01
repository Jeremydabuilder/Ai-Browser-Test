"""One QWebEngineProfile for the whole test process.

Every test module that drives a real browser used to build its own
`BrowserProfile` in `setUpModule` and delete it in `tearDownModule`. That was
fine until the new-tab page arrived: only one profile per process can serve a
custom URL scheme (see `app/browser/newtab.py` -> `claim_scheme`), so the
second module to run got a profile whose `pybrowser://newtab/` never loads.

Sharing one profile fixes that, and incidentally removes the "Release of
profile requested but WebEnginePage still not deleted" warnings that every
module produced on the way out.

The profile is never deleted. It is torn down when the process exits, which is
what a browser profile's lifetime looks like in the real application too.
"""

from __future__ import annotations

from app.browser.profile import BrowserProfile

_PROFILE: BrowserProfile | None = None


def shared_profile() -> BrowserProfile:
    """The process-wide profile. Call from `setUpModule`, never delete it."""
    global _PROFILE
    if _PROFILE is None:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:  # pragma: no cover - a caller forgot to build one
            raise RuntimeError("create the QApplication before the profile")
        _PROFILE = BrowserProfile(app)
    return _PROFILE
