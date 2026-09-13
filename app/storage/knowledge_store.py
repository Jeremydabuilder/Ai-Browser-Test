"""Persistence for Phase 13 (Semantic History / Local RAG) - see
app/knowledge/index.py for the layer that decides what/when to index, and
app/knowledge/retrieval.py for search. This module only stores/retrieves
chunk rows; it has no opinion about embeddings or chunking.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.storage.database import Database

if TYPE_CHECKING:
    from app.knowledge.types import Chunk

MAX_TITLE_CHARS = 300
MAX_LOCATION_CHARS = 2000
MAX_TEXT_CHARS = 4000


def _row_to_chunk(row) -> "Chunk":
    from app.knowledge.types import Chunk

    return Chunk(
        id=row["id"], source_type=row["source_type"], source_id=row["source_id"],
        parent_id=row["parent_id"], chunk_index=row["chunk_index"], title=row["title"],
        location=row["location"], text=row["text"], content_hash=row["content_hash"],
        timestamp=row["timestamp"], embedding=tuple(json.loads(row["embedding"])),
        created_at=row["created_at"],
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class KnowledgeStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- writes --------------------------------------------------------
    def upsert_chunk(
        self, *, source_type: str, source_id: str, chunk_index: int, text: str,
        content_hash: str, embedding: tuple[float, ...], timestamp: str,
        parent_id: str | None = None, title: str = "", location: str = "",
    ) -> None:
        """Replace the chunk at (source_type, source_id, chunk_index) if
        one exists, else insert. This is what makes re-indexing the same
        source safe to call repeatedly - stale chunk positions from a
        shrunk document are cleaned up separately by the caller via
        ``delete_source_chunks_from``."""
        existing = self._db.query_one(
            "SELECT id FROM knowledge_chunks WHERE source_type = ? AND source_id = ? "
            "AND chunk_index = ?", (source_type, source_id, chunk_index))
        embedding_json = json.dumps(list(embedding))
        if existing is not None:
            self._db.execute(
                "UPDATE knowledge_chunks SET text = ?, content_hash = ?, embedding = ?, "
                "timestamp = ?, parent_id = ?, title = ?, location = ? WHERE id = ?",
                (text[:MAX_TEXT_CHARS], content_hash, embedding_json, timestamp, parent_id,
                 title[:MAX_TITLE_CHARS], location[:MAX_LOCATION_CHARS], existing["id"]))
        else:
            self._db.execute(
                "INSERT INTO knowledge_chunks (source_type, source_id, parent_id, "
                "chunk_index, title, location, text, content_hash, timestamp, embedding, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source_type, source_id, parent_id, chunk_index, title[:MAX_TITLE_CHARS],
                 location[:MAX_LOCATION_CHARS], text[:MAX_TEXT_CHARS], content_hash,
                 timestamp, embedding_json, _now()))

    def existing_hash(self, source_type: str, source_id: str, chunk_index: int) -> str | None:
        row = self._db.query_one(
            "SELECT content_hash FROM knowledge_chunks WHERE source_type = ? "
            "AND source_id = ? AND chunk_index = ?", (source_type, source_id, chunk_index))
        return row["content_hash"] if row is not None else None

    def delete_source_chunks_from(self, source_type: str, source_id: str, from_index: int) -> None:
        """Drop any chunk at or past ``from_index`` for this source - what
        keeps re-indexing a shrunk document (fewer chunks than before)
        from leaving orphaned stale chunks behind."""
        self._db.execute(
            "DELETE FROM knowledge_chunks WHERE source_type = ? AND source_id = ? "
            "AND chunk_index >= ?", (source_type, source_id, from_index))

    def delete_source(self, source_type: str, source_id: str) -> None:
        self._db.execute(
            "DELETE FROM knowledge_chunks WHERE source_type = ? AND source_id = ?",
            (source_type, source_id))

    def delete_by_parent(self, parent_id: str) -> None:
        self._db.execute("DELETE FROM knowledge_chunks WHERE parent_id = ?", (parent_id,))

    def clear(self) -> None:
        self._db.execute("DELETE FROM knowledge_chunks", ())

    # -- reads -----------------------------------------------------------
    def all_chunks(self) -> list["Chunk"]:
        rows = self._db.query("SELECT * FROM knowledge_chunks ORDER BY id ASC")
        return [_row_to_chunk(row) for row in rows]

    def chunks_for_source(self, source_type: str, source_id: str) -> list["Chunk"]:
        rows = self._db.query(
            "SELECT * FROM knowledge_chunks WHERE source_type = ? AND source_id = ? "
            "ORDER BY chunk_index ASC", (source_type, source_id))
        return [_row_to_chunk(row) for row in rows]

    def count(self) -> int:
        row = self._db.query_one("SELECT COUNT(*) AS n FROM knowledge_chunks")
        return int(row["n"]) if row is not None else 0

    def distinct_source_count(self) -> int:
        row = self._db.query_one(
            "SELECT COUNT(DISTINCT source_type || ':' || source_id) AS n FROM knowledge_chunks")
        return int(row["n"]) if row is not None else 0

    def last_indexed_at(self) -> str | None:
        row = self._db.query_one("SELECT MAX(created_at) AS latest FROM knowledge_chunks")
        return row["latest"] if row is not None else None

    def estimated_storage_bytes(self) -> int:
        """A cheap, honest estimate (text + embedding JSON length) - not a
        real on-disk page count, but good enough for a Settings display."""
        row = self._db.query_one(
            "SELECT COALESCE(SUM(LENGTH(text) + LENGTH(embedding)), 0) AS total "
            "FROM knowledge_chunks")
        return int(row["total"]) if row is not None else 0
