"""ScheduledTaskStore: plain persistence, no timing or agent logic - see
tests/test_task_runner.py for the state-machine behaviour built on top of
this.

Run with:
    python -m unittest tests.test_scheduled_tasks_store -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.missions.scheduler import ScheduleKind, TaskState  # noqa: E402
from app.storage import Database  # noqa: E402
from app.storage.scheduled_tasks import ScheduledTaskStore  # noqa: E402


class ScheduledTaskStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmp.name, "t.sqlite3"))
        self.store = ScheduledTaskStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_create_and_get_round_trip(self) -> None:
        task = self.store.create(goal="check the price", schedule_kind=ScheduleKind.DAILY,
                                 time_of_day="09:00")
        self.assertIsNotNone(task)
        fetched = self.store.get(task.id)
        self.assertEqual(fetched.goal, "check the price")
        self.assertEqual(fetched.schedule_kind, ScheduleKind.DAILY)
        self.assertEqual(fetched.time_of_day, "09:00")
        self.assertEqual(fetched.state, TaskState.QUEUED)
        self.assertFalse(fetched.write_attempted)

    def test_a_blank_goal_is_refused(self) -> None:
        self.assertIsNone(self.store.create(goal="   ", schedule_kind=ScheduleKind.ONCE))

    def test_due_tasks_only_returns_queued_tasks_at_or_before_now(self) -> None:
        now = datetime.now(timezone.utc)
        due = self.store.create(goal="due", schedule_kind=ScheduleKind.ONCE,
                                next_run_at=(now - timedelta(minutes=1)).isoformat())
        self.store.create(goal="not due yet", schedule_kind=ScheduleKind.ONCE,
                          next_run_at=(now + timedelta(hours=1)).isoformat())
        paused = self.store.create(goal="paused", schedule_kind=ScheduleKind.ONCE,
                                   next_run_at=(now - timedelta(minutes=1)).isoformat())
        self.store.set_state(paused.id, TaskState.PAUSED)

        result = self.store.due_tasks(now)
        self.assertEqual([t.id for t in result], [due.id])

    def test_all_orders_by_next_run_then_id(self) -> None:
        now = datetime.now(timezone.utc)
        later = self.store.create(goal="later", schedule_kind=ScheduleKind.ONCE,
                                  next_run_at=(now + timedelta(hours=2)).isoformat())
        sooner = self.store.create(goal="sooner", schedule_kind=ScheduleKind.ONCE,
                                   next_run_at=(now + timedelta(hours=1)).isoformat())
        self.assertEqual([t.id for t in self.store.all()], [sooner.id, later.id])

    def test_record_run_result_clears_write_attempted_for_the_next_run(self) -> None:
        task = self.store.create(goal="x", schedule_kind=ScheduleKind.ONCE)
        self.store.set_write_attempted(task.id, True)
        self.assertTrue(self.store.get(task.id).write_attempted)
        self.store.record_run_result(
            task.id, state=TaskState.COMPLETED, next_run_at=None,
            last_run_at=datetime.now(timezone.utc).isoformat(), duration_s=1.5)
        updated = self.store.get(task.id)
        self.assertFalse(updated.write_attempted)
        self.assertEqual(updated.last_duration_s, 1.5)

    def test_task_runs_history_accumulates_across_many_fires(self) -> None:
        task = self.store.create(goal="x", schedule_kind=ScheduleKind.INTERVAL,
                                 interval_seconds=60)
        for _ in range(3):
            run_id = self.store.record_run_start(
                task.id, datetime.now(timezone.utc).isoformat())
            self.store.record_run_finish(
                run_id, finished_at=datetime.now(timezone.utc).isoformat(),
                outcome="completed")
        self.assertEqual(len(self.store.runs_for_task(task.id)), 3)

    def test_remove_deletes_the_task_and_its_history(self) -> None:
        task = self.store.create(goal="x", schedule_kind=ScheduleKind.ONCE)
        self.store.record_run_start(task.id, datetime.now(timezone.utc).isoformat())
        self.store.remove(task.id)
        self.assertIsNone(self.store.get(task.id))
        self.assertEqual(self.store.runs_for_task(task.id), [])


if __name__ == "__main__":
    unittest.main()
