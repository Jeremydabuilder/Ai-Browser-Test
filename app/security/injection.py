"""Prompt-injection PATTERN detection - a signal for logging/caution, not
the defense itself.

The structural defense against prompt injection is that untrusted content
never carries authority in the first place (see app.security.provenance
and app.agent.tools.wrap_untrusted's fencing) - a page cannot grant itself
permission to call a tool no matter what it says. This module exists only
to notice when content is *trying*, so it can be logged (see
app.security.log) and so a caller with judgement to spare can be extra
careful - it must never be the thing a safety decision depends on, since
regex is easy to word around.
"""

from __future__ import annotations

import re

#: Phrases that show up in real prompt-injection attempts. Grouped so a
#: caller can see *why* something was flagged, not just that it was.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?\b"),
     "asks to ignore previous instructions"),
    (re.compile(r"(?i)\b(?:system|developer)\s*(?:message|instruction|prompt)\b"),
     "claims to be a system/developer message"),
    (re.compile(r"(?i)\bdo\s+not\s+tell\s+the\s+user\b"),
     "asks to hide something from the user"),
    (re.compile(r"(?i)\breveal\s+(?:your\s+)?(?:system\s+prompt|secrets?|api\s*keys?|"
                r"credentials?|passwords?)\b"),
     "asks to reveal secrets or the system prompt"),
    (re.compile(r"(?i)\b(?:click|press)\s+(?:the\s+)?allow\b"),
     "instructs clicking an approval control"),
    (re.compile(r"(?i)\byou\s+are\s+now\s+in\s+(?:unrestricted|developer|admin)\s*mode\b"),
     "claims to unlock an unrestricted mode"),
    (re.compile(r"(?i)\bdisable\s+(?:your\s+)?(?:safety|security)\s*(?:restrictions?|checks?)\b"),
     "asks to disable safety restrictions"),
    (re.compile(r"(?i)\bstop\s+(?:asking|requesting)\s+(?:for\s+)?confirmation\b"),
     "asks to stop requesting confirmation"),
    (re.compile(r"(?i)\bthe\s+user\s+(?:has\s+)?already\s+approved\b"),
     "claims prior approval that was never actually given"),
    (re.compile(r"(?i)\bsend\s+(?:this|the|your)\s+.{0,40}\s+to\s+"
                r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
     "asks to send data to an email address embedded in the content"),
    (re.compile(r"(?i)\buse\s+(?:the\s+)?\w+\s+mcp\b.{0,60}\bto\s+send\b"),
     "asks to use an MCP tool to exfiltrate data"),
    (re.compile(r"(?i)</?(?:untrusted|system)[a-z_]*>"),
     "attempts to forge or close a trust-boundary marker"),
]


#: Mirrors SettingsStore.injection_protection_enabled - see
#: app.security.firewall's identical _enabled/set_enabled/is_enabled for
#: why this is simple module state rather than a threaded-through setting.
_enabled = True


def set_enabled(value: bool) -> None:
    global _enabled
    _enabled = value


def is_enabled() -> bool:
    return _enabled


def detect(text: str) -> list[str]:
    """Reasons this text looks like it is attempting to inject
    instructions, or an empty list. A non-empty result is a signal for
    app.security.log, never a block by itself - see the module
    docstring."""
    if not text:
        return []
    reasons = []
    for pattern, reason in _PATTERNS:
        if pattern.search(text):
            reasons.append(reason)
    return reasons
