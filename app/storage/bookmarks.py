"""Bookmarks persisted in SQLite (one row per unique URL)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.storage.database import Database
from app.storage.history import MAX_TITLE_LENGTH, MAX_URL_LENGTH


@dataclass(frozen=True)
class Bookmark:
    id: int
    url: str
    title: str
    created_at: str


class BookmarkStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    # Bookmarking a blank tab or an error page is never what the user meant.
    # Never recorded: blank tabs, error pages, inline data - and PyBrowser's
    # own internal pages, which are UI rather than places you visited.
    IGNORED_SCHEMES = ("about:", "data:", "chrome-error:", "chrome://", "javascript:",
                       "blob:", "pybrowser:")

    def is_bookmarkable(self, url: str) -> bool:
        url = (url or "").strip()
        if not url or len(url) > MAX_URL_LENGTH:
            return False
        return not url.lower().startswith(self.IGNORED_SCHEMES)

    def add(self, url: str, title: str = "") -> bool:
        """Insert a bookmark. Returns False if it was rejected or already present.

        The UNIQUE constraint on url is the real guard against duplicates; the
        contains() check just lets us report the outcome without an exception.
        """
        url = (url or "").strip()
        title = (title or "").strip()[:MAX_TITLE_LENGTH]
        if not self.is_bookmarkable(url):
            return False
        if self.contains(url):
            return False
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO bookmarks (url, title, created_at) VALUES (?, ?, ?)",
            (url, title, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        return cursor is not None

    def remove(self, url: str) -> None:
        self._db.execute("DELETE FROM bookmarks WHERE url = ?", (url,))

    def remove_by_id(self, bookmark_id: int) -> None:
        self._db.execute("DELETE FROM bookmarks WHERE id = ?", (bookmark_id,))

    def toggle(self, url: str, title: str = "") -> bool:
        """Bookmark or un-bookmark ``url``. Returns True if it is now bookmarked."""
        url = (url or "").strip()
        if not self.is_bookmarkable(url):
            return False
        if self.contains(url):
            self.remove(url)
            return False
        self.add(url, title)
        return True

    def contains(self, url: str) -> bool:
        url = (url or "").strip()
        return self._db.query_one(
            "SELECT 1 FROM bookmarks WHERE url = ?", (url,)
        ) is not None

    def all(self) -> list[Bookmark]:
        rows = self._db.query(
            "SELECT id, url, title, created_at FROM bookmarks ORDER BY created_at DESC"
        )
        return [Bookmark(**dict(row)) for row in rows]

    def rename(self, bookmark_id: int, title: str) -> None:
        self._db.execute(
            "UPDATE bookmarks SET title = ? WHERE id = ?", (title, bookmark_id)
        )
