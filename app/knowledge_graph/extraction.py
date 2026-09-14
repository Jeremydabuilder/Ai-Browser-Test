"""Deterministic, local-only extraction: topics and contradiction
comparisons. No network call, no model call, ever required for baseline
graph building - Part TOPICS: "Do not send all historical content to a
cloud model just to generate topic labels", and Part LOCAL MODELS: "Keep
graph building functional without requiring a cloud model."

A local or cloud model MAY be used later for richer extraction (see
``builder.GraphBuilder``'s optional ``topic_extractor``/``claim_extractor``
hooks), but nothing in this module or the default builder path ever
requires one.
"""

from __future__ import annotations

import re
from collections import Counter

#: A short, unremarkable stopword list - enough to keep "the", "and", "of"
#: out of topic candidates without pretending to be a real NLP pipeline.
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "of", "in", "on", "at", "to", "for",
    "with", "from", "by", "is", "are", "was", "were", "be", "been", "being",
    "this", "that", "these", "those", "it", "its", "as", "than", "then",
    "so", "if", "not", "no", "do", "does", "did", "has", "have", "had",
    "will", "would", "should", "could", "can", "may", "might", "must",
    "about", "into", "over", "after", "before", "up", "down", "out", "off",
    "you", "your", "i", "we", "they", "he", "she", "his", "her", "their",
    "what", "which", "who", "when", "where", "why", "how", "there", "here",
})

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'+#.-]{1,}")

#: How many topic candidates a single piece of text ever yields - keeps
#: builder.py's per-event work small and bounded, matching Part
#: PERFORMANCE's "graph construction must be incremental" ask.
MAX_TOPICS_PER_TEXT = 5
MIN_TOPIC_LENGTH = 3


def extract_topics(text: str, *, max_topics: int = MAX_TOPICS_PER_TEXT) -> list[str]:
    """Lightweight keyphrase extraction: 1-2 word candidates, stopwords and
    very short tokens dropped, most frequent (then longest, as a mild bias
    toward more specific phrases) wins. Deliberately simple - a real
    research feature should surface topics a person recognizes as their
    own words, not a black-box label.
    """
    if not text:
        return []
    words = [w.lower() for w in _WORD_RE.findall(text)]
    tokens = [w for w in words if w not in _STOPWORDS and len(w) >= MIN_TOPIC_LENGTH]
    if not tokens:
        return []
    counts: Counter[str] = Counter(tokens)
    # Bigrams of consecutive *kept* tokens as they appeared - a cheap stand-in
    # for phrase detection ("tool calling" beats "tool" and "calling" apart).
    bigrams: Counter[str] = Counter()
    kept_positions = [i for i, w in enumerate(words) if w in tokens or w not in _STOPWORDS]
    for i in range(len(words) - 1):
        a, b = words[i], words[i + 1]
        if a not in _STOPWORDS and b not in _STOPWORDS and len(a) >= MIN_TOPIC_LENGTH \
                and len(b) >= MIN_TOPIC_LENGTH:
            bigrams[f"{a} {b}"] += 1

    candidates: Counter[str] = Counter()
    for phrase, count in bigrams.items():
        if count >= 2:
            candidates[phrase] = count * 2  # a repeated phrase beats a repeated single word
    for word, count in counts.items():
        candidates[word] += count

    ranked = sorted(candidates.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    return [phrase for phrase, _count in ranked[:max_topics]]


_NUMBER_RE = re.compile(r"[\d.,]+")


def strip_numbers(text: str) -> str:
    """The "same claim, different number" comparison key - Part
    CONTRADICTIONS' price example ("costs $99" vs "now costs $129") needs
    the surrounding sentence to match with numbers removed before their
    difference means anything."""
    return re.sub(r"\s+", " ", _NUMBER_RE.sub("#", (text or "").lower())).strip()


def extract_numbers(text: str) -> list[str]:
    return _NUMBER_RE.findall(text or "")


def lexical_similarity(a: str, b: str) -> float:
    """A plain token-overlap (Jaccard) score, 0..1 - the same "cheap and
    honest" standard the rest of this codebase's lexical matching already
    uses (app.knowledge.retrieval's own lexical half)."""
    tokens_a = set(_WORD_RE.findall((a or "").lower()))
    tokens_b = set(_WORD_RE.findall((b or "").lower()))
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union) if union else 0.0
