"""A lightweight, in-memory security event log.

Records that something protective happened - a secret was redacted, an
outbound send was blocked, an injection attempt was flagged, an
unauthorized tool attempt was refused, an external client tried to claim
approval it never had - without ever storing the secret, the full
sensitive payload, or the raw injected text itself. An event's ``detail``
is always a short, pre-summarized, secret-free line (see
app.security.firewall.summarize) - never raw content passed through
verbatim.

In-memory only for now (bounded, so a long session cannot grow it
without limit) - see the phase report's known limitations for why this
was not also persisted to the on-disk database this pass.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any


class EventType:
    SECRET_REDACTED = "secret_redacted"
    OUTBOUND_BLOCKED = "outbound_blocked"
    INJECTION_DETECTED = "injection_detected"
    UNAUTHORIZED_TOOL_ATTEMPT = "unauthorized_tool_attempt"
    EXTERNAL_CLIENT_VIOLATION = "external_client_violation"


@dataclass(frozen=True)
class SecurityEvent:
    event_type: str
    detail: str
    timestamp: float = field(default_factory=time.time)
    #: Where this happened - a tool name, an MCP server id, a role name.
    #: Never a URL's full query string or anything else that could itself
    #: carry sensitive data.
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"event_type": self.event_type, "detail": self.detail,
                "timestamp": self.timestamp, "source": self.source}


class SecurityLog:
    """Bounded, append-only, process-local. One instance is shared
    process-wide (see the module-level ``security_log`` below) so every
    caller logs to the same place a Settings/debug view could read from,
    without threading a store reference through every layer that might
    want to log something."""

    def __init__(self, limit: int = 500) -> None:
        self._limit = limit
        self._events: deque[SecurityEvent] = deque(maxlen=limit)

    def record(self, event_type: str, detail: str, *, source: str = "") -> SecurityEvent:
        event = SecurityEvent(event_type=event_type, detail=detail, source=source)
        self._events.append(event)
        return event

    def recent(self, limit: int | None = None) -> list[SecurityEvent]:
        events = list(self._events)
        return events[-limit:] if limit else events

    def clear(self) -> None:
        self._events.clear()


#: The process-wide log. Tests that need isolation construct their own
#: SecurityLog() instead of using this one.
security_log = SecurityLog()
