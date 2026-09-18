"""Entry point for the browser.

Run with:  python main.py [url ...]
Add --safe-mode to start with MCP/sync/schedules disabled and one blank
tab (see app/startup_state.py) - useful when a bad plugin/provider/session
state is causing repeated startup failures.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys

# Qt WebEngine must be imported before QApplication is constructed so that the
# Chromium runtime can install its hooks. Importing the module here is enough.
from PySide6.QtCore import QCoreApplication, QUrl  # noqa: F401  (import order matters)
from PySide6.QtGui import QIcon
from PySide6.QtWebEngineCore import QWebEngineProfile  # noqa: F401
from PySide6.QtWidgets import QApplication, QMessageBox

from app import APP_NAME, ORG_NAME, __version__
from app.browser.newtab import register_scheme
from app.browser.profile import BrowserProfile
from app.config import database_path, icon_path, log_path
from app.startup_state import mark_session_clean, mark_session_started, resolve_safe_mode
from app.storage import Database
from app.ui import theme
from app.ui.main_window import MainWindow


def configure_logging() -> None:
    """Write warnings and up to a rotating file, and catch what nothing else
    would: an uncaught exception in a windowed, no-console packaged build has
    no terminal to print a traceback to, so without this the app just
    silently vanishes with no way for anyone to say what happened.

    Deliberately just the exception's own type/message/traceback - never any
    application data. Nothing here should ever be handed an API key: the
    Credential type callers pass around exposes only a fingerprint for this
    exact reason (see app/agent/credentials.py), never the key itself.
    """
    handler = logging.handlers.RotatingFileHandler(
        log_path(), maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.WARNING)

    def log_uncaught(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.getLogger("pybrowser.crash").critical(
            "Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = log_uncaught


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pybrowser", description=f"{APP_NAME} - a Python desktop web browser")
    parser.add_argument("urls", nargs="*", help="URLs to open on startup")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument(
        "--safe-mode", action="store_true",
        help="Start with MCP connections, scheduled tasks/watches, and sync disabled, "
             "and one blank tab - use this if PyBrowser is failing to start normally.")
    return parser.parse_args(argv)


def _show_fatal_startup_error(app: QApplication, message: str) -> None:
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(f"{APP_NAME} cannot start")
    box.setText(message)
    box.exec()


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = parse_args(argv if argv is not None else sys.argv[1:])

    # Chromium reads its scheme registry once, before the application exists,
    # so pybrowser:// has to be declared here and not a line later.
    register_scheme()

    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setOrganizationName(ORG_NAME)
    QCoreApplication.setApplicationVersion(__version__)

    app = QApplication(sys.argv[:1])
    # The bundled file is what actually shows up on Windows/macOS; fromTheme
    # only resolves on Linux desktops with a matching icon theme installed,
    # so it is a fallback, never the primary source.
    bundled_icon = QIcon(str(icon_path()))
    app.setWindowIcon(bundled_icon if not bundled_icon.isNull() else QIcon.fromTheme("web-browser"))
    # PyBrowser's own look, following the desktop's light/dark preference.
    theme.apply(app)
    # Ctrl+C in the terminal should kill the app instead of being swallowed by
    # the Qt event loop.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    # Phase 22 Part 15: fail with a friendly dialog rather than a raw
    # traceback if the environment itself is broken (unwritable profile
    # dir, a database that cannot be opened/migrated, WebEngine missing).
    from app.startup_checks import fatal_failures, run_all

    checks = run_all()
    failures = fatal_failures(checks)
    if failures:
        _show_fatal_startup_error(
            app, "\n\n".join(f.message for f in failures))
        return 1

    # Phase 22 Part 13/14: did the previous session end cleanly? Bumps a
    # crash counter used below to auto-trigger Safe Mode after repeated
    # crashes, independent of whether --safe-mode was passed explicitly.
    session_start = mark_session_started()
    safe_mode = resolve_safe_mode(cli_flag=args.safe_mode, crash_count=session_start.crash_count)

    start_urls = args.urls or None
    if session_start.crashed_last_session and not safe_mode.enabled and not args.urls:
        # Part 13: offer to restore, never silently replay uncertain
        # state - "restore" here means the ordinary tab/workspace session
        # restore MainWindow already does (start_urls=None); declining it
        # opens one blank tab instead, the same "start fresh" MainWindow
        # gives Safe Mode itself.
        choice = QMessageBox.question(
            None, f"{APP_NAME} didn't close cleanly",
            f"{APP_NAME} may have crashed or been force-quit last time.\n\n"
            "Restore your previous browsing session?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if choice == QMessageBox.StandardButton.No:
            start_urls = ["about:blank"]

    database = Database(database_path())
    profile = BrowserProfile(app)

    window = MainWindow(profile, database, start_urls=start_urls, safe_mode=safe_mode)
    window.show()
    window.show_first_run_if_needed()

    exit_code = app.exec()
    database.close()
    if exit_code == 0:
        mark_session_clean()

    # os._exit(), not return/sys.exit(): confirmed via a minimal,
    # application-code-free reproducer that PySide6/QtWebEngine 6.11.2
    # segfaults ("Release of profile requested but WebEnginePage still
    # not deleted") inside CPython's own interpreter finalization
    # (Py_Finalize) after any real WebEngine page has been loaded and
    # later deleted - regardless of teardown order, event-loop pumping,
    # or elapsed wall-clock time (see tests/run.py's docstring for the
    # full investigation). The only known working mitigation is to never
    # let that finalization sequence run in a process that ever loaded a
    # real page - every ordinary browsing session. mark_session_clean()
    # above and database.close() have already run, so nothing meaningful
    # is skipped; logging's own handlers are flushed explicitly since
    # atexit-registered flushing is part of the finalization being
    # skipped here.
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)


if __name__ == "__main__":
    # main() only returns (rather than os._exit()ing itself - see its own
    # tail) via its early fatal-startup-check failure path, before any
    # QWebEngineProfile/page is ever created - ordinary sys.exit() is safe
    # there. Every path that got as far as app.exec() calls os._exit()
    # itself and never returns.
    raise SystemExit(main())
