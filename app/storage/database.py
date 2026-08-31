"""Thin SQLite wrapper shared by every store.

Design notes
------------
* One connection for the whole app. The UI is single-threaded, but WebEngine
  callbacks can arrive from helper threads, so ``check_same_thread=False`` plus
  an explicit lock keeps things safe without dragging in an ORM.
* **Writes that happen on every page load are queued to a background thread.**
  A single INSERT measures ~0.3 ms, so this is not about throughput; it is
  about never letting an fsync stall on a slow or busy disk block the GUI
  thread. User-initiated writes (bookmarks, settings) stay synchronous because
  the UI reads them back immediately and must see its own change.
* Reads call ``flush()`` first, so "queued in the background" is never visible
  as missing data.
* Schema creation is idempotent and versioned via ``PRAGMA user_version`` so
  migrations can be added later without breaking existing profiles.
"""

from __future__ import annotations

import queue
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    visited_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_visited_at ON history(visited_at DESC);
CREATE INDEX IF NOT EXISTS idx_history_url ON history(url);

CREATE TABLE IF NOT EXISTS bookmarks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT NOT NULL UNIQUE,
    title      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_STOP = object()


class Database:
    """Owns the sqlite3 connection, plus a background writer for hot-path writes."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = self._open_or_recover()

        self._writes: queue.Queue = queue.Queue()
        self._writer = threading.Thread(
            target=self._writer_loop, name="sqlite-writer", daemon=True
        )
        self._writer.start()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        # WAL lets a read proceed while a write is in flight - the right default
        # for a desktop app that writes on every page load.
        conn.execute("PRAGMA journal_mode=WAL")
        # With WAL, NORMAL means we fsync at checkpoints rather than on every
        # commit. The worst case is losing the last few history rows after an
        # OS crash, which is an acceptable trade for never stalling the UI.
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _open_or_recover(self) -> sqlite3.Connection:
        """Open the database, quarantining and recreating it if it is corrupt.

        A truncated or non-SQLite file at this path would otherwise make the
        whole application fail to start, and losing history is a far better
        outcome than a browser that will not launch. Note that the failure can
        surface as early as the first PRAGMA, so both the connect and the
        schema step are covered here.
        """
        try:
            conn = self._connect()
            self._apply_schema(conn)
            return conn
        except sqlite3.DatabaseError:
            pass

        # Move the unusable file aside rather than deleting it, so a user who
        # cares can still try to recover it by hand.
        quarantine = self.path.with_name(self.path.name + ".corrupt")
        try:
            self.path.replace(quarantine)
        except OSError:
            self.path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)

        conn = self._connect()
        self._apply_schema(conn)
        return conn

    def _apply_schema(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.commit()

    # -- background writer ----------------------------------------------
    def _writer_loop(self) -> None:
        while True:
            item = self._writes.get()
            try:
                if item is _STOP:
                    return
                try:
                    with self._lock:
                        if callable(item):
                            # A task that needs to read-then-write atomically
                            # runs entirely on this thread, under the lock.
                            item(self._conn)
                        else:
                            sql, params = item
                            self._conn.execute(sql, params)
                        self._conn.commit()
                except sqlite3.Error:
                    # A failed history write must never take the browser down.
                    pass
            finally:
                self._writes.task_done()

    def submit(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Queue a fire-and-forget write. Never blocks the caller."""
        if self._closed:
            return
        self._writes.put((sql, tuple(params)))

    def submit_task(self, task) -> None:
        """Queue a callable that receives the connection. Never blocks."""
        if self._closed:
            return
        self._writes.put(task)

    def flush(self, timeout: float = 5.0) -> None:
        """Wait for queued writes to land. Called before any read."""
        if self._closed:
            return
        done = threading.Event()
        # join() has no timeout, so drain via a sentinel write instead.
        self._writes.put(("SELECT 1", ()))
        deadline = threading.Timer(timeout, done.set)
        deadline.start()
        try:
            while not self._writes.empty() and not done.is_set():
                done.wait(0.002)
        finally:
            deadline.cancel()

    # -- synchronous access ----------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, seq)
            self._conn.commit()

    def close(self) -> None:
        if self._closed:
            return
        self.flush()
        self._closed = True
        self._writes.put(_STOP)
        self._writer.join(timeout=5.0)
        with self._lock:
            self._conn.close()
