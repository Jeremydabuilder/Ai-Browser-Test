"""Phase 22 Part 13/14: crash-recovery detection and Safe Mode.

Deliberately a flat JSON file next to the database, not a row in it: the
whole point is to detect "did the last session end cleanly", including
the case where the database itself failed to open or migrate - a check
that lives inside the thing that might be broken cannot report on it.

Flow:

* ``mark_session_started()`` - called once, early in main(), before the
  event loop runs. If the marker says the previous session never called
  ``mark_session_clean()``, that session crashed or was killed; this
  returns that fact (and bumps a crash counter) so main.py can decide
  whether to offer "Restore previous session" (Part 13) and/or suggest
  Safe Mode (Part 14).
* ``mark_session_clean()`` - called once, right before the event loop
  returns normally (a clean exit). Resets the crash counter to 0.

Safe Mode itself is just a set of flags (``SafeModeFlags``) that main.py
and MainWindow read to skip constructing things - it does not know *why*
it was requested (CLI flag, environment variable, or auto-triggered by
repeated crashes all end up calling the same thing), keeping the policy
decision in one place (``resolve_safe_mode``) and the mechanism in
another.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from app.config import user_data_dir

#: After this many consecutive unclean shutdowns, Safe Mode is suggested
#: automatically even without an explicit flag - a startup loop (a bad
#: MCP server, a broken sync folder, a corrupt scheduled task) that
#: crashes the app every launch must not require the user to already
#: know about a --safe-mode flag to escape it.
AUTO_SAFE_MODE_THRESHOLD = 2

_ENV_SAFE_MODE = "PYBROWSER_SAFE_MODE"


def _marker_path() -> Path:
    return user_data_dir() / "session_state.json"


def _read_marker() -> dict:
    path = _marker_path()
    if not path.exists():
        return {"clean": True, "crash_count": 0}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A corrupt marker file must never itself crash startup - treat
        # it as "unknown", which is the same as "not clean" below.
        return {"clean": False, "crash_count": 0}


def _write_marker(data: dict) -> None:
    try:
        _marker_path().write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass  # A marker write failing must never be fatal to startup.


@dataclass(frozen=True)
class SessionStartResult:
    crashed_last_session: bool
    crash_count: int


def mark_session_started() -> SessionStartResult:
    previous = _read_marker()
    crashed = not previous.get("clean", True)
    crash_count = int(previous.get("crash_count", 0)) + (1 if crashed else 0)
    if not crashed:
        crash_count = 0
    _write_marker({"clean": False, "crash_count": crash_count})
    return SessionStartResult(crashed_last_session=crashed, crash_count=crash_count)


def mark_session_clean() -> None:
    _write_marker({"clean": True, "crash_count": 0})


@dataclass(frozen=True)
class SafeModeFlags:
    enabled: bool
    reason: str = ""

    @property
    def disable_mcp(self) -> bool:
        return self.enabled

    @property
    def disable_schedules(self) -> bool:
        return self.enabled

    @property
    def disable_sync(self) -> bool:
        return self.enabled


def resolve_safe_mode(*, cli_flag: bool = False, crash_count: int = 0) -> SafeModeFlags:
    """Decide whether Safe Mode is active for this launch. Checked in
    order: an explicit CLI flag, then the environment variable, then
    automatic triggering after repeated crashes - the first true reason
    wins and is what gets reported (e.g. in the About/diagnostics view),
    so a user always knows *why* they are in Safe Mode."""
    if cli_flag:
        return SafeModeFlags(enabled=True, reason="requested with --safe-mode")
    if (os.environ.get(_ENV_SAFE_MODE) or "").strip().lower() in ("1", "true", "yes"):
        return SafeModeFlags(enabled=True, reason=f"{_ENV_SAFE_MODE} is set")
    if crash_count >= AUTO_SAFE_MODE_THRESHOLD:
        return SafeModeFlags(
            enabled=True,
            reason=f"PyBrowser did not shut down cleanly {crash_count} times in a row")
    return SafeModeFlags(enabled=False)
