"""Test runner entry point that works around a confirmed QtWebEngine/CPython
interpreter-shutdown crash - use this instead of ``python -m unittest`` for
any run that touches a real QWebEngineView/Page (almost every test module
in this suite, transitively, since app.browser is imported everywhere).

The crash: "Release of profile requested but WebEnginePage still not
deleted" followed by a segfault with no Python frame on the crashing
thread. Root-caused during the release-blocker investigation with a
minimal, application-code-free reproducer (~15 lines: QApplication, a
QWebEngineView/Page on QWebEngineProfile.defaultProfile(), one
about:blank load, deleteLater() + processEvents(), then a normal process
exit) - proving this is a PySide6/QtWebEngine 6.11.2 (offscreen platform)
teardown incompatibility with CPython's own interpreter finalization
(Py_Finalize), not an ownership/parenting bug in this codebase's
app/browser/* code:

  - No explicit teardown at all (never call deleteLater(), just let the
    process exit) - does NOT crash.
  - Explicit page/view deleteLater() + up to 50 rounds of
    processEvents() + 2.5s of real wall-clock sleep, THEN a normal
    process exit - STILL crashes, identically.
  - The exact same teardown, but exiting via os._exit() instead of a
    normal return/sys.exit() (skipping CPython's own finalization
    entirely) - does NOT crash.

So the crash is not a matter of deleting things in the right order or
pumping the event loop enough - it happens *after* all application and
Qt-visible cleanup has already finished correctly, inside CPython's own
shutdown sequence tearing down the QtWebEngineCore/QtWebEngineWidgets
extension modules. The only known working mitigation is to never let
that finalization sequence run at all in a process that ever created a
real (loaded) WebEngine page - i.e. skip straight to _exit().

This is safe for a test run specifically because unittest has already
fully executed and reported every test result by the time this runs;
skipping Python's normal atexit/GC-at-shutdown machinery loses nothing
a test's own tearDown() didn't already handle (database files, temp
dirs, etc. - all closed/cleaned up before this point, same as any
ordinary test run).

Usage (drop-in replacement for `python -m unittest ARGS -v`):
    python3 tests/run.py ARGS [-v]

Any argument unittest's TestLoader.loadTestsFromName() accepts works:
a module (`tests.test_mcp_phase2`), a class
(`tests.test_mcp_phase2.SecurityTests`), or a single test
(`tests.test_mcp_phase2.SecurityTests.test_foo`).
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv: list[str]) -> None:
    verbosity = 2 if "-v" in argv or "--verbose" in argv else 1
    names = [a for a in argv if not a.startswith("-")]
    if not names:
        print("usage: python3 tests/run.py <test.module.or.Class.or.method> [...] [-v]",
              file=sys.stderr)
        os._exit(2)

    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in names:
        suite.addTests(loader.loadTestsFromName(name))

    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)

    # Flush before _exit() - normal interpreter shutdown would do this for
    # us, but we are deliberately skipping that (see module docstring), so
    # buffered output must be flushed explicitly or it can be lost.
    sys.stdout.flush()
    sys.stderr.flush()

    # os._exit(), not sys.exit(): sys.exit() raises SystemExit, which still
    # unwinds through and triggers CPython's normal interpreter
    # finalization (Py_Finalize) - exactly the sequence that crashes. This
    # skips it entirely by terminating the process directly.
    os._exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main(sys.argv[1:])
