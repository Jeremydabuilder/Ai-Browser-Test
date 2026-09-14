"""WatchRunner: the timer that decides when to check a Page Watch.

Reuses app/missions/task_runner.py's own QTimer (see TaskRunner.timer)
rather than starting a second background timing system - each tick, this
runner asks the store for due watches and checks at most one at a time, via
a caller-supplied ``fetcher``. One at a time is deliberate: the phase spec
asks for a concurrency cap and no burst of simultaneously open tabs, and a
strict queue of one is the simplest way to guarantee both regardless of how
many watches are due.

``fetcher`` is ``(url, on_done) -> None`` where ``on_done`` is called
exactly once with the page's plain text, or ``None`` on any failure
(timeout, blocked, navigation error, login wall) - this runner never tells
those apart itself; see _on_fetched for why a fetch failure is always
"try again later", never "the page changed".
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from app.storage.watches import WatchStore
from app.watches.detection import evaluate_check
from app.watches.model import Watch, WatchState

#: Consecutive failed checks before a watch stops retrying on its own
#: schedule and asks the user to look at it instead - "Needs attention",
#: never silently forever-retrying and never reported as a page change.
MAX_CONSECUTIVE_FAILURES = 3
#: Backoff after a failed check, capped well under a day so a struggling
#: site is not hammered but a watch does not vanish for a week either.
_BACKOFF_BASE_S = 300
_BACKOFF_CAP_S = 6 * 3600
#: A floor under any configured check interval - see the phase's own
#: "reasonable minimum polling intervals" requirement.
MIN_CHECK_INTERVAL_S = 60


class WatchRunner(QObject):
    #: A meaningful change was detected. Carries the Watch (already updated).
    watch_changed = Signal(object)
    #: A watch just moved to needs_attention after repeated failures.
    watch_needs_attention = Signal(object)

    def __init__(
        self,
        store: WatchStore,
        fetcher: Callable[[str, Callable[[str | None], None]], None],
        parent: QObject | None = None,
        ownership: "object | None" = None,
        device_id: str = "",
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._fetcher = fetcher
        self._checking_id: int | None = None
        #: Phase 20 Part 12 - same ownership gate as TaskRunner's; None
        #: (the default) preserves today's single-device behavior exactly.
        self._ownership = ownership
        self._device_id = device_id

    def attach_to(self, timer: QTimer) -> None:
        """Piggyback on an existing timer's timeout - see the module
        docstring for why this never starts one of its own."""
        timer.timeout.connect(self.tick)

    def tick(self) -> None:
        if self._checking_id is not None:
            return          # a check is already in flight - stay to one at a time
        due = self._store.due_watches(_utc_now())
        if self._ownership is not None:
            due = [w for w in due
                  if self._ownership.is_owned_by("watch", str(w.id), self._device_id)]
        if not due:
            return
        self._check(due[0])

    def check_now(self, watch_id: int) -> bool:
        if self._checking_id is not None:
            return False
        watch = self._store.get(watch_id)
        if watch is None or watch.state not in (WatchState.ACTIVE, WatchState.PAUSED):
            return False
        self._check(watch)
        return True

    def pause(self, watch_id: int) -> None:
        watch = self._store.get(watch_id)
        if watch is not None and watch.state == WatchState.ACTIVE:
            self._store.set_state(watch_id, WatchState.PAUSED)

    def resume(self, watch_id: int) -> None:
        watch = self._store.get(watch_id)
        if watch is None or watch.state not in (WatchState.PAUSED, WatchState.NEEDS_ATTENTION):
            return
        next_check = _utc_now() + timedelta(seconds=max(watch.check_interval_seconds,
                                                        MIN_CHECK_INTERVAL_S))
        self._store.record_check(
            watch_id, state=WatchState.ACTIVE, next_check_at=next_check.isoformat(),
            last_checked_at=watch.last_checked_at or _utc_now().isoformat(),
            last_observed_hash=watch.last_observed_hash,
            last_observed_value=watch.last_observed_value, failure_count=0)

    # -- one check ------------------------------------------------------------
    def _check(self, watch: Watch) -> None:
        self._checking_id = watch.id

        def on_done(text: str | None) -> None:
            self._checking_id = None
            if text is None:
                self._on_fetch_failed(watch)
            else:
                self._on_fetched(watch, text)

        self._fetcher(watch.url, on_done)

    def _on_fetch_failed(self, watch: Watch) -> None:
        """A temporary failure (timeout, blocked, login wall, network error)
        must never be reported as "the page changed" - it is reported as
        nothing at all until it either recovers or repeats enough times to
        need a human's attention."""
        failures = watch.failure_count + 1
        now = _utc_now()
        if failures >= MAX_CONSECUTIVE_FAILURES:
            self._store.record_check(
                watch.id, state=WatchState.NEEDS_ATTENTION, next_check_at=None,
                last_checked_at=now.isoformat(),
                last_observed_hash=watch.last_observed_hash,
                last_observed_value=watch.last_observed_value, failure_count=failures)
            updated = self._store.get(watch.id)
            if updated is not None:
                self.watch_needs_attention.emit(updated)
            return
        backoff = min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * (2 ** (failures - 1)))
        next_check = now + timedelta(seconds=backoff)
        self._store.record_check(
            watch.id, state=WatchState.ACTIVE, next_check_at=next_check.isoformat(),
            last_checked_at=now.isoformat(),
            last_observed_hash=watch.last_observed_hash,
            last_observed_value=watch.last_observed_value, failure_count=failures)

    def _on_fetched(self, watch: Watch, text: str) -> None:
        result = evaluate_check(
            watch.condition, watch.condition_value, text,
            previous_hash=watch.baseline_hash, previous_value=watch.last_observed_value)
        now = _utc_now()
        interval = max(watch.check_interval_seconds, MIN_CHECK_INTERVAL_S)
        next_check = (now + timedelta(seconds=interval)).isoformat()

        baseline_hash = watch.baseline_hash
        baseline_value = watch.baseline_value
        if baseline_hash is None:
            # The very first successful check only establishes the baseline
            # - never an alert on a watch's own creation.
            baseline_hash = result.new_hash
            baseline_value = result.new_value

        self._store.record_check(
            watch.id, state=WatchState.ACTIVE, next_check_at=next_check,
            last_checked_at=now.isoformat(),
            baseline_hash=baseline_hash if watch.baseline_hash is None else None,
            baseline_value=baseline_value if watch.baseline_hash is None else None,
            last_observed_hash=result.new_hash, last_observed_value=result.new_value,
            failure_count=0)

        if result.meaningful:
            self._store.record_change(
                watch.id, observed_at=now.isoformat(), summary=result.summary,
                old_value=watch.last_observed_value, new_value=result.new_value)
            updated = self._store.get(watch.id)
            if updated is not None:
                self.watch_changed.emit(updated)


def _utc_now() -> datetime:
    from app.missions.scheduler import utc_now

    return utc_now()
