"""Browsing history persisted in SQLite."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.storage.database import Database


@dataclass(frozen=True)
class HistoryEntry:
    id: int
    url: str
    title: str
    visited_at: str

    @property
    def visited_datetime(self) -> datetime:
        return datetime.fromisoformat(self.visited_at)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class HistoryStore:
    """Append-only visit log with lookup helpers for the address bar."""

    # Ignore URLs we never want in history: blank tabs and in-page errors.
    IGNORED_SCHEMES = ("about:", "data:", "chrome-error:", "javascript:")

    def __init__(self, db: Database) -> None:
        self._db = db

    def should_record(self, url: str) -> bool:
        if not url:
            return False
        return not url.lower().startswith(self.IGNORED_SCHEMES)

    def add_visit(self, url: str, title: str = "") -> None:
        if not self.should_record(url):
            return
        # Collapse reloads / rapid re-entries of the same page: if the newest
        # entry is the same URL, just refresh its title and timestamp.
        newest = self._db.query_one(
            "SELECT id, url FROM history ORDER BY id DESC LIMIT 1"
        )
        if newest is not None and newest["url"] == url:
            self._db.execute(
                "UPDATE history SET title = ?, visited_at = ? WHERE id = ?",
                (title, _now(), newest["id"]),
            )
            return
        self._db.execute(
            "INSERT INTO history (url, title, visited_at) VALUES (?, ?, ?)",
            (url, title, _now()),
        )

    def update_title(self, url: str, title: str) -> None:
        """Titles arrive after the URL, so patch the most recent matching row."""
        if not title or not self.should_record(url):
            return
        row = self._db.query_one(
            "SELECT id FROM history WHERE url = ? ORDER BY id DESC LIMIT 1", (url,)
        )
        if row is not None:
            self._db.execute(
                "UPDATE history SET title = ? WHERE id = ?", (title, row["id"])
            )

    def recent(self, limit: int = 200) -> list[HistoryEntry]:
        rows = self._db.query(
            "SELECT id, url, title, visited_at FROM history "
            "ORDER BY visited_at DESC, id DESC LIMIT ?",
            (limit,),
        )
        return [HistoryEntry(**dict(row)) for row in rows]

    def search(self, term: str, limit: int = 50) -> list[HistoryEntry]:
        pattern = f"%{term}%"
        rows = self._db.query(
            "SELECT id, url, title, visited_at FROM history "
            "WHERE url LIKE ? OR title LIKE ? "
            "ORDER BY visited_at DESC, id DESC LIMIT ?",
            (pattern, pattern, limit),
        )
        return [HistoryEntry(**dict(row)) for row in rows]

    def suggestions(self, prefix: str, limit: int = 20) -> list[str]:
        """Distinct URLs for address-bar autocompletion."""
        if not prefix:
            return []
        pattern = f"%{prefix}%"
        rows = self._db.query(
            "SELECT url, MAX(visited_at) AS last FROM history "
            "WHERE url LIKE ? GROUP BY url ORDER BY last DESC LIMIT ?",
            (pattern, limit),
        )
        return [row["url"] for row in rows]

    def delete(self, entry_id: int) -> None:
        self._db.execute("DELETE FROM history WHERE id = ?", (entry_id,))

    def clear(self) -> None:
        self._db.execute("DELETE FROM history")

    def count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM history")
        return int(row["n"]) if row else 0
