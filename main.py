"""Entry point for the browser.

Run with:  python main.py [url ...]
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
from PySide6.QtWidgets import QApplication

from app import APP_NAME, ORG_NAME, __version__
from app.browser.newtab import register_scheme
from app.browser.profile import BrowserProfile
from app.config import database_path, icon_path, log_path
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
    return parser.parse_args(argv)


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

    database = Database(database_path())
    profile = BrowserProfile(app)

    window = MainWindow(profile, database, start_urls=args.urls or None)
    window.show()
    window.show_first_run_if_needed()

    exit_code = app.exec()
    database.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
