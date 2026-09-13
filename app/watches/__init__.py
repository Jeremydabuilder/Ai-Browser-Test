"""Page Watches: monitor a page (or a selected section of one) over time and
notify only on a meaningful change.

Three modules, deliberately separate:

* :mod:`app.watches.model` - the data: what a Watch is, and its states.
* :mod:`app.watches.detection` - pure, deterministic comparison. No Qt, no
  network, no browser - just text in, a verdict out. This is what decides
  "did anything worth mentioning change", not an LLM (see its docstring for
  where that boundary sits).
* :mod:`app.watches.runner` - the timer that decides *when* to check a
  Watch and turns detection's verdict into state changes and
  notifications. Reuses app/missions/task_runner.py's own QTimer rather
  than starting a second background timing system - see its docstring.
"""
