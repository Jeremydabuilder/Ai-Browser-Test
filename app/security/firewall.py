"""The central data-egress firewall: one place every outbound path (a
webpage's text, a PDF's text, a local file, a knowledge-retrieval result,
an MCP result, an MCP call's own arguments) runs through before it
reaches a model provider or an external tool, instead of each feature
inventing its own redaction pass.

Deterministic and fast by design (see detectors.py) - this sits on a hot
path (every tool result), so it must never make a network call or invoke
another model to decide what is sensitive. Results are cached by content
hash so re-rendering the same page text twice does not re-scan it.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256

from app.security.detectors import PLACEHOLDER_BY_CATEGORY, Category, Match, RiskLevel, find

__all__ = [
    "Category", "RiskLevel", "Finding", "scan", "redact", "highest_risk",
    "summarize", "clear_cache", "set_enabled", "is_enabled",
]

#: Mirrors SettingsStore.redact_secrets_enabled - kept here as simple
#: module state (rather than threading a SettingsStore through every
#: caller of wrap_untrusted) so the one place that already reads user
#: settings at startup (MainWindow) can set this once and every layer
#: that calls scan()/redact() picks it up immediately. Defaults to the
#: same protective-by-default value the setting itself defaults to.
_enabled = True


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = value


def is_enabled() -> bool:
    return _enabled


@dataclass(frozen=True)
class Finding:
    """One detection - the category and risk only. Never the matched text
    itself: a Finding is safe to log, display, or pass into a
    confirmation prompt precisely because it carries no secret."""

    category: str
    risk: str

    def to_dict(self) -> dict[str, str]:
        return {"category": self.category, "risk": self.risk}


#: Bounded LRU-ish cache keyed by a hash of the scanned text - a page
#: revisited or re-rendered within a task does not pay for re-scanning.
#: Capped so a long session cannot grow this without limit; the content
#: itself is never stored, only counts/positions, so the cache holds no
#: secrets even at capacity.
_CACHE_LIMIT = 500
_cache: "OrderedDict[str, tuple[str, list[Match]]]" = OrderedDict()


def _cache_key(text: str) -> str:
    return sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _scan_raw(text: str) -> tuple[str, list[Match]]:
    key = _cache_key(text)
    cached = _cache.get(key)
    if cached is not None:
        _cache.move_to_end(key)
        return cached
    matches = find(text)
    _cache[key] = (text, matches)
    _cache.move_to_end(key)
    while len(_cache) > _CACHE_LIMIT:
        _cache.popitem(last=False)
    return text, matches


def clear_cache() -> None:
    """For tests, and for a "clear my data" style action - never needed in
    normal operation since the cache holds no secrets to begin with."""
    _cache.clear()


def scan(text: str) -> list[Finding]:
    """What likely-sensitive things does ``text`` contain? Category/risk
    only, never the matched spans or the original text."""
    _, matches = _scan_raw(text)
    return [Finding(category=m.category, risk=m.risk) for m in matches]


def highest_risk(findings: list[Finding]) -> str:
    order = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}
    if not findings:
        return RiskLevel.LOW
    return max((f.risk for f in findings), key=lambda r: order[r])


def redact(text: str, *, only_high_risk: bool = False) -> tuple[str, list[Finding]]:
    """Replace every match with its category's placeholder
    ("[REDACTED_API_KEY]" etc.), keeping enough surrounding structure that
    a model can still reason about the shape of what was there. Returns
    the redacted text and the findings that drove it.

    ``only_high_risk`` redacts credentials/payment/authentication data
    (see detectors.RiskLevel) but leaves a MEDIUM finding (an email, a
    phone number) in place - used where the caller wants to warn about
    personal information rather than silently remove it (see the phase's
    own MEDIUM-risk "may show a disclosure" rule).
    """
    _, matches = _scan_raw(text)
    if only_high_risk:
        matches = [m for m in matches if m.risk == RiskLevel.HIGH]
    if not matches:
        return text, []
    # Matches are non-overlapping and sorted by start (see detectors.find);
    # rebuild the string back-to-front so earlier spans stay valid.
    out = text
    findings: list[Finding] = []
    for match in sorted(matches, key=lambda m: m.start, reverse=True):
        placeholder = PLACEHOLDER_BY_CATEGORY[match.category]
        out = out[:match.start] + placeholder + out[match.end:]
        findings.append(Finding(category=match.category, risk=match.risk))
    findings.reverse()
    return out, findings


def summarize(findings: list[Finding]) -> str:
    """A short, secret-free line for a security log entry or an egress
    preview - "2 email addresses, 1 API key detected", never the values."""
    if not findings:
        return ""
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.category] = counts.get(f.category, 0) + 1
    parts = [f"{count} {category.replace('_', ' ').lower()}{'s' if count != 1 else ''}"
             for category, count in counts.items()]
    return ", ".join(parts) + " detected"
