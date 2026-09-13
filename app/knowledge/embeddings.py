"""Local, dependency-free embeddings.

A neural sentence-embedding model would need a heavyweight dependency
(and, downloaded weights) this codebase otherwise avoids entirely - see
the Phase 13 spec's "avoid adding a heavyweight server dependency" and
"prefer local embeddings if performance/size is acceptable". Instead,
each chunk is embedded with the hashing trick: every token is hashed into
one of a fixed number of dimensions with a deterministic sign, and the
resulting vector is L2-normalized. This is a real, standard technique
(scikit-learn's HashingVectorizer works the same way) - it captures
word-overlap similarity well, needs no fitting/vocabulary/corpus pass, so
a single new chunk can be embedded in isolation and compared against any
other chunk's vector by cosine similarity alone.

Nothing here makes a network call or reads any file outside the text it
is given.
"""

from __future__ import annotations

import hashlib
import math
import re

#: Small enough to be instant per chunk and per query, large enough that
#: hash collisions rarely matter for a desktop-scale personal corpus.
DIMENSIONS = 256

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Deliberately tiny and English-only - this is a token-frequency signal,
#: not a linguistic pipeline. Filtering the most common function words
#: keeps content words from being drowned out.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "was", "were", "it", "this", "that", "with", "as", "by", "at", "be", "i",
})


def tokenize(text: str) -> list[str]:
    tokens = _TOKEN_RE.findall((text or "").lower())
    return [t for t in tokens if t not in _STOPWORDS]


def _token_hash(token: str) -> int:
    # A stable hash across runs/processes - Python's own hash() is salted
    # per-process for security, which would make embeddings incomparable
    # between two runs of the app.
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def embed(text: str) -> tuple[float, ...]:
    """A unit-length, ``DIMENSIONS``-wide vector for ``text``. Empty or
    entirely-stopword text embeds to the zero vector (cosine similarity
    against it is always 0 - never a false match)."""
    vector = [0.0] * DIMENSIONS
    for token in tokenize(text):
        h = _token_hash(token)
        index = h % DIMENSIONS
        sign = 1.0 if (h // DIMENSIONS) % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        return tuple(vector)
    return tuple(v / norm for v in vector)


def cosine_similarity(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    if not a or not b:
        return 0.0
    return max(0.0, min(1.0, sum(x * y for x, y in zip(a, b))))


def content_hash(text: str) -> str:
    """A stable fingerprint for "has this chunk's text changed" - see
    app/knowledge/index.py, which skips re-embedding when this matches
    the already-stored hash for the same source/chunk position."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()
