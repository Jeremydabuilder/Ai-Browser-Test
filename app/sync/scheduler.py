"""Background sync (Part 18) - reuses TaskRunner's existing timer exactly
the way WatchRunner already does (``attach_to``), rather than starting a
second periodic-timing system. Deliberately not coupled to Mission
execution: it only ever calls SyncService.sync_now(), never anything that
touches an AgentSession.

No Qt import here - this class is plain Python, unit-testable without a
QApplication; MainWindow connects its ``on_tick`` to a QTimer's
``timeout`` signal, the same split every other Qt-free engine in this
codebase (ContextComposer, tab_grouping's suggestion engine, ...) uses.
"""

from __future__ import annotations

from typing import Callable


class BackgroundSyncScheduler:
    """Ticks alongside TaskRunner's 15s poll, but only actually syncs every
    ``interval_ticks`` of them (default: ~2 minutes) - Part 18's "do not
    write on every keystroke" debounce, in its simplest workable form:
    a period long enough that a burst of local edits collapses into one
    sync pass, without needing to hook a change signal from every domain
    store this phase touches.
    """

    def __init__(self, sync_service, domain_stores_provider: Callable[[], dict], *,
                interval_ticks: int = 8) -> None:
        self._sync_service = sync_service
        self._domain_stores_provider = domain_stores_provider
        self._interval_ticks = max(1, interval_ticks)
        self._tick_count = 0

    def on_tick(self) -> None:
        if not self._sync_service.enabled:
            self._tick_count = 0
            return
        self._tick_count += 1
        if self._tick_count < self._interval_ticks:
            return
        self._tick_count = 0
        self._sync_service.sync_now(**self._domain_stores_provider())

    def sync_now(self) -> object:
        """The explicit "Sync now" action (Part 16) - runs immediately,
        regardless of the debounce counter, and resets it."""
        self._tick_count = 0
        return self._sync_service.sync_now(**self.domain_stores())

    def domain_stores(self) -> dict:
        return self._domain_stores_provider()
