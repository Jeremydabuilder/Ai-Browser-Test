"""Highlights: a piece of selected page text a person chose to keep.

Persisted with its source url/title copied in at save time - never looked
up again later - so a highlight stays usable as context even after the
original page changes or disappears entirely. See MainWindow's selection
context menu (Save Highlight) for how one gets created, and
app/agent/context_items.py for how a saved highlight becomes a selectable
@-mention alongside tabs, files and images.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.storage.database import Database
from app.storage.history import MAX_TITLE_LENGTH, MAX_URL_LENGTH

#: A highlight is a quote, not a document - this is generous for a real
#: selection (a few paragraphs) while still refusing to silently swallow
#: someone accidentally selecting an entire page. add() truncates past this
#: and reports it, so the caller can tell the user rather than saving a
#: surprise.
MAX_HIGHLIGHT_CHARS = 4000
MAX_NOTE_CHARS = 2000


@dataclass(frozen=True)
class Highlight:
    id: int
    url: str
    title: str
    text: str
    note: str
    created_at: str


class HighlightStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, url: str, title: str, text: str, note: str = "") -> tuple[Highlight | None, bool]:
        """Save a highlight. Returns (the saved row, whether it was truncated).

        Returns (None, False) for empty text - there is nothing to keep, and
        silently saving a blank row would just clutter the library for no
        reason. url is not required to look like a real address: a
        highlight already carries everything it needs (title, text), so an
        internal or malformed source still saves rather than being refused.
        """
        text = (text or "").strip()
        if not text:
            return None, False
        truncated = len(text) > MAX_HIGHLIGHT_CHARS
        if truncated:
            text = text[:MAX_HIGHLIGHT_CHARS]
        url = (url or "").strip()[:MAX_URL_LENGTH]
        title = (title or "").strip()[:MAX_TITLE_LENGTH]
        note = (note or "").strip()[:MAX_NOTE_CHARS]
        cursor = self._db.execute(
            "INSERT INTO highlights (url, title, text, note, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (url, title, text, note,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        if cursor is None:
            return None, False
        return self.get(cursor.lastrowid), truncated

    def get(self, highlight_id: int) -> Highlight | None:
        row = self._db.query_one(
            "SELECT id, url, title, text, note, created_at FROM highlights WHERE id = ?",
            (highlight_id,))
        return Highlight(**dict(row)) if row is not None else None

    def all(self) -> list[Highlight]:
        # id DESC as a tiebreaker: created_at has only second resolution, so
        # two highlights saved within the same second would otherwise sort
        # in an arbitrary order rather than most-recent-first.
        rows = self._db.query(
            "SELECT id, url, title, text, note, created_at FROM highlights "
            "ORDER BY created_at DESC, id DESC")
        return [Highlight(**dict(row)) for row in rows]

    def search(self, query: str) -> list[Highlight]:
        """Case-insensitive substring match against title/text/note/url -
        deliberately not a full-text index, the same "cheap and honest"
        scope as ContextComposer.search()."""
        needle = (query or "").strip().lower()
        if not needle:
            return self.all()
        return [h for h in self.all()
                if needle in h.title.lower() or needle in h.text.lower()
                or needle in h.note.lower() or needle in h.url.lower()]

    def set_note(self, highlight_id: int, note: str) -> None:
        note = (note or "").strip()[:MAX_NOTE_CHARS]
        self._db.execute("UPDATE highlights SET note = ? WHERE id = ?", (note, highlight_id))

    def remove(self, highlight_id: int) -> None:
        self._db.execute("DELETE FROM highlights WHERE id = ?", (highlight_id,))
