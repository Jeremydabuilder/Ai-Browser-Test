"""Persistence layer: a single SQLite database with one store class per feature."""

from app.storage.database import Database
from app.storage.history import HistoryEntry, HistoryStore
from app.storage.bookmarks import Bookmark, BookmarkStore
from app.storage.settings import SettingsStore

__all__ = [
    "Database",
    "HistoryEntry",
    "HistoryStore",
    "Bookmark",
    "BookmarkStore",
    "SettingsStore",
]
