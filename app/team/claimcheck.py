"""A deterministic, conservative check of "does the cited text actually say this".

The Team already removes citations to sources that do not exist. That only
shows a source *exists*. This module adds the next, weaker-than-truth step:
for each sentence that cites a source, do that source's words and - above all -
its figures appear in the text the Team really holds for it?

What this is NOT: a fact check. It cannot tell whether the source is right,
whether the sentence follows from it, or whether a paraphrase that shares no
words is faithful. A "found" result means the wording and numbers are present,
nothing more; "not found" means the Team could not locate them and a person
should look. No review by a model changes that, and nothing here claims it does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_CITATION = re.compile(r"\[(S\d+)\]")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\[\"'(])|\n+")
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?%?")
_WORD = re.compile(r"[a-z][a-z'-]{3,}")
_STOP = frozenset("""
that this with from have been were their there which would about into than then them they these those
also more most such some what when where while will your over only other being both each does done
said says according report reports found shows show using used based while however because since
""".split())

#: Overlap needed (share of the claim's content words present in the source).
SUPPORT_THRESHOLD = 0.6


class Verdict:
    FOUND = "found"            # wording and every figure present in the cited text
    PARTIAL = "partial"        # some wording present, or a figure missing
    NOT_FOUND = "not_found"    # little or none of it present
    SNIPPET = "snippet_only"   # matched, but only against a search snippet (page never opened)
    NO_TEXT = "no_text"        # the cited source has no text to compare against


@dataclass
class ClaimResult:
    claim: str
    sources: list[str]
    verdict: str
    missing_numbers: list[str] = field(default_factory=list)
    overlap: float = 0.0
    note: str = ""

    def to_dict(self) -> dict:
        return {"claim": self.claim, "sources": self.sources, "verdict": self.verdict,
                "missing_numbers": self.missing_numbers, "overlap": round(self.overlap, 2),
                "note": self.note}


def _norm_number(token: str) -> str:
    return token.replace(",", "").rstrip("%").lstrip("0") or "0"


def _numbers(text: str) -> list[str]:
    seen: list[str] = []
    for m in _NUMBER.finditer(text):
        n = _norm_number(m.group(0))
        # A lone digit ("3 reasons", "step 2") is as often a count of items in
        # the sentence itself as a figure from the source; checking it would
        # flag nearly every sentence. Everything from two digits up is checked.
        if n.isdigit() and len(n) < 2 and "%" not in m.group(0):
            continue
        if n not in seen:
            seen.append(n)
    return seen


def _stem(word: str) -> str:
    return word[:6]


def _content_words(text: str) -> list[str]:
    words: list[str] = []
    for w in _WORD.findall(text.lower()):
        if w in _STOP:
            continue
        s = _stem(w)
        if s not in words:
            words.append(s)
    return words


def _source_index(text: str) -> tuple[set[str], set[str]]:
    lowered = text.lower()
    stems = {_stem(w) for w in _WORD.findall(lowered)}
    nums = {_norm_number(m.group(0)) for m in _NUMBER.finditer(text)}
    return stems, nums


def claims_with_citations(text: str) -> list[tuple[str, list[str]]]:
    """(sentence without its markers, [source ids]) for each cited sentence."""
    out: list[tuple[str, list[str]]] = []
    for sentence in _SENTENCE_SPLIT.split(text or ""):
        ids = list(dict.fromkeys(_CITATION.findall(sentence)))
        if not ids:
            continue
        clean = _CITATION.sub("", sentence).strip(" -*•#>\t")
        if len(clean) >= 20:
            out.append((clean, ids))
    return out


def check_claims(text: str, sources: dict) -> list[ClaimResult]:
    """``sources`` maps id -> an object with ``text``, ``kind``/``depth``
    attributes (the Team's Source). Unknown ids are ignored: the existing
    citation check already removed them."""
    indexes: dict[str, tuple[set[str], set[str], object]] = {}
    for sid, src in sources.items():
        body = getattr(src, "text", "") or ""
        stems, nums = _source_index(body)
        indexes[sid] = (stems, nums, src)

    results: list[ClaimResult] = []
    for claim, ids in claims_with_citations(text):
        ids = [i for i in ids if i in indexes]
        if not ids:
            continue
        words, numbers = _content_words(claim), _numbers(claim)
        best: ClaimResult | None = None
        for sid in ids:
            stems, nums, src = indexes[sid]
            if not stems:
                candidate = ClaimResult(claim, ids, Verdict.NO_TEXT,
                                        note=f"[{sid}] has no readable text to compare against.")
            else:
                overlap = (sum(1 for w in words if w in stems) / len(words)) if words else 1.0
                missing = [n for n in numbers if n not in nums]
                if overlap >= SUPPORT_THRESHOLD and not missing:
                    verdict = Verdict.FOUND
                elif overlap >= 0.3 or (not missing and numbers):
                    verdict = Verdict.PARTIAL
                else:
                    verdict = Verdict.NOT_FOUND
                snippet_only = getattr(src, "kind", "") == "web" and getattr(src, "depth", "") != "page"
                if verdict == Verdict.FOUND and snippet_only:
                    verdict = Verdict.SNIPPET
                candidate = ClaimResult(claim, ids, verdict, missing, overlap)
            if best is None or _rank(candidate.verdict) < _rank(best.verdict):
                best = candidate
        if best is not None:
            results.append(best)
    return results


def _rank(verdict: str) -> int:
    order = (Verdict.FOUND, Verdict.SNIPPET, Verdict.PARTIAL, Verdict.NOT_FOUND, Verdict.NO_TEXT)
    return order.index(verdict)


def possible_conflicts(results: list[ClaimResult]) -> list[tuple[ClaimResult, ClaimResult]]:
    """Pairs of claims on the same topic (shared wording) that state different
    figures. Heuristic and labelled as possible - the figures may describe
    different things (years, regions) the wording does not separate."""
    pairs: list[tuple[ClaimResult, ClaimResult]] = []
    prepared = [(r, set(_content_words(r.claim)), set(_numbers(r.claim))) for r in results]
    for i in range(len(prepared)):
        for j in range(i + 1, len(prepared)):
            a, wa, na = prepared[i]
            b, wb, nb = prepared[j]
            if not na or not nb or na & nb:
                continue
            shared = wa & wb
            if len(shared) >= 3 and len(shared) / max(1, min(len(wa), len(wb))) >= 0.5:
                pairs.append((a, b))
    return pairs


def summarize(results: list[ClaimResult]) -> dict:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    return {"checked": len(results), **counts}


def appendix(results: list[ClaimResult], *, uncited_figures: list[str] | None = None) -> str:
    """The reader-facing block. Lists what needs a human look; says plainly
    what the check can and cannot show."""
    if not results and not uncited_figures:
        return ""
    summary = summarize(results)
    found = summary.get(Verdict.FOUND, 0)
    lines = [
        "\n## Citation check (automatic)",
        "_Compares each cited sentence's wording and figures with the text the Team holds for that "
        "source. It is not a fact check: it cannot tell whether a source is right or whether a "
        "sentence follows from it, and a paraphrase can be faithful yet not match. Treat anything "
        "listed below as needing your own look._",
        f"- {len(results)} cited sentence(s) checked; {found} had their wording and figures found in the cited text.",
    ]
    flagged = [r for r in results if r.verdict != Verdict.FOUND]
    labels = {Verdict.PARTIAL: "only partly found", Verdict.NOT_FOUND: "not found in the cited text",
              Verdict.SNIPPET: "matches a search snippet only - the page was never opened",
              Verdict.NO_TEXT: "the cited source had no text to compare"}
    for r in flagged[:15]:
        cites = ", ".join(f"[{s}]" for s in r.sources)
        extra = f" (figure(s) not in the source: {', '.join(r.missing_numbers)})" if r.missing_numbers else ""
        lines.append(f"- {labels.get(r.verdict, r.verdict)} {cites}{extra}: “{r.claim[:140]}”")
    if len(flagged) > 15:
        lines.append(f"- …and {len(flagged) - 15} more.")
    for a, b in possible_conflicts(results)[:5]:
        lines.append(f"- Possible conflict (different figures on similar wording): "
                     f"“{a.claim[:90]}” vs “{b.claim[:90]}”")
    for sentence in (uncited_figures or [])[:5]:
        lines.append(f"- A figure with no citation: “{sentence[:140]}”")
    return "\n".join(lines)


def uncited_figure_sentences(text: str) -> list[str]:
    """Sentences stating a figure but citing nothing."""
    out = []
    for sentence in _SENTENCE_SPLIT.split(text or ""):
        if _CITATION.search(sentence):
            continue
        clean = sentence.strip(" -*•#>\t")
        if len(clean) >= 25 and _numbers(clean) and not clean.startswith(("#", "|")):
            out.append(clean)
    return out
