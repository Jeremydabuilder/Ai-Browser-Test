"""Source-aware chunking - never a blind fixed-length split when the
source's own structure says something better.
"""

from __future__ import annotations

#: A chunk larger than this is still searchable but makes for a poor
#: excerpt and a slower per-chunk embed - paragraphs are grouped up to
#: (not split below) this size.
MAX_CHUNK_CHARS = 1000


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in (text or "").replace("\r\n", "\n").split("\n\n")
           if p.strip()]


def chunk_paragraphs(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Group paragraphs up to ``max_chars`` per chunk - webpages, files,
    and PDF pages all use this. A single paragraph longer than
    ``max_chars`` becomes its own (oversized) chunk rather than being cut
    mid-sentence; it is rare enough not to warrant a smarter fallback."""
    paragraphs = _paragraphs(text)
    if not paragraphs:
        stripped = (text or "").strip()
        return [stripped] if stripped else []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        added_len = len(paragraph) + (2 if current else 0)
        if current and current_len + added_len > max_chars:
            chunks.append("\n\n".join(current))
            current = [paragraph]
            current_len = len(paragraph)
        else:
            current.append(paragraph)
            current_len += added_len
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def chunk_webpage(text: str) -> list[str]:
    return chunk_paragraphs(text)


def chunk_file(text: str) -> list[str]:
    return chunk_paragraphs(text)


def chunk_pdf(pages: list[str]) -> list[tuple[int, str]]:
    """Page-aware: each page is chunked on its own, tagged with its
    1-based page number, so a result can point at the exact page."""
    result: list[tuple[int, str]] = []
    for page_number, page_text in enumerate(pages, start=1):
        for chunk in chunk_paragraphs(page_text):
            result.append((page_number, chunk))
    return result


def chunk_highlight(text: str) -> list[str]:
    """A highlight is already a deliberately-selected, short passage - one
    chunk, per the Phase 13 spec, rather than sub-splitting it."""
    stripped = (text or "").strip()
    return [stripped] if stripped else []


def chunk_mission(goal: str, result: str = "") -> list[tuple[str, str]]:
    """Goal and result are indexed as separate, labeled chunks - findings
    are indexed individually elsewhere (one MISSION_FINDING chunk each,
    see app/knowledge/index.py), never folded into this pair."""
    chunks: list[tuple[str, str]] = []
    goal_text = (goal or "").strip()
    if goal_text:
        chunks.append(("goal", goal_text))
    result_text = (result or "").strip()
    if result_text:
        chunks.append(("result", result_text))
    return chunks
