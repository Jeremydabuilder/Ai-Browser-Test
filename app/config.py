"""Application-wide paths and constants.

Everything that needs to know "where do we store things" asks this module, so
there is exactly one place to change if you want a portable install or a
different profile directory.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "PyBrowser"

#: The app's own icon, shipped as a real file rather than relying on
#: QIcon.fromTheme("web-browser") - that only resolves on a Linux desktop
#: with a matching icon theme installed. Without a bundled file, a packaged
#: Windows or macOS build shows a blank generic icon in the taskbar/dock and
#: window title bar, which is the whole reason this exists.
_ICON_DIR = Path(__file__).resolve().parent / "ui" / "assets" / "icons"


def icon_path() -> Path:
    """The best icon file for this platform: a multi-resolution .ico on
    Windows (native taskbar/title-bar quality), a .png everywhere else."""
    if os.name == "nt":
        return _ICON_DIR / "pybrowser.ico"
    return _ICON_DIR / "pybrowser.png"

# PyBrowser's own new-tab page - not a website. A search provider is where
# searches GO; it is not the browser's home. See app/browser/newtab.py.
NEW_TAB_URL = "pybrowser://newtab/"
DEFAULT_HOME_URL = NEW_TAB_URL
DEFAULT_SEARCH_URL = "https://duckduckgo.com/?q={query}"

# Qt WebEngine needs a stable on-disk profile directory for cookies, cache and
# local storage; without it every launch starts logged out of everything.
_ENV_DATA_DIR = "PYBROWSER_DATA_DIR"


def user_data_dir() -> Path:
    """Return the per-user directory where the profile and database live.

    Honours PYBROWSER_DATA_DIR so tests (and portable installs) can redirect
    storage without touching the real user profile.
    """
    override = os.environ.get(_ENV_DATA_DIR)
    if override:
        path = Path(override).expanduser()
    elif os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        path = Path(base) / APP_DIR_NAME
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    else:
        base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
        path = Path(base) / APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    return user_data_dir() / "browser.sqlite3"


def profile_storage_path() -> Path:
    path = user_data_dir() / "profile"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_path() -> Path:
    path = user_data_dir() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path() -> Path:
    """Where startup/runtime errors are written.

    A packaged build has no terminal to print a traceback to - without a
    file, an uncaught exception during startup just makes the app vanish
    with no way for anyone to say what happened. See main.py's excepthook.
    """
    path = user_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path / "pybrowser.log"


def downloads_path() -> Path:
    path = Path.home() / "Downloads"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        path = user_data_dir() / "downloads"
        path.mkdir(parents=True, exist_ok=True)
    return path
