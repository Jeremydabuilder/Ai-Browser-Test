"""Search over the knowledge index: semantic (cosine similarity on the
local hashed embeddings) blended with lexical (keyword/title overlap),
never one alone - see the Phase 13 spec's "combine lexical + semantic
scoring if practical".

A full scan over every chunk is fine at desktop scale (thousands of
chunks, 256-dim vectors) and needs no vector-index dependency; see
app/knowledge/embeddings.py for why a corpus-wide fit is not needed here.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.knowledge.embeddings import cosine_similarity, embed, tokenize
from app.knowledge.types import Chunk, SearchResult

#: A result semantically anchored on content older than this is flagged
#: stale - Py should not present it as necessarily still true. See the
#: Phase 13 spec's "do not present a 6-month-old price/page as current
#: fact".
FRESHNESS_THRESHOLD_DAYS = 30

DEFAULT_LIMIT = 8
#: Relevance weighting - semantic first, lexical as a keyword-match boost
#: (an exact title/keyword hit should still surface even when the hashed
#: embedding's overlap happens to be modest).
SEMANTIC_WEIGHT = 0.65
LEXICAL_WEIGHT = 0.35


def _lexical_score(query_tokens: set[str], chunk: Chunk) -> float:
    if not query_tokens:
        return 0.0
    text_tokens = set(tokenize(chunk.text))
    title_tokens = set(tokenize(chunk.title))
    text_overlap = len(query_tokens & text_tokens) / len(query_tokens)
    title_overlap = len(query_tokens & title_tokens) / len(query_tokens)
    # A title match counts extra - a query word appearing in the title is a
    # much stronger signal than the same word buried in a long body.
    return min(1.0, text_overlap + 0.5 * title_overlap)


def _is_stale(timestamp: str) -> bool:
    try:
        when = datetime.fromisoformat(timestamp)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - when).total_seconds() / 86400
    return age_days > FRESHNESS_THRESHOLD_DAYS


def _excerpt(text: str, query_tokens: set[str], max_chars: int = 220) -> str:
    """A short window around the first query-token match, so the result
    list shows *why* something matched rather than just its opening
    sentence every time."""
    if not text:
        return ""
    lowered = text.lower()
    best_index = -1
    for token in query_tokens:
        found = lowered.find(token)
        if found != -1 and (best_index == -1 or found < best_index):
            best_index = found
    if best_index == -1:
        snippet = text[:max_chars]
    else:
        start = max(0, best_index - max_chars // 3)
        snippet = text[start:start + max_chars]
    snippet = snippet.strip()
    if len(text) > len(snippet):
        snippet = f"…{snippet}…" if snippet != text[:len(snippet)] else f"{snippet}…"
    return snippet


def search(
    chunks: list[Chunk], query: str, *, limit: int = DEFAULT_LIMIT,
    source_types: tuple[str, ...] | None = None,
    workspace_ids: tuple[str | None, ...] | None = None,
) -> list[SearchResult]:
    """Rank ``chunks`` (typically ``KnowledgeStore.all_chunks()``) against
    ``query``. An empty query returns no results - this is a search, not
    a browse-everything view.

    ``workspace_ids``, when given, scopes results to chunks whose
    ``workspace_id`` is in that set - pass ``(current_id, None)`` to mean
    "this workspace, plus anything not tied to one" (see
    app/workspaces/), never silently mixing in a genuinely different
    workspace's content just because the query happened to match it.
    """
    query = (query or "").strip()
    if not query:
        return []
    query_tokens = set(tokenize(query))
    query_embedding = embed(query)

    scored: list[SearchResult] = []
    for chunk in chunks:
        if source_types is not None and chunk.source_type not in source_types:
            continue
        if workspace_ids is not None and chunk.workspace_id not in workspace_ids:
            continue
        semantic = cosine_similarity(query_embedding, chunk.embedding)
        lexical = _lexical_score(query_tokens, chunk)
        combined = SEMANTIC_WEIGHT * semantic + LEXICAL_WEIGHT * lexical
        if combined <= 0:
            continue
        scored.append(SearchResult(
            chunk=chunk, score=combined, semantic_score=semantic, lexical_score=lexical,
            excerpt=_excerpt(chunk.text, query_tokens), stale=_is_stale(chunk.timestamp)))

    scored.sort(key=lambda r: r.score, reverse=True)
    return scored[:max(0, limit)]
