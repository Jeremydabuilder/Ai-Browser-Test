"""Key/value settings stored in the same SQLite database.

Using our own table instead of QSettings keeps all persistent state in one
file, which makes the profile easy to back up, inspect or delete.
"""

from __future__ import annotations

from app.config import DEFAULT_HOME_URL, DEFAULT_SEARCH_URL, NEW_TAB_URL
from app.storage.database import Database

KEY_HOME_URL = "home_url"
KEY_SEARCH_URL = "search_url"
KEY_RESTORE_TABS = "restore_tabs"
KEY_NEW_TAB_MODE = "new_tab_mode"
KEY_NEW_TAB_CUSTOM = "new_tab_custom_url"

# What a new tab (and Home) opens. PyBrowser's own page is the default, but
# nobody is locked into it.
NEW_TAB_PYBROWSER = "pybrowser"
NEW_TAB_SEARCH = "search"
NEW_TAB_CUSTOM = "custom"
NEW_TAB_BLANK = "blank"

NEW_TAB_MODES = (
    (NEW_TAB_PYBROWSER, "PyBrowser New Tab"),
    (NEW_TAB_SEARCH, "Your search provider's home page"),
    (NEW_TAB_CUSTOM, "A custom address"),
    (NEW_TAB_BLANK, "A blank page"),
)

_DEFAULTS = {
    KEY_HOME_URL: DEFAULT_HOME_URL,
    KEY_SEARCH_URL: DEFAULT_SEARCH_URL,
    KEY_RESTORE_TABS: "0",
    KEY_NEW_TAB_MODE: NEW_TAB_PYBROWSER,
    KEY_NEW_TAB_CUSTOM: "",
}


class SettingsStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, key: str, default: str | None = None) -> str:
        row = self._db.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is not None:
            return row["value"]
        if default is not None:
            return default
        return _DEFAULTS.get(key, "")

    def set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_bool(self, key: str, default: bool = False) -> bool:
        return self.get(key, "1" if default else "0") in ("1", "true", "True")

    def set_bool(self, key: str, value: bool) -> None:
        self.set(key, "1" if value else "0")

    # Convenience accessors used across the UI.
    @property
    def home_url(self) -> str:
        return self.get(KEY_HOME_URL)

    @home_url.setter
    def home_url(self, value: str) -> None:
        self.set(KEY_HOME_URL, value)

    @property
    def search_url(self) -> str:
        return self.get(KEY_SEARCH_URL)

    @search_url.setter
    def search_url(self, value: str) -> None:
        self.set(KEY_SEARCH_URL, value)

    # -- what a new tab opens -------------------------------------------
    @property
    def new_tab_mode(self) -> str:
        mode = self.get(KEY_NEW_TAB_MODE)
        return mode if mode in dict(NEW_TAB_MODES) else NEW_TAB_PYBROWSER

    @new_tab_mode.setter
    def new_tab_mode(self, value: str) -> None:
        self.set(KEY_NEW_TAB_MODE, value if value in dict(NEW_TAB_MODES)
                 else NEW_TAB_PYBROWSER)

    @property
    def new_tab_custom_url(self) -> str:
        return self.get(KEY_NEW_TAB_CUSTOM)

    @new_tab_custom_url.setter
    def new_tab_custom_url(self, value: str) -> None:
        self.set(KEY_NEW_TAB_CUSTOM, (value or "").strip())

    def new_tab_url(self) -> str:
        """The address a new tab should open.

        Resolved here rather than stored as a URL so that changing the search
        provider moves the "search home page" option with it, and so a custom
        address that has been cleared falls back rather than opening nothing.
        """
        mode = self.new_tab_mode
        if mode == NEW_TAB_SEARCH:
            return self._search_home() or NEW_TAB_URL
        if mode == NEW_TAB_CUSTOM:
            return self.new_tab_custom_url or NEW_TAB_URL
        if mode == NEW_TAB_BLANK:
            return "about:blank"
        return NEW_TAB_URL

    def _search_home(self) -> str:
        """The origin of the search template - duckduckgo.com/?q={} -> its root."""
        from urllib.parse import urlsplit

        parts = urlsplit(self.search_url)
        if not parts.scheme or not parts.netloc:
            return ""
        return f"{parts.scheme}://{parts.netloc}/"
