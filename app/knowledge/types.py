"""Shared types for the knowledge index - the chunk record every source
type is normalized into, and what a search returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class SourceType:
    """Every indexable origin. A chunk's ``source_type`` is always one of
    these - see app/knowledge/chunking.py for how each is split, and
    app/knowledge/index.py for how each is (re)indexed and removed."""

    HISTORY = "history"
    MISSION = "mission"
    MISSION_FINDING = "mission_finding"
    HIGHLIGHT = "highlight"
    PDF = "pdf"
    FILE = "file"
    ALL = (HISTORY, MISSION, MISSION_FINDING, HIGHLIGHT, PDF, FILE)

    LABELS = {
        HISTORY: "Browsing history", MISSION: "Mission", MISSION_FINDING: "Mission finding",
        HIGHLIGHT: "Highlight", PDF: "PDF", FILE: "File",
    }


@dataclass(frozen=True)
class Chunk:
    """One indexed piece of text, with full provenance - every chunk
    knows exactly where it came from, so a result can always be traced
    back to (and reopen) its source."""

    id: int
    source_type: str
    source_id: str
    chunk_index: int
    text: str
    content_hash: str
    timestamp: str
    embedding: tuple[float, ...]
    created_at: str
    parent_id: str | None = None
    title: str = ""
    location: str = ""
    #: Phase 17: which Workspace this was indexed under, or None for
    #: content with no particular workspace (everything indexed before
    #: Phase 17, and anything indexed while no workspace was current) -
    #: see app/workspaces/. A scoping hint for search, never a security
    #: boundary - all indexed content still lives in one local database.
    workspace_id: str | None = None


@dataclass(frozen=True)
class SearchResult:
    chunk: Chunk
    score: float
    semantic_score: float
    lexical_score: float
    excerpt: str
    #: True when the chunk's own timestamp is old enough that Py should
    #: treat it as possibly stale rather than current fact - see
    #: app/knowledge/retrieval.py's FRESHNESS_THRESHOLD_DAYS.
    stale: bool = False
