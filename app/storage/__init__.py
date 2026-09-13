"""Persistence layer: a single SQLite database with one store class per feature."""

from app.storage.database import Database
from app.storage.history import HistoryEntry, HistoryStore
from app.storage.bookmarks import Bookmark, BookmarkStore
from app.storage.highlights import Highlight, HighlightStore
from app.storage.skills import SkillStore
from app.storage.settings import SettingsStore
from app.storage.scheduled_tasks import ScheduledTaskStore

__all__ = [
    "Database",
    "HistoryEntry",
    "HistoryStore",
    "Bookmark",
    "BookmarkStore",
    "Highlight",
    "HighlightStore",
    "SkillStore",
    "SettingsStore",
    "ScheduledTaskStore",
]
