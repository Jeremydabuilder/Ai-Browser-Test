"""WatchRunner: schedules checks and turns detection results into state
changes, history rows and notifications - driven by a fake fetcher instead
of a real browser tab (see tests/test_watch_detection.py for the pure
comparison logic this builds on).

Run with:
    python -m unittest tests.test_watch_runner -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.storage import Database  # noqa: E402
from app.storage.watches import WatchStore  # noqa: E402
from app.watches.model import WatchCondition, WatchState  # noqa: E402
from app.watches.runner import MAX_CONSECUTIVE_FAILURES, WatchRunner  # noqa: E402


class FakeFetcher:
    """Answers fetches synchronously (on_done is called immediately) from a
    queue of scripted responses - None means "this fetch failed"."""

    def __init__(self, responses: list[str | None]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def __call__(self, url: str, on_done) -> None:
        self.calls.append(url)
        response = self._responses.pop(0) if self._responses else None
        on_done(response)


def _past_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()


def _make_store():
    tmp = tempfile.TemporaryDirectory()
    db = Database(os.path.join(tmp.name, "t.sqlite3"))
    return WatchStore(db), db, tmp


class FirstCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.db, self._tmp = _make_store()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_the_first_check_establishes_a_baseline_without_alerting(self) -> None:
        watch = self.store.create(title="w", url="https://example.com",
                                  target_type="full_page", condition=WatchCondition.ANY_CHANGE,
                                  check_interval_seconds=3600, next_check_at=_past_iso())
        fetcher = FakeFetcher(["hello world"])
        seen = []
        runner = WatchRunner(self.store, fetcher)
        runner.watch_changed.connect(lambda w: seen.append(w))
        runner.tick()
        self.assertEqual(seen, [])
        updated = self.store.get(watch.id)
        self.assertIsNotNone(updated.baseline_hash)
        self.assertIsNotNone(updated.next_check_at)
        self.assertEqual(updated.failure_count, 0)


class AnyChangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.db, self._tmp = _make_store()
        self.fetcher = FakeFetcher(["version one", "version two"])
        self.runner = WatchRunner(self.store, self.fetcher)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_no_change_produces_no_alert(self) -> None:
        watch = self.store.create(title="w", url="https://example.com",
                                  target_type="full_page", condition=WatchCondition.ANY_CHANGE,
                                  check_interval_seconds=60, next_check_at=_past_iso())
        fetcher = FakeFetcher(["same text", "same text"])
        runner = WatchRunner(self.store, fetcher)
        seen = []
        runner.watch_changed.connect(lambda w: seen.append(w))
        runner.tick()
        self.store.record_check(
            watch.id, state=WatchState.ACTIVE, next_check_at=_past_iso(),
            last_checked_at=self.store.get(watch.id).last_checked_at,
            last_observed_hash=self.store.get(watch.id).last_observed_hash,
            last_observed_value=None)
        runner.tick()
        self.assertEqual(seen, [])

    def test_a_meaningful_change_produces_exactly_one_alert(self) -> None:
        watch = self.store.create(title="w", url="https://example.com",
                                  target_type="full_page", condition=WatchCondition.ANY_CHANGE,
                                  check_interval_seconds=60, next_check_at=_past_iso())
        seen = []
        self.runner.watch_changed.connect(lambda w: seen.append(w))
        self.runner.tick()          # establishes baseline from "version one"
        self.store.record_check(
            watch.id, state=WatchState.ACTIVE, next_check_at=_past_iso(),
            last_checked_at=self.store.get(watch.id).last_checked_at,
            last_observed_hash=self.store.get(watch.id).last_observed_hash,
            last_observed_value=None)
        self.runner.tick()          # "version two" - a real change
        self.assertEqual(len(seen), 1)
        history = self.store.history_for(watch.id)
        self.assertEqual(len(history), 1)

    def test_repeated_identical_changed_state_does_not_repeatedly_alert(self) -> None:
        """A price that stays below the threshold across many checks must
        alert once on the crossing, not again on every later check."""
        watch = self.store.create(
            title="w", url="https://example.com", target_type="full_page",
            condition=WatchCondition.VALUE_BELOW, condition_value="50",
            check_interval_seconds=60, next_check_at=_past_iso())
        fetcher = FakeFetcher(["Price: $100", "Price: $40", "Price: $39", "Price: $38"])
        runner = WatchRunner(self.store, fetcher)
        seen = []
        runner.watch_changed.connect(lambda w: seen.append(w))
        for _ in range(4):
            runner.tick()
            watch = self.store.get(watch.id)
            self.store.record_check(
                watch.id, state=WatchState.ACTIVE, next_check_at=_past_iso(),
                last_checked_at=watch.last_checked_at,
                last_observed_hash=watch.last_observed_hash,
                last_observed_value=watch.last_observed_value)
        self.assertEqual(len(seen), 1)   # only the $100 -> $40 crossing


class FailureHandlingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.db, self._tmp = _make_store()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_a_fetch_failure_is_never_reported_as_a_change(self) -> None:
        watch = self.store.create(title="w", url="https://example.com",
                                  target_type="full_page", condition=WatchCondition.ANY_CHANGE,
                                  check_interval_seconds=60, next_check_at=_past_iso())
        fetcher = FakeFetcher([None])
        runner = WatchRunner(self.store, fetcher)
        seen = []
        runner.watch_changed.connect(lambda w: seen.append(w))
        runner.tick()
        self.assertEqual(seen, [])
        updated = self.store.get(watch.id)
        self.assertEqual(updated.failure_count, 1)
        self.assertEqual(updated.state, WatchState.ACTIVE)

    def test_repeated_failures_move_the_watch_to_needs_attention(self) -> None:
        watch = self.store.create(title="w", url="https://example.com",
                                  target_type="full_page", condition=WatchCondition.ANY_CHANGE,
                                  check_interval_seconds=60, next_check_at=_past_iso())
        fetcher = FakeFetcher([None] * MAX_CONSECUTIVE_FAILURES)
        runner = WatchRunner(self.store, fetcher)
        attention_seen = []
        runner.watch_needs_attention.connect(lambda w: attention_seen.append(w))
        for _ in range(MAX_CONSECUTIVE_FAILURES):
            runner.tick()
            watch = self.store.get(watch.id)
            if watch.state == WatchState.ACTIVE:
                self.store.record_check(
                    watch.id, state=WatchState.ACTIVE, next_check_at=_past_iso(),
                    last_checked_at=watch.last_checked_at,
                    last_observed_hash=watch.last_observed_hash,
                    last_observed_value=watch.last_observed_value,
                    failure_count=watch.failure_count)
        updated = self.store.get(watch.id)
        self.assertEqual(updated.state, WatchState.NEEDS_ATTENTION)
        self.assertEqual(len(attention_seen), 1)
        self.assertIsNone(updated.next_check_at)

    def test_a_successful_check_resets_the_failure_count(self) -> None:
        watch = self.store.create(title="w", url="https://example.com",
                                  target_type="full_page", condition=WatchCondition.ANY_CHANGE,
                                  check_interval_seconds=60, next_check_at=_past_iso())
        fetcher = FakeFetcher([None, "some text"])
        runner = WatchRunner(self.store, fetcher)
        runner.tick()
        self.assertEqual(self.store.get(watch.id).failure_count, 1)
        self.store.record_check(
            watch.id, state=WatchState.ACTIVE, next_check_at=_past_iso(),
            last_checked_at=self.store.get(watch.id).last_checked_at,
            last_observed_hash=self.store.get(watch.id).last_observed_hash,
            last_observed_value=None, failure_count=self.store.get(watch.id).failure_count)
        runner.tick()
        self.assertEqual(self.store.get(watch.id).failure_count, 0)


class PauseResumeCheckNowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.db, self._tmp = _make_store()

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_a_paused_watch_is_never_picked_up_as_due(self) -> None:
        watch = self.store.create(title="w", url="https://example.com",
                                  target_type="full_page", condition=WatchCondition.ANY_CHANGE,
                                  check_interval_seconds=60, next_check_at=_past_iso())
        self.store.set_state(watch.id, WatchState.PAUSED)
        fetcher = FakeFetcher(["text"])
        runner = WatchRunner(self.store, fetcher)
        runner.tick()
        self.assertEqual(fetcher.calls, [])

    def test_resuming_reactivates_and_clears_failures(self) -> None:
        watch = self.store.create(title="w", url="https://example.com",
                                  target_type="full_page", condition=WatchCondition.ANY_CHANGE,
                                  check_interval_seconds=60)
        self.store.record_check(watch.id, state=WatchState.NEEDS_ATTENTION, next_check_at=None,
                                last_checked_at=_past_iso(), failure_count=3)
        fetcher = FakeFetcher([])
        runner = WatchRunner(self.store, fetcher)
        runner.resume(watch.id)
        updated = self.store.get(watch.id)
        self.assertEqual(updated.state, WatchState.ACTIVE)
        self.assertEqual(updated.failure_count, 0)
        self.assertIsNotNone(updated.next_check_at)

    def test_check_now_runs_even_when_not_yet_due(self) -> None:
        watch = self.store.create(
            title="w", url="https://example.com", target_type="full_page",
            condition=WatchCondition.ANY_CHANGE, check_interval_seconds=3600,
            next_check_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        fetcher = FakeFetcher(["text"])
        runner = WatchRunner(self.store, fetcher)
        self.assertTrue(runner.check_now(watch.id))
        self.assertEqual(fetcher.calls, ["https://example.com"])

    def test_check_now_is_refused_while_a_check_is_already_in_flight(self) -> None:
        watch = self.store.create(title="w", url="https://example.com",
                                  target_type="full_page", condition=WatchCondition.ANY_CHANGE,
                                  check_interval_seconds=60, next_check_at=_past_iso())

        deferred = []

        def slow_fetcher(url, on_done):
            deferred.append(on_done)

        runner = WatchRunner(self.store, slow_fetcher)
        runner.tick()  # never calls on_done - simulates an in-flight check
        self.assertFalse(runner.check_now(watch.id))


if __name__ == "__main__":
    unittest.main()
