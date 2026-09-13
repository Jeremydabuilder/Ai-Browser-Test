"""Task Center UI: the table reflects each ScheduledTask's state, and the
per-row actions (Run Now/Pause/Resume/Delete) enable only when they are
actually valid for the selected row's state.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_task_center_ui -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from app.missions.scheduler import ScheduleKind, TaskState  # noqa: E402
from app.storage import Database  # noqa: E402
from app.storage.scheduled_tasks import ScheduledTaskStore  # noqa: E402
from app.ui.task_center import TaskCenterDialog  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class _FakeRunner:
    def __init__(self) -> None:
        self.run_now_calls: list[int] = []
        self.pause_calls: list[int] = []
        self.resume_calls: list[int] = []
        self.run_now_result = True

    def run_now(self, task_id: int) -> bool:
        self.run_now_calls.append(task_id)
        return self.run_now_result

    def pause(self, task_id: int) -> None:
        self.pause_calls.append(task_id)

    def resume(self, task_id: int) -> None:
        self.resume_calls.append(task_id)


def _past_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()


class TaskCenterUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._tmp.name, "t.sqlite3"))
        self.store = ScheduledTaskStore(self.db)
        self.runner = _FakeRunner()
        self.dialog = TaskCenterDialog(self.store, self.runner, missions=None)

    def tearDown(self) -> None:
        self.dialog.close()
        self.db.close()
        self._tmp.cleanup()

    def test_each_row_shows_the_tasks_own_state_label(self) -> None:
        self.store.create(goal="a queued one", schedule_kind=ScheduleKind.ONCE,
                          next_run_at=_past_iso())
        paused = self.store.create(goal="a paused one", schedule_kind=ScheduleKind.ONCE,
                                   next_run_at=_past_iso())
        self.store.set_state(paused.id, TaskState.PAUSED)
        self.dialog.refresh()
        labels = {self.dialog.table.item(row, 0).text(): self.dialog.table.item(row, 1).text()
                 for row in range(self.dialog.table.rowCount())}
        self.assertEqual(labels["a queued one"[:60]], "Queued")
        self.assertEqual(labels["a paused one"[:60]], "Paused")

    def test_a_failed_tasks_error_is_shown_in_its_own_column(self) -> None:
        task = self.store.create(goal="broke", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        self.store.record_run_result(
            task.id, state=TaskState.FAILED, next_run_at=None,
            last_run_at=datetime.now(timezone.utc).isoformat(), duration_s=1.0,
            error="the page timed out")
        self.dialog.refresh()
        self.assertEqual(self.dialog.table.item(0, 5).text(), "the page timed out")
        self.assertEqual(self.dialog.table.item(0, 1).text(), "Failed")

    def test_run_now_is_disabled_for_a_running_task(self) -> None:
        task = self.store.create(goal="in flight", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        self.store.set_state(task.id, TaskState.RUNNING)
        self.dialog.refresh()
        self.dialog.table.selectRow(0)
        self.assertFalse(self.dialog.run_now_button.isEnabled())
        self.assertFalse(self.dialog.pause_button.isEnabled())
        self.assertFalse(self.dialog.resume_button.isEnabled())

    def test_pause_is_only_enabled_for_a_queued_task(self) -> None:
        task = self.store.create(goal="queued", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        self.dialog.refresh()
        self.dialog.table.selectRow(0)
        self.assertTrue(self.dialog.pause_button.isEnabled())
        self.assertFalse(self.dialog.resume_button.isEnabled())

    def test_resume_is_only_enabled_for_a_paused_task(self) -> None:
        task = self.store.create(goal="paused", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        self.store.set_state(task.id, TaskState.PAUSED)
        self.dialog.refresh()
        self.dialog.table.selectRow(0)
        self.assertTrue(self.dialog.resume_button.isEnabled())
        self.assertFalse(self.dialog.pause_button.isEnabled())

    def test_pause_button_calls_through_to_the_runner(self) -> None:
        task = self.store.create(goal="queued", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        self.dialog.refresh()
        self.dialog.table.selectRow(0)
        self.dialog._on_pause()
        self.assertEqual(self.runner.pause_calls, [task.id])

    def test_run_now_button_calls_through_to_the_runner(self) -> None:
        task = self.store.create(goal="queued", schedule_kind=ScheduleKind.ONCE,
                                 next_run_at=_past_iso())
        self.dialog.refresh()
        self.dialog.table.selectRow(0)
        self.dialog._on_run_now()
        self.assertEqual(self.runner.run_now_calls, [task.id])

    def test_creating_a_new_schedule_adds_a_row(self) -> None:
        self.store.create(goal="already there", schedule_kind=ScheduleKind.ONCE,
                          next_run_at=_past_iso())
        self.dialog.refresh()
        self.assertEqual(self.dialog.table.rowCount(), 1)
        self.store.create(goal="added later", schedule_kind=ScheduleKind.DAILY,
                          time_of_day="09:00", next_run_at=_past_iso())
        self.dialog.refresh()
        self.assertEqual(self.dialog.table.rowCount(), 2)


if __name__ == "__main__":
    unittest.main()
