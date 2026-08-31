"""Key/value settings stored in the same SQLite database.

Using our own table instead of QSettings keeps all persistent state in one
file, which makes the profile easy to back up, inspect or delete.
"""

from __future__ import annotations

from app.config import DEFAULT_HOME_URL, DEFAULT_SEARCH_URL
from app.storage.database import Database

KEY_HOME_URL = "home_url"
KEY_SEARCH_URL = "search_url"
KEY_RESTORE_TABS = "restore_tabs"

_DEFAULTS = {
    KEY_HOME_URL: DEFAULT_HOME_URL,
    KEY_SEARCH_URL: DEFAULT_SEARCH_URL,
    KEY_RESTORE_TABS: "0",
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
