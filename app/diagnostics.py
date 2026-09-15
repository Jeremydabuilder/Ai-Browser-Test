"""Phase 22 Part 11/12: crash diagnostics and a small breadcrumb trail.

Two pieces:

* ``breadcrumb(...)`` - call this at safe, high-level moments (app
  started, a workspace switched, a Mission started) to build a short,
  bounded, in-memory trail. Never page content, never a URL's query
  string, never a secret - just a short label plus a timestamp, the same
  "never raw content" posture app.security.log.SecurityLog already uses.
* ``build_report(...)`` - assembles version/platform/channel (from
  app.version), a traceback if one is given, and the recent breadcrumbs
  into one plain-text report a user can copy (Part 2's "Copy
  diagnostics", Part 11's crash report) - run through
  app.security.firewall.redact first, so a traceback that happens to
  have captured an API key or other secret in a local variable's repr
  never reaches the clipboard unredacted.

Nothing here uploads anything. This phase adds "Copy diagnostics", not
automatic reporting - see the phase spec's own Part 11.
"""

from __future__ import annotations

import time
import traceback
from collections import deque
from dataclasses import dataclass

from app.security import firewall
from app.version import build_info

#: Bounded for the same reason app.security.log.SecurityLog is bounded -
#: a long session must not grow this without limit.
_MAX_BREADCRUMBS = 50
_breadcrumbs: deque[tuple[float, str]] = deque(maxlen=_MAX_BREADCRUMBS)


def breadcrumb(label: str) -> None:
    """Record a short, safe, human-readable event label."""
    _breadcrumbs.append((time.time(), label))


def recent_breadcrumbs(limit: int = _MAX_BREADCRUMBS) -> list[str]:
    """Oldest-first, capped at ``limit``, each formatted as a plain line."""
    items = list(_breadcrumbs)[-limit:]
    return [f"{time.strftime('%H:%M:%S', time.localtime(ts))}  {label}" for ts, label in items]


def clear_breadcrumbs() -> None:
    _breadcrumbs.clear()


@dataclass(frozen=True)
class DiagnosticsReport:
    text: str
    redacted: bool


def build_report(*, exc_info: tuple | None = None, extra_lines: list[str] | None = None) -> DiagnosticsReport:
    """Assemble a plain-text diagnostics report.

    ``exc_info`` is a (type, value, tb) tuple, e.g. from sys.exc_info() -
    formatted the same way an uncaught-exception log entry is (see
    main.py's excepthook), never re-executed or introspected beyond
    ``traceback.format_exception``.
    """
    info = build_info()
    lines = list(info.display_lines())
    lines.append("")

    if exc_info is not None:
        lines.append("Traceback:")
        lines.extend(traceback.format_exception(*exc_info))
        lines.append("")

    crumbs = recent_breadcrumbs()
    if crumbs:
        lines.append("Recent events:")
        lines.extend(crumbs)

    if extra_lines:
        lines.append("")
        lines.extend(extra_lines)

    raw_text = "\n".join(lines)
    redacted_text, findings = firewall.redact(raw_text)
    return DiagnosticsReport(text=redacted_text, redacted=bool(findings))
