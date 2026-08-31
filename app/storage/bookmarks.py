"""Bookmarks persisted in SQLite (one row per unique URL)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.storage.database import Database


@dataclass(frozen=True)
class Bookmark:
    id: int
    url: str
    title: str
    created_at: str


class BookmarkStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, url: str, title: str = "") -> bool:
        """Insert a bookmark. Returns False if the URL was already bookmarked."""
        if not url:
            return False
        if self.contains(url):
            return False
        self._db.execute(
            "INSERT INTO bookmarks (url, title, created_at) VALUES (?, ?, ?)",
            (url, title, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        return True

    def remove(self, url: str) -> None:
        self._db.execute("DELETE FROM bookmarks WHERE url = ?", (url,))

    def remove_by_id(self, bookmark_id: int) -> None:
        self._db.execute("DELETE FROM bookmarks WHERE id = ?", (bookmark_id,))

    def toggle(self, url: str, title: str = "") -> bool:
        """Bookmark or un-bookmark ``url``. Returns True if it is now bookmarked."""
        if self.contains(url):
            self.remove(url)
            return False
        self.add(url, title)
        return True

    def contains(self, url: str) -> bool:
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
