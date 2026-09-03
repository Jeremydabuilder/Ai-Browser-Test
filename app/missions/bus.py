"""One process-wide announcement channel for Mission changes.

Every window has its own MissionService over a shared database, which means a
Mission deleted in one window is still sitting in another window's panel with
no way to find out. That is a correctness problem today - a stale active
Mission whose rows are gone - and a structural one later, because a browser
that is meant to understand what is happening across the user's work cannot
have each window holding a private opinion about it.

Deliberately tiny: an announcement that something changed, and an id. Never the
data. A listener that cares re-reads from the store, which is the only place
the truth lives.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class MissionBus(QObject):
    """Changes to Missions, announced across every window in this process."""

    #: A Mission was created, renamed, or had its status or contents change.
    changed = Signal(int)        # mission_id
    #: A Mission was deleted - softly or permanently. Holders must let go.
    deleted = Signal(int)        # mission_id


_BUS: MissionBus | None = None


def bus() -> MissionBus:
    """The process-wide bus, created on first use.

    A module-level singleton rather than something passed down through every
    constructor, because the whole point is that windows which do not know
    about each other still hear each other.
    """
    global _BUS
    if _BUS is None:
        _BUS = MissionBus()
    return _BUS
