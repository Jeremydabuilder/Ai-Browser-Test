"""Phase 22 Part 15: first-run technical validation.

Distinct from app.ui.onboarding.FirstRunDialog (the friendly "welcome to
PyBrowser" tour, shown once) - these are the checks that must pass on
*every* launch before the rest of the app tries to use them, because a
raw traceback from deep inside Database.__init__ or QWebEngineProfile
construction is meaningless to someone who just double-clicked an icon.
Each check returns a plain, human-readable message on failure instead of
raising past this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.config import user_data_dir


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    message: str = ""


def check_writable_profile_dir() -> CheckResult:
    try:
        path = user_data_dir()
        probe = path / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return CheckResult("writable_profile_dir", True)
    except OSError as exc:
        return CheckResult(
            "writable_profile_dir", False,
            f"PyBrowser cannot write to its data folder ({user_data_dir()}). "
            f"Check that you have permission to write there, or that the disk "
            f"is not full.\n\nDetails: {exc}")


def check_database_migration(database_path) -> CheckResult:
    """Actually opens the database (which runs its own migration) rather
    than guessing - a real corrupt-file recovery already lives in
    Database.__init__ (see app/storage/database.py's _open_or_recover),
    so this check's job is only to turn a failure there into a friendly
    message instead of an unhandled exception reaching main()."""
    from app.storage.database import Database

    try:
        db = Database(database_path)
        db.close()
        return CheckResult("database_migration", True)
    except Exception as exc:  # noqa: BLE001 - must never let this reach main() unhandled
        return CheckResult(
            "database_migration", False,
            f"PyBrowser's local database could not be opened or updated.\n\n"
            f"Details: {exc}")


def check_webengine_available() -> CheckResult:
    try:
        import PySide6.QtWebEngineCore  # noqa: F401
        import PySide6.QtWebEngineWidgets  # noqa: F401
        return CheckResult("webengine_available", True)
    except ImportError as exc:
        return CheckResult(
            "webengine_available", False,
            "PyBrowser could not load Qt WebEngine (the web page renderer). "
            "This usually means the installation is incomplete or corrupted - "
            "try reinstalling PyBrowser.\n\n"
            f"Details: {exc}")


def check_keyring_available() -> CheckResult:
    """Soft check only (Part 15 groups this with the others, but a
    missing/broken keyring backend must never block startup - see
    app/agent/keys.py's own KeyringUnavailable handling, which already
    treats this as recoverable everywhere a key is read or written)."""
    if (os.environ.get("PYBROWSER_DISABLE_KEYRING") or "") == "1":
        return CheckResult("keyring_available", True, "Keyring disabled by environment.")
    try:
        import keyring  # noqa: F401
        return CheckResult("keyring_available", True)
    except ImportError as exc:
        return CheckResult(
            "keyring_available", True,  # soft: never blocks startup
            f"The OS keyring is unavailable - saving an AI provider key will "
            f"not work until this is resolved, but PyBrowser will still start. "
            f"Details: {exc}")


def run_all(*, database_path=None) -> list[CheckResult]:
    from app.config import database_path as default_database_path

    results = [check_writable_profile_dir()]
    # A database check is meaningless if the data directory itself is not
    # writable - skip it rather than piling on a second, derivative error.
    if results[0].ok:
        results.append(check_database_migration(database_path or default_database_path()))
    results.append(check_webengine_available())
    results.append(check_keyring_available())
    return results


def fatal_failures(results: list[CheckResult]) -> list[CheckResult]:
    """Which of ``results`` should stop startup outright - currently the
    profile dir, database, and WebEngine checks; the keyring check is
    soft (see check_keyring_available) and never appears here."""
    return [r for r in results if not r.ok and r.name != "keyring_available"]
