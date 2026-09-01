"""Browsing history persisted in SQLite.

Visits are written on the database's background thread (see ``Database``) so a
page load never waits on disk. Every read flushes that queue first, so the
asynchrony is invisible to callers.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from app.storage.database import Database

# Guard rails against a hostile or broken page poisoning the database.
MAX_URL_LENGTH = 4096
MAX_TITLE_LENGTH = 512


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

    # URLs we never want in history: blank tabs, error pages, inline data.
    # Never recorded: blank tabs, error pages, inline data - and PyBrowser's
    # own internal pages, which are UI rather than places you visited.
    IGNORED_SCHEMES = ("about:", "data:", "chrome-error:", "chrome://", "javascript:",
                       "blob:", "pybrowser:")

    def __init__(self, db: Database) -> None:
        self._db = db

    # -- validation ------------------------------------------------------
    def should_record(self, url: str) -> bool:
        if not url or not url.strip():
            return False
        if len(url) > MAX_URL_LENGTH:
            return False
        return not url.lower().startswith(self.IGNORED_SCHEMES)

    @staticmethod
    def _clean_title(title: str) -> str:
        return (title or "").strip()[:MAX_TITLE_LENGTH]

    # -- writes (queued off the GUI thread) ------------------------------
    def add_visit(self, url: str, title: str = "") -> None:
        if not self.should_record(url):
            return
        title = self._clean_title(title)
        timestamp = _now()

        def task(conn: sqlite3.Connection) -> None:
            # Collapse reloads and rapid re-entries: if the newest row is the
            # same URL, refresh it instead of appending a duplicate. Doing the
            # read and the write in one task on the writer thread keeps this
            # atomic - a plain read-then-write from the GUI thread would race.
            newest = conn.execute(
                "SELECT id, url FROM history ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if newest is not None and newest["url"] == url:
                conn.execute(
                    "UPDATE history SET title = ?, visited_at = ? WHERE id = ?",
                    (title, timestamp, newest["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO history (url, title, visited_at) VALUES (?, ?, ?)",
                    (url, title, timestamp),
                )

        self._db.submit_task(task)

    def update_title(self, url: str, title: str) -> None:
        """Titles arrive after the URL, so patch the most recent matching row."""
        title = self._clean_title(title)
        if not title or not self.should_record(url):
            return

        def task(conn: sqlite3.Connection) -> None:
            row = conn.execute(
                "SELECT id FROM history WHERE url = ? ORDER BY id DESC LIMIT 1", (url,)
            ).fetchone()
            if row is not None:
                conn.execute(
                    "UPDATE history SET title = ? WHERE id = ?", (title, row["id"])
                )

        self._db.submit_task(task)

    # -- reads (always flush the write queue first) ----------------------
    def recent(self, limit: int = 200) -> list[HistoryEntry]:
        self._db.flush()
        rows = self._db.query(
            "SELECT id, url, title, visited_at FROM history "
            "ORDER BY visited_at DESC, id DESC LIMIT ?",
            (limit,),
        )
        return [HistoryEntry(**dict(row)) for row in rows]

    def search(self, term: str, limit: int = 50) -> list[HistoryEntry]:
        self._db.flush()
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
        self._db.flush()
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
        self._db.flush()
        self._db.execute("DELETE FROM history")

    def count(self) -> int:
        self._db.flush()
        row = self._db.query_one("SELECT COUNT(*) AS n FROM history")
        return int(row["n"]) if row else 0
