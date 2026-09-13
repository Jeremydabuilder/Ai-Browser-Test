"""Deterministic change detection for Page Watches.

Pure functions only: text in, a verdict out - no Qt, no network, no
browser, and (deliberately) no LLM. The phase spec is explicit that this
must be layered and deterministic-first:

    1. normalize (whitespace, nothing else) and hash - cheap, and enough to
       rule out "nothing changed at all" without looking any closer.
    2. only if the hash changed, extract whatever the condition actually
       cares about (a number, a substring, an availability guess) and
       decide whether *that* changed in a way the condition names.

An LLM never sits in this path. If a future "judge meaningfulness with AI"
option is added on top, it must only run after this module already found a
real difference, and only on the small before/after fragment this module
identifies - never on a full page, and never as a substitute for this
layer. See the module docstring in app/watches/__init__.py.

None of this ever executes anything the page says: everything here reads
plain page text as *data* and produces plain strings/numbers/booleans back.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.watches.model import WatchCondition

_WHITESPACE_RE = re.compile(r"\s+")

#: A short list of obviously time-shaped substrings worth stripping before
#: hashing, so a page whose only difference is "Updated 2 minutes ago" does
#: not look changed. This is a best-effort denylist, not real semantic
#: understanding of the page - a site with its own unusual "last updated"
#: phrasing will not be caught, and that is a stated limitation, not a bug.
_NOISE_PATTERNS = (
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\b", re.IGNORECASE),
    re.compile(r"\b\d+\s+(?:second|minute|hour|day)s?\s+ago\b", re.IGNORECASE),
    re.compile(r"\bjust now\b", re.IGNORECASE),
    re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"),
)

_NUMBER_RE = re.compile(r"[-+]?\$?\s?\d[\d,]*(?:\.\d+)?%?")

_UNAVAILABLE_PHRASES = (
    "out of stock", "sold out", "unavailable", "no longer available",
    "notify me when available", "currently unavailable", "temporarily out of stock",
)
_AVAILABLE_PHRASES = (
    "add to cart", "add to bag", "in stock", "buy now", "available now",
)


def normalize_text(text: str) -> str:
    """Whitespace-collapsed, denoised text - never returned to the user,
    only ever hashed or scanned. Case is preserved: TEXT_CONTAINS/
    TEXT_NOT_CONTAINS compare case-insensitively themselves, and preserving
    case elsewhere costs nothing and loses no information."""
    text = text or ""
    for pattern in _NOISE_PATTERNS:
        text = pattern.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def hash_text(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()


def extract_first_number(normalized: str) -> float | None:
    """The first number-shaped token, price symbols and thousands
    separators stripped - "$1,299.00" -> 1299.0. Returns None when nothing
    number-shaped is found; a watch on a numeric condition then has nothing
    to compare and must not guess (see evaluate_check).
    """
    match = _NUMBER_RE.search(normalized)
    if not match:
        return None
    cleaned = match.group(0).replace("$", "").replace(",", "").replace("%", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def looks_available(normalized: str) -> bool | None:
    """A best-effort read of "in stock" vs "sold out" from page text.

    Deliberately conservative: an explicit "out of stock"-shaped phrase
    wins over an explicit "add to cart"-shaped one if both are somehow
    present (a page listing related/other items), and if neither phrase
    appears at all this returns None - "cannot tell" is the honest answer
    for a page that does not say either way, not a guess in either
    direction. This is real text matching, not semantic understanding of
    an arbitrary storefront's own wording.
    """
    lowered = normalized.lower()
    if any(phrase in lowered for phrase in _UNAVAILABLE_PHRASES):
        return False
    if any(phrase in lowered for phrase in _AVAILABLE_PHRASES):
        return True
    return None


@dataclass(frozen=True)
class CheckResult:
    #: The raw content hash changed at all - step 1 of the layering.
    changed: bool
    #: Worth alerting the user about, per the watch's own condition.
    meaningful: bool
    new_hash: str
    #: The condition-specific derived value to store as "last observed" -
    #: a number as text, "true"/"false", or None for ANY_CHANGE, which
    #: tracks nothing beyond the hash itself.
    new_value: str | None
    #: A short, human-readable line describing what changed - only ever
    #: set when meaningful is True.
    summary: str


def evaluate_check(
    condition: str,
    condition_value: str,
    raw_text: str,
    *,
    previous_hash: str | None,
    previous_value: str | None,
) -> CheckResult:
    """Run one check's worth of comparison.

    ``previous_hash is None`` means this is the very first check for this
    watch - there is nothing to compare against yet, so this always
    establishes a baseline (changed=False, meaningful=False) rather than
    alerting on the watch's own creation.
    """
    normalized = normalize_text(raw_text)
    new_hash = hash_text(normalized)

    if previous_hash is None:
        return CheckResult(changed=False, meaningful=False, new_hash=new_hash,
                          new_value=_derived_value(condition, normalized), summary="")

    changed = new_hash != previous_hash
    if not changed:
        return CheckResult(changed=False, meaningful=False, new_hash=new_hash,
                          new_value=previous_value, summary="")

    return _evaluate_changed(condition, condition_value, normalized, new_hash, previous_value)


def _derived_value(condition: str, normalized: str) -> str | None:
    if condition in WatchCondition.NUMERIC:
        number = extract_first_number(normalized)
        return None if number is None else str(number)
    if condition in WatchCondition.TEXTUAL:
        return None
    if condition == WatchCondition.BECOMES_AVAILABLE:
        available = looks_available(normalized)
        return "unknown" if available is None else str(available).lower()
    return None


def _evaluate_changed(
    condition: str, condition_value: str, normalized: str, new_hash: str,
    previous_value: str | None,
) -> CheckResult:
    if condition == WatchCondition.ANY_CHANGE:
        preview = normalized[:280]
        return CheckResult(changed=True, meaningful=True, new_hash=new_hash,
                          new_value=None, summary=f"The page changed: \"{preview}\"")

    if condition in WatchCondition.NUMERIC:
        number = extract_first_number(normalized)
        if number is None:
            # The content changed, but nothing number-shaped could be found
            # this time - never guess a threshold crossing from nothing.
            return CheckResult(changed=True, meaningful=False, new_hash=new_hash,
                              new_value=previous_value, summary="")
        try:
            threshold = float(condition_value)
        except (TypeError, ValueError):
            return CheckResult(changed=True, meaningful=False, new_hash=new_hash,
                              new_value=str(number), summary="")
        previous_number = _safe_float(previous_value)
        crosses = number < threshold if condition == WatchCondition.VALUE_BELOW \
            else number > threshold
        was_already_crossed = (
            previous_number is not None
            and (previous_number < threshold if condition == WatchCondition.VALUE_BELOW
                 else previous_number > threshold))
        meaningful = crosses and not was_already_crossed
        direction = "below" if condition == WatchCondition.VALUE_BELOW else "above"
        summary = (f"Value is now {number:g}, {direction} the threshold of {threshold:g}"
                  if meaningful else "")
        return CheckResult(changed=True, meaningful=meaningful, new_hash=new_hash,
                          new_value=str(number), summary=summary)

    if condition in WatchCondition.TEXTUAL:
        needle = (condition_value or "").strip().lower()
        contains_now = bool(needle) and needle in normalized.lower()
        contains_before = previous_value == "true"
        if condition == WatchCondition.TEXT_CONTAINS:
            meaningful = contains_now and not contains_before
            summary = f'The page now contains "{condition_value}"' if meaningful else ""
        else:
            meaningful = contains_before and not contains_now
            summary = f'The page no longer contains "{condition_value}"' if meaningful else ""
        return CheckResult(changed=True, meaningful=meaningful, new_hash=new_hash,
                          new_value="true" if contains_now else "false", summary=summary)

    if condition == WatchCondition.BECOMES_AVAILABLE:
        available = looks_available(normalized)
        was_available = previous_value == "true"
        meaningful = available is True and not was_available
        summary = "The item now looks available" if meaningful else ""
        new_value = "unknown" if available is None else str(available).lower()
        return CheckResult(changed=True, meaningful=meaningful, new_hash=new_hash,
                          new_value=new_value, summary=summary)

    return CheckResult(changed=True, meaningful=False, new_hash=new_hash,
                      new_value=previous_value, summary="")


def _safe_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
