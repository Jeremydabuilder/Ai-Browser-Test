"""WatchStore: persistence and history retention.

Run with:
    python -m unittest tests.test_watch_store -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.storage import Database  # noqa: E402
from app.storage.watches import MAX_HISTORY_PER_WATCH, WatchStore  # noqa: E402
from app.watches.model import WatchCondition, WatchState  # noqa: E402


class WatchStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmp.name, "t.sqlite3"))
        self.store = WatchStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_create_and_get_round_trip(self) -> None:
        watch = self.store.create(title="a page", url="https://example.com",
                                  target_type="full_page", condition=WatchCondition.ANY_CHANGE,
                                  check_interval_seconds=1800)
        self.assertIsNotNone(watch)
        fetched = self.store.get(watch.id)
        self.assertEqual(fetched.title, "a page")
        self.assertEqual(fetched.state, WatchState.ACTIVE)
        self.assertEqual(fetched.failure_count, 0)

    def test_a_blank_title_or_url_is_refused(self) -> None:
        self.assertIsNone(self.store.create(title="", url="https://example.com",
                                            target_type="full_page",
                                            condition=WatchCondition.ANY_CHANGE,
                                            check_interval_seconds=60))
        self.assertIsNone(self.store.create(title="x", url="   ", target_type="full_page",
                                            condition=WatchCondition.ANY_CHANGE,
                                            check_interval_seconds=60))

    def test_due_watches_excludes_paused_and_future_ones(self) -> None:
        now = datetime.now(timezone.utc)
        due = self.store.create(title="due", url="https://example.com", target_type="full_page",
                                condition=WatchCondition.ANY_CHANGE, check_interval_seconds=60,
                                next_check_at=(now - timedelta(minutes=1)).isoformat())
        self.store.create(title="future", url="https://example.com", target_type="full_page",
                          condition=WatchCondition.ANY_CHANGE, check_interval_seconds=60,
                          next_check_at=(now + timedelta(hours=1)).isoformat())
        paused = self.store.create(title="paused", url="https://example.com",
                                   target_type="full_page", condition=WatchCondition.ANY_CHANGE,
                                   check_interval_seconds=60,
                                   next_check_at=(now - timedelta(minutes=1)).isoformat())
        self.store.set_state(paused.id, WatchState.PAUSED)
        self.assertEqual([w.id for w in self.store.due_watches(now)], [due.id])

    def test_baseline_is_only_written_once(self) -> None:
        watch = self.store.create(title="x", url="https://example.com", target_type="full_page",
                                  condition=WatchCondition.ANY_CHANGE, check_interval_seconds=60)
        self.store.record_check(watch.id, state=WatchState.ACTIVE, next_check_at=None,
                                last_checked_at="t1", baseline_hash="h1", baseline_value="v1",
                                last_observed_hash="h1", last_observed_value="v1")
        # A later check must not overwrite the baseline, only last_observed_*.
        self.store.record_check(watch.id, state=WatchState.ACTIVE, next_check_at=None,
                                last_checked_at="t2", last_observed_hash="h2",
                                last_observed_value="v2")
        updated = self.store.get(watch.id)
        self.assertEqual(updated.baseline_hash, "h1")
        self.assertEqual(updated.last_observed_hash, "h2")

    def test_history_retention_trims_to_the_newest_entries(self) -> None:
        watch = self.store.create(title="x", url="https://example.com", target_type="full_page",
                                  condition=WatchCondition.ANY_CHANGE, check_interval_seconds=60)
        base = datetime.now(timezone.utc)
        for i in range(MAX_HISTORY_PER_WATCH + 10):
            self.store.record_change(
                watch.id, observed_at=(base + timedelta(minutes=i)).isoformat(),
                summary=f"change {i}", old_value=None, new_value=None)
        history = self.store.history_for(watch.id, limit=MAX_HISTORY_PER_WATCH + 10)
        self.assertEqual(len(history), MAX_HISTORY_PER_WATCH)
        # The newest entries survive, not the oldest.
        self.assertEqual(history[0]["summary"], f"change {MAX_HISTORY_PER_WATCH + 9}")

    def test_remove_deletes_the_watch_and_its_history(self) -> None:
        watch = self.store.create(title="x", url="https://example.com", target_type="full_page",
                                  condition=WatchCondition.ANY_CHANGE, check_interval_seconds=60)
        self.store.record_change(watch.id, observed_at="t", summary="s",
                                 old_value=None, new_value=None)
        self.store.remove(watch.id)
        self.assertIsNone(self.store.get(watch.id))
        self.assertEqual(self.store.history_for(watch.id), [])


if __name__ == "__main__":
    unittest.main()
