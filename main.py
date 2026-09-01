"""Entry point for the browser.

Run with:  python main.py [url ...]
"""

from __future__ import annotations

import argparse
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
from app.config import database_path
from app.storage import Database
from app.ui.main_window import MainWindow


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="pybrowser", description=f"{APP_NAME} - a Python desktop web browser")
    parser.add_argument("urls", nargs="*", help="URLs to open on startup")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    # Chromium reads its scheme registry once, before the application exists,
    # so pybrowser:// has to be declared here and not a line later.
    register_scheme()

    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setOrganizationName(ORG_NAME)
    QCoreApplication.setApplicationVersion(__version__)

    app = QApplication(sys.argv[:1])
    app.setWindowIcon(QIcon.fromTheme("web-browser"))
    # Ctrl+C in the terminal should kill the app instead of being swallowed by
    # the Qt event loop.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    database = Database(database_path())
    profile = BrowserProfile(app)

    window = MainWindow(profile, database, start_urls=args.urls or None)
    window.show()

    exit_code = app.exec()
    database.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
