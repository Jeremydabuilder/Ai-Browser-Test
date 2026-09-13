"""TaskRunner: the scheduling layer over the one AgentSession a window owns.

Uses a lightweight fake session (same signal names as the real
AgentSession, driven by hand) rather than the full Claude/browser stack -
what is under test here is TaskRunner's own state machine (write_attempted
tracking, waiting_for_approval, crash recovery, requeueing), not the agent
loop itself, which tests/test_agent.py already covers.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_task_runner -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.session import AgentState, Step, StepState  # noqa: E402
from app.missions.scheduler import ScheduleKind, TaskState  # noqa: E402
from app.missions.task_runner import TaskRunner  # noqa: E402
from app.storage import Database  # noqa: E402
from app.storage.scheduled_tasks import ScheduledTaskStore  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class FakeMission:
    def __init__(self, id: int, title: str = "") -> None:
        self.id = id
        self.title = title


class FakeMissions:
    """Stands in for MissionService: TaskRunner only ever calls start()/
    resume(), never anything about tabs or the browser. Inserts real rows
    into the missions table (rather than making up ids) since
    scheduled_tasks.mission_id is a real foreign key."""

    def __init__(self, db) -> None:
        self._db = db
        self.started: list[str] = []
        self.resumed: list[int] = []

    def start(self, goal: str):
        self.started.append(goal)
        now = datetime.now(timezone.utc).isoformat()
        title = f"Mission: {goal[:40]}"
        cursor = self._db.execute(
            "INSERT INTO missions (title, goal, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (title, goal, now, now))
        return FakeMission(cursor.lastrowid, title=title)

    def resume(self, mission_id: int):
        self.resumed.append(mission_id)
        row = self._db.query_one("SELECT title FROM missions WHERE id = ?", (mission_id,))
        return FakeMission(mission_id, title=row["title"] if row else "")


class FakeSession(QObject):
    """Same signal names/shapes as AgentSession, driven by hand from a test
    instead of a scripted Claude transport - see the module docstring."""

    state_changed = Signal(str)
    step_changed = Signal(object)
    confirmation_required = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.busy = False
        self.sent: list[str] = []

    def send(self, goal: str) -> bool:
        if self.busy:
            return False
        self.busy = True
        self.sent.append(goal)
        return True

    # -- test-side driving ------------------------------------------------
    def step(self, tool: str, state: str = StepState.RUNNING) -> None:
        self.step_changed.emit(Step(index=0, description="doing it", state=state, tool=tool))

    def request_confirmation(self) -> None:
        self.state_changed.emit(AgentState.AWAITING_CONFIRMATION)
        self.confirmation_required.emit(object())

    def approve_and_continue(self) -> None:
        self.state_changed.emit(AgentState.ACTING)

    def succeed(self) -> None:
        self.busy = False
        self.finished.emit()

    def fail(self, message: str = "something broke") -> None:
        self.error.emit(message)
        self.busy = False
        self.finished.emit()


def _make_store() -> tuple[ScheduledTaskStore, Database, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    db = Database(os.path.join(tmp.name, "t.sqlite3"))
    return ScheduledTaskStore(db), db, tmp


def _past_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()


class OneTimeScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.db, self._tmp = _make_store()
        self.missions = FakeMissions(self.db)
        self.session = FakeSession()
        self.runner = TaskRunner(self.store, self.missions, lambda: self.session)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_a_due_one_time_task_fires_and_completes(self) -> None:
        task = self.store.create(goal="check the price", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        self.runner._tick()
        self.assertEqual(self.session.sent, ["check the price"])
        self.assertEqual(self.store.get(task.id).state, TaskState.RUNNING)
        self.assertEqual(self.missions.started, ["check the price"])

        self.session.succeed()
        updated = self.store.get(task.id)
        self.assertEqual(updated.state, TaskState.COMPLETED)
        self.assertIsNone(updated.next_run_at)
        self.assertIsNotNone(updated.last_run_at)
        self.assertIsNotNone(updated.mission_id)

    def test_a_future_one_time_task_does_not_fire(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.store.create(goal="later", schedule_kind=ScheduleKind.ONCE, next_run_at=future)
        self.runner._tick()
        self.assertEqual(self.session.sent, [])

    def test_an_existing_mission_is_resumed_rather_than_started_fresh(self) -> None:
        existing = self.missions.start("an earlier trip")
        task = self.store.create(goal="continue researching", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso(), mission_id=existing.id,
                                 mission_title="Trip")
        self.missions.started.clear()
        self.runner._tick()
        self.assertEqual(self.missions.resumed, [existing.id])
        self.assertEqual(self.missions.started, [])
        self.session.succeed()
        self.assertEqual(self.store.get(task.id).mission_id, existing.id)


class RecurringScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.db, self._tmp = _make_store()
        self.missions = FakeMissions(self.db)
        self.session = FakeSession()
        self.runner = TaskRunner(self.store, self.missions, lambda: self.session)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_a_completed_recurring_task_is_requeued_with_a_new_next_run(self) -> None:
        task = self.store.create(goal="daily digest", schedule_kind=ScheduleKind.INTERVAL,
                                 interval_seconds=3600, next_run_at=_past_iso())
        self.runner._tick()
        self.session.succeed()
        updated = self.store.get(task.id)
        self.assertEqual(updated.state, TaskState.QUEUED)
        self.assertIsNotNone(updated.next_run_at)
        self.assertGreater(
            datetime.fromisoformat(updated.next_run_at), datetime.now(timezone.utc))

    def test_a_failed_recurring_task_still_gets_requeued(self) -> None:
        """One bad run does not permanently kill a recurring schedule - see
        TaskRunner._on_finished. The failure stays visible via last_error."""
        task = self.store.create(goal="daily digest", schedule_kind=ScheduleKind.DAILY,
                                 time_of_day="09:00", next_run_at=_past_iso())
        self.runner._tick()
        self.session.fail("the site was down")
        updated = self.store.get(task.id)
        self.assertEqual(updated.state, TaskState.QUEUED)
        self.assertEqual(updated.last_error, "the site was down")
        self.assertIsNotNone(updated.next_run_at)


class FailedRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.db, self._tmp = _make_store()
        self.missions = FakeMissions(self.db)
        self.session = FakeSession()
        self.runner = TaskRunner(self.store, self.missions, lambda: self.session)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_a_failed_one_time_task_ends_in_the_failed_state(self) -> None:
        task = self.store.create(goal="one shot", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        self.runner._tick()
        self.session.fail("provider error")
        updated = self.store.get(task.id)
        self.assertEqual(updated.state, TaskState.FAILED)
        self.assertEqual(updated.last_error, "provider error")
        self.assertIsNone(updated.next_run_at)

    def test_mission_failed_signal_carries_the_task(self) -> None:
        task = self.store.create(goal="one shot", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        seen = []
        self.runner.mission_failed.connect(lambda t: seen.append(t))
        self.runner._tick()
        self.session.fail("nope")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].id, task.id)

    def test_mission_completed_signal_carries_the_task(self) -> None:
        task = self.store.create(goal="one shot", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        seen = []
        self.runner.mission_completed.connect(lambda t: seen.append(t))
        self.runner._tick()
        self.session.succeed()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].id, task.id)


class PauseResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.db, self._tmp = _make_store()
        self.missions = FakeMissions(self.db)
        self.session = FakeSession()
        self.runner = TaskRunner(self.store, self.missions, lambda: self.session)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_a_paused_task_is_never_picked_up_as_due(self) -> None:
        task = self.store.create(goal="x", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        self.runner.pause(task.id)
        self.assertEqual(self.store.get(task.id).state, TaskState.PAUSED)
        self.runner._tick()
        self.assertEqual(self.session.sent, [])

    def test_resuming_a_paused_task_requeues_it_with_a_fresh_next_run(self) -> None:
        task = self.store.create(goal="x", schedule_kind=ScheduleKind.INTERVAL,
                                 interval_seconds=3600, next_run_at=_past_iso())
        self.runner.pause(task.id)
        self.runner.resume(task.id)
        updated = self.store.get(task.id)
        self.assertEqual(updated.state, TaskState.QUEUED)
        self.assertIsNotNone(updated.next_run_at)
        self.assertGreater(
            datetime.fromisoformat(updated.next_run_at), datetime.now(timezone.utc))

    def test_pausing_a_running_task_is_refused(self) -> None:
        task = self.store.create(goal="x", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        self.runner._tick()
        self.assertEqual(self.store.get(task.id).state, TaskState.RUNNING)
        self.runner.pause(task.id)
        # Refused - only a queued task can be paused straight from the store.
        self.assertEqual(self.store.get(task.id).state, TaskState.RUNNING)


class WaitingForApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.db, self._tmp = _make_store()
        self.missions = FakeMissions(self.db)
        self.session = FakeSession()
        self.runner = TaskRunner(self.store, self.missions, lambda: self.session)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_a_confirmation_request_moves_the_task_to_waiting_for_approval(self) -> None:
        task = self.store.create(goal="buy the tickets", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        self.runner._tick()
        seen = []
        self.runner.approval_required.connect(lambda t: seen.append(t))
        self.session.request_confirmation()
        self.assertEqual(self.store.get(task.id).state, TaskState.WAITING_FOR_APPROVAL)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].id, task.id)

    def test_approving_returns_the_task_to_running(self) -> None:
        task = self.store.create(goal="buy the tickets", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        self.runner._tick()
        self.session.request_confirmation()
        self.session.approve_and_continue()
        self.assertEqual(self.store.get(task.id).state, TaskState.RUNNING)
        self.session.succeed()
        self.assertEqual(self.store.get(task.id).state, TaskState.COMPLETED)


class WriteAttemptedTests(unittest.TestCase):
    """The crash-safety flag: set the moment a non-read-only tool is about
    to run, before it actually does - see TaskRunner._on_step_changed."""

    def setUp(self) -> None:
        self.store, self.db, self._tmp = _make_store()
        self.missions = FakeMissions(self.db)
        self.session = FakeSession()
        self.runner = TaskRunner(self.store, self.missions, lambda: self.session)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_a_read_only_step_never_sets_write_attempted(self) -> None:
        task = self.store.create(goal="look something up", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        self.runner._tick()
        self.session.step("browser_get_page_text")
        self.assertFalse(self.store.get(task.id).write_attempted)

    def test_a_write_step_sets_write_attempted_before_the_run_finishes(self) -> None:
        task = self.store.create(goal="click something", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        self.runner._tick()
        self.session.step("browser_click")
        self.assertTrue(self.store.get(task.id).write_attempted)

    def test_write_attempted_resets_once_the_run_finishes(self) -> None:
        """A task's own last write_attempted flag is scoped to one run - see
        ScheduledTaskStore.record_run_result. A later run starts clean."""
        task = self.store.create(goal="click something", schedule_kind=ScheduleKind.INTERVAL,
                                 interval_seconds=3600, next_run_at=_past_iso())
        self.runner._tick()
        self.session.step("browser_click")
        self.session.succeed()
        self.assertFalse(self.store.get(task.id).write_attempted)


class CrashRecoveryTests(unittest.TestCase):
    """No TaskRunner/QTimer involved here - this is what happens on the very
    next startup after the app died mid-run, before anything is ticking."""

    def setUp(self) -> None:
        self.store, self.db, self._tmp = _make_store()
        self.missions = FakeMissions(self.db)
        self.session = FakeSession()
        self.runner = TaskRunner(self.store, self.missions, lambda: self.session)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_a_task_left_running_with_no_write_attempted_is_marked_safe_to_retry(self) -> None:
        task = self.store.create(goal="look something up", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        self.store.set_state(task.id, TaskState.RUNNING)
        self.store.record_run_start(task.id, datetime.now(timezone.utc).isoformat())

        recovered = self.runner.recover_after_restart()
        self.assertEqual([t.id for t in recovered], [task.id])
        updated = self.store.get(task.id)
        self.assertEqual(updated.state, TaskState.FAILED)
        self.assertIn("safe to retry", updated.last_error)

    def test_a_task_left_running_with_a_write_attempted_warns_before_retrying(self) -> None:
        task = self.store.create(goal="buy it", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        self.store.set_state(task.id, TaskState.RUNNING)
        self.store.set_write_attempted(task.id, True)
        self.store.record_run_start(task.id, datetime.now(timezone.utc).isoformat())

        self.runner.recover_after_restart()
        updated = self.store.get(task.id)
        self.assertEqual(updated.state, TaskState.FAILED)
        self.assertIn("review before retrying", updated.last_error)

    def test_recovery_never_silently_requeues_a_recurring_task(self) -> None:
        """Even a recurring schedule stops and waits for the user after an
        interrupted run - never an automatic silent replay of a write whose
        result is uncertain."""
        task = self.store.create(goal="daily", schedule_kind=ScheduleKind.DAILY,
                                 time_of_day="09:00", next_run_at=_past_iso())
        self.store.set_state(task.id, TaskState.RUNNING)
        self.store.set_write_attempted(task.id, True)
        self.store.record_run_start(task.id, datetime.now(timezone.utc).isoformat())

        self.runner.recover_after_restart()
        updated = self.store.get(task.id)
        self.assertEqual(updated.state, TaskState.FAILED)
        self.assertIsNone(updated.next_run_at)

    def test_recovery_closes_the_open_audit_run_too(self) -> None:
        task = self.store.create(goal="x", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        self.store.set_state(task.id, TaskState.RUNNING)
        run_id = self.store.record_run_start(task.id, datetime.now(timezone.utc).isoformat())

        self.runner.recover_after_restart()
        runs = self.store.runs_for_task(task.id)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["id"], run_id)
        self.assertEqual(runs[0]["outcome"], "failed")
        self.assertIsNotNone(runs[0]["finished_at"])

    def test_a_finished_task_is_left_alone_by_recovery(self) -> None:
        task = self.store.create(goal="x", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        self.store.record_run_result(
            task.id, state=TaskState.COMPLETED, next_run_at=None,
            last_run_at=datetime.now(timezone.utc).isoformat(), duration_s=1.0)
        recovered = self.runner.recover_after_restart()
        self.assertEqual(recovered, [])
        self.assertEqual(self.store.get(task.id).state, TaskState.COMPLETED)


class RestartPersistenceTests(unittest.TestCase):
    """No AgentSession or TaskRunner needed here - only that the store's own
    data genuinely survives the file being closed and reopened."""

    def test_a_schedule_survives_closing_and_reopening_the_database(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            path = os.path.join(tmp.name, "t.sqlite3")
            db = Database(path)
            store = ScheduledTaskStore(db)
            task = store.create(goal="survive a restart", schedule_kind=ScheduleKind.DAILY,
                                time_of_day="08:00", next_run_at=_past_iso())
            run_id = store.record_run_start(task.id, datetime.now(timezone.utc).isoformat())
            store.record_run_finish(run_id, finished_at=datetime.now(timezone.utc).isoformat(),
                                    outcome="completed")
            db.close()

            reopened = Database(path)
            store2 = ScheduledTaskStore(reopened)
            restored = store2.get(task.id)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.goal, "survive a restart")
            self.assertEqual(restored.schedule_kind, ScheduleKind.DAILY)
            self.assertEqual(len(store2.runs_for_task(task.id)), 1)
            reopened.close()
        finally:
            tmp.cleanup()


class MissionHistoryAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, self.db, self._tmp = _make_store()
        self.missions = FakeMissions(self.db)
        self.session = FakeSession()
        self.runner = TaskRunner(self.store, self.missions, lambda: self.session)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_every_fire_leaves_its_own_audit_row_even_across_many_runs(self) -> None:
        task = self.store.create(goal="daily digest", schedule_kind=ScheduleKind.INTERVAL,
                                 interval_seconds=3600, next_run_at=_past_iso())
        self.runner._tick()
        self.session.succeed()

        self.store.set_state(task.id, TaskState.QUEUED)
        # Force it due again for a second cycle.
        self.store.record_run_result(
            task.id, state=TaskState.QUEUED, next_run_at=_past_iso(),
            last_run_at=self.store.get(task.id).last_run_at, duration_s=1.0)
        self.runner._tick()
        self.session.fail("second run broke")

        runs = self.store.runs_for_task(task.id)
        self.assertEqual(len(runs), 2)
        outcomes = sorted(r["outcome"] for r in runs)
        self.assertEqual(outcomes, ["completed", "failed"])
        # The task's own last_error/last_run_at show the *latest* run only -
        # task_runs is what makes the full history visible.
        self.assertEqual(self.store.get(task.id).last_error, "second run broke")


if __name__ == "__main__":
    unittest.main()
