"""CI-only test entry point.

Only needed for CI: `python -m unittest discover` is still the
documented, supported way to run this suite locally, and works fine
there. On CI hardware specifically, PySide6/QtWebEngine intermittently
segfaults *after* every test has already passed, while CPython tears
down the last QWebEngine objects during normal interpreter shutdown
(GC finalizers, atexit hooks, Qt's own static destructors) - a known,
pre-existing Qt WebEngine issue unrelated to this app's own code, only
ever seen after a clean run. Racing that crash against unittest's own
final "OK"/"Ran N tests" summary line (whichever gets written to the
log first) is what release.yml's test steps used to have to forgive
with a regex.

This sidesteps the crash instead of racing it: run the suite exactly
as `unittest discover` would, flush the result the moment it's known,
then exit immediately via os._exit() - which skips atexit hooks, GC
finalizers, and Qt/WebEngine's own static destructors entirely, so
that teardown path (and its crash) never runs. A real mid-suite
failure is unaffected - it still shows up in the output and still
produces a nonzero exit code, verified below.
"""

from __future__ import annotations

import os
import sys
import unittest

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir="tests")
    runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=2)
    result = runner.run(suite)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
