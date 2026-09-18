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

import gc

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


def close_window(window, app) -> None:
    """The one correct way to tear down a MainWindow built in a test:
    close() it, deleteLater() it, pump the app once so the deferred
    delete actually runs, then force a synchronous gc.collect() - right
    here, on the GUI thread, at a moment known to be safe.

    Why the gc.collect() is necessary and not just cosmetic: a MainWindow
    is a QWidget with dozens of its own buttons/actions/tabs connected to
    its own bound methods (completely ordinary, idiomatic Qt code) - that
    is a Python reference cycle (self -> child widget -> connection ->
    bound method of self -> self) by construction, on every single
    MainWindow instance, and there is no practical way to avoid it short
    of weakref-wrapping essentially every widget-to-self connection in
    the UI layer (out of proportion to the problem: ordinary refcounting
    still reclaims a MainWindow that ISN'T part of a live worker-thread
    hazard just fine; the cycle only matters when it also holds
    something like AgentSession's QThread). Left alone, this cycle is
    only reclaimed whenever Python's cyclic GC next happens to trip its
    own allocation threshold - which can happen on ANY thread - and a
    test suite that builds many MainWindows across many test modules in
    one process accumulates them as garbage between those collections.
    Confirmed directly during the release-blocker investigation: a batch
    of tests.test_live_config across two AgentPanel test classes left up
    to 28 already-stopped ("claude-worker") QThreads alive simultaneously
    this way, and when cyclic GC eventually swept them, it did so from
    whichever background thread's allocation tripped it - reproducing
    the exact "QObject::killTimer: Timers cannot be stopped from another
    thread" warning chased in this investigation.

    This is a test-harness leak, not a production bug: the real
    application builds exactly one MainWindow for the life of the
    process and only ever discards it at application exit (see main.py's
    own os._exit() - a different, already-fixed problem). Calling this
    after every test's window.close() keeps that garbage from piling up
    across the run instead.
    """
    window.close()
    window.deleteLater()
    app.processEvents()
    gc.collect()
