"""The indexing layer: turns already-approved content (history, Missions,
findings, highlights, PDFs, local files) into stored, embedded chunks -
never anything the user has not already chosen to keep somewhere in
PyBrowser. Every method here is a no-op while semantic history is
disabled, so turning the feature off truly means "stop indexing", not
just "hide the UI".

Nothing here blocks the GUI thread for longer than a single small page's
worth of hashing/embedding work (microseconds) - see KnowledgeIndex.rebuild
for the one bulk operation, which the UI runs on a background thread.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.knowledge import chunking
from app.knowledge.embeddings import content_hash, embed
from app.knowledge.types import SourceType

if TYPE_CHECKING:
    from app.storage.knowledge_store import KnowledgeStore
    from app.storage.settings import SettingsStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def url_source_id(url: str) -> str:
    """A stable id for a URL, so revisiting the same page updates one
    history chunk rather than growing the index per visit."""
    return hashlib.sha256((url or "").encode("utf-8")).hexdigest()[:16]


class KnowledgeIndex:
    def __init__(self, store: "KnowledgeStore", settings: "SettingsStore") -> None:
        self._store = store
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return self._settings.semantic_history_enabled

    @property
    def store(self) -> "KnowledgeStore":
        return self._store

    # -- shared plumbing ---------------------------------------------------
    def _write_chunks(
        self, source_type: str, source_id: str, texts: list[str], *,
        timestamp: str, parent_id: str | None = None, title: str = "", location: str = "",
        locations: list[str] | None = None, workspace_id: str | None = None,
    ) -> None:
        """Embed and store each text as a chunk at its list position,
        skipping any whose content hash matches what is already stored -
        the "do not re-embed unchanged content" requirement - then drop
        any leftover chunk positions from a previous, longer version of
        this source. ``locations`` overrides ``location`` per-chunk (a
        PDF's page-tagged locations), when given."""
        for index, text in enumerate(texts):
            digest = content_hash(text)
            if self._store.existing_hash(source_type, source_id, index) == digest:
                continue
            self._store.upsert_chunk(
                source_type=source_type, source_id=source_id, chunk_index=index,
                text=text, content_hash=digest, embedding=embed(text), timestamp=timestamp,
                parent_id=parent_id, title=title,
                location=locations[index] if locations is not None else location,
                workspace_id=workspace_id)
        self._store.delete_source_chunks_from(source_type, source_id, len(texts))

    # -- history -------------------------------------------------------
    def index_history_visit(self, url: str, title: str, visited_at: str | None = None,
                            *, workspace_id: str | None = None) -> None:
        """Metadata only (title + URL) - never the page's own body. This
        is the one source type the Phase 13 spec says is fine to index by
        default (once the feature itself is on) precisely because it is
        metadata PyBrowser already keeps in plain browsing history."""
        if not self.enabled or not url:
            return
        source_id = url_source_id(url)
        text = f"{title}\n{url}".strip()
        self._write_chunks(
            SourceType.HISTORY, source_id, [text], timestamp=visited_at or _now(),
            title=title, location=url, workspace_id=workspace_id)

    # -- missions --------------------------------------------------------
    def index_mission(self, mission_id: int, goal: str, result: str, *,
                      title: str = "", timestamp: str | None = None,
                      workspace_id: str | None = None) -> None:
        if not self.enabled:
            return
        source_id = str(mission_id)
        texts = [text for _label, text in chunking.chunk_mission(goal, result)]
        self._write_chunks(
            SourceType.MISSION, source_id, texts, timestamp=timestamp or _now(),
            title=title or goal[:120], workspace_id=workspace_id)

    def index_mission_finding(
        self, finding_id: int, mission_id: int, text: str, *,
        title: str = "", timestamp: str | None = None, workspace_id: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        chunks = chunking.chunk_highlight(text)  # a finding is already one short fact
        self._write_chunks(
            SourceType.MISSION_FINDING, str(finding_id), chunks, timestamp=timestamp or _now(),
            parent_id=str(mission_id), title=title, workspace_id=workspace_id)

    def remove_mission(self, mission_id: int) -> None:
        """A deleted Mission takes its goal/result chunk AND every finding
        chunk that named it as parent - nothing indexed should outlive
        the thing it was about."""
        self._store.delete_source(SourceType.MISSION, str(mission_id))
        self._store.delete_by_parent(str(mission_id))

    def remove_mission_finding(self, finding_id: int) -> None:
        self._store.delete_source(SourceType.MISSION_FINDING, str(finding_id))

    # -- highlights --------------------------------------------------------
    def index_highlight(
        self, highlight_id: int, text: str, *, title: str = "", location: str = "",
        timestamp: str | None = None, workspace_id: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        chunks = chunking.chunk_highlight(text)
        self._write_chunks(
            SourceType.HIGHLIGHT, str(highlight_id), chunks, timestamp=timestamp or _now(),
            title=title, location=location, workspace_id=workspace_id)

    def remove_highlight(self, highlight_id: int) -> None:
        self._store.delete_source(SourceType.HIGHLIGHT, str(highlight_id))

    # -- PDFs and local files ------------------------------------------------
    def index_pdf(self, path: str, pages: list[str], *, title: str = "",
                 timestamp: str | None = None) -> None:
        if not self.enabled:
            return
        page_chunks = chunking.chunk_pdf(pages)
        texts = [text for _page_number, text in page_chunks]
        # A page-tagged location per chunk, so a result can say "PDF, page
        # 3" even though chunk_index is not the page number once a page
        # itself splits into more than one paragraph-chunk.
        locations = [f"{path}#page={page_number}" for page_number, _text in page_chunks]
        self._write_chunks(
            SourceType.PDF, path, texts, timestamp=timestamp or _now(), title=title,
            locations=locations)

    def index_file(self, path: str, text: str, *, title: str = "",
                   timestamp: str | None = None) -> None:
        if not self.enabled:
            return
        texts = chunking.chunk_file(text)
        self._write_chunks(
            SourceType.FILE, path, texts, timestamp=timestamp or _now(),
            title=title or path, location=path)

    def remove_document(self, source_type: str, source_id: str) -> None:
        self._store.delete_source(source_type, source_id)

    # -- lifecycle actions (Settings -> Privacy / Memory / Knowledge) ------
    def clear(self) -> None:
        """Wipes every indexed chunk - works regardless of the enabled
        toggle, so a user who disabled the feature can still purge what
        was indexed before they did."""
        self._store.clear()

    def stats(self) -> dict:
        return {
            "chunk_count": self._store.count(),
            "source_count": self._store.distinct_source_count(),
            "last_indexed_at": self._store.last_indexed_at(),
            "storage_bytes": self._store.estimated_storage_bytes(),
        }
