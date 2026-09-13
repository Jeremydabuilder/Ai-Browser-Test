"""Mission Plan UI: the plan-preview dialog ("Py plans to do N steps",
Start/Edit/Cancel) and the Workstreams/Mission-Plan checklist dialog's
Retry/Skip/Cancel actions.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mission_plan_ui -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.missions.coordinator import CoordinatorLimits, MissionCoordinator, WorkerState  # noqa: E402
from app.missions.repository import MissionStore  # noqa: E402
from app.missions.service import MissionService  # noqa: E402
from app.storage import Database  # noqa: E402
from app.ui.mission_plan_preview_dialog import MissionPlanPreviewDialog  # noqa: E402
from app.ui.workstreams_dialog import WorkstreamsDialog  # noqa: E402
from tests.test_mission_graph_coordinator import SessionQueueFactory, _plan_json  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


def _make_missions():
    tmp = tempfile.TemporaryDirectory()
    db = Database(os.path.join(tmp.name, "t.sqlite3"))
    missions = MissionService(MissionStore(db), controller=None, tabs=None)
    return missions, db, tmp


class PlanPreviewDialogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.missions, self.db, self._tmp = _make_missions()
        self.missions.start("compare product A vs product B and write a recommendation")
        self.factory = SessionQueueFactory()
        self.coordinator = MissionCoordinator(
            self.missions, self.factory, CoordinatorLimits(), require_plan_approval=True)
        self.dialog = MissionPlanPreviewDialog(self.coordinator)

    def tearDown(self) -> None:
        self.dialog.close()
        self.db.close()
        self._tmp.cleanup()

    def _plan(self) -> None:
        self.coordinator.run("compare product A vs product B and write a recommendation")
        self.factory.built[0].answer(_plan_json([
            {"role": "researcher", "title": "Research A", "instructions": "look up A"},
            {"role": "writer", "title": "Write", "instructions": "write it up",
             "depends_on": [1]},
        ]))

    def test_the_summary_names_the_step_count(self) -> None:
        self._plan()
        self.assertIn("2 steps", self.dialog.summary_label.text())

    def test_the_planner_itself_is_not_listed_as_a_step(self) -> None:
        self._plan()
        labels = [self.dialog.list_widget.item(i).text()
                 for i in range(self.dialog.list_widget.count())]
        self.assertTrue(all("Planner" not in label for label in labels))

    def test_start_button_begins_execution_and_closes_the_dialog(self) -> None:
        self._plan()
        self.dialog._on_start()
        self.assertEqual(self.dialog.result(), self.dialog.DialogCode.Accepted)
        researcher = next(t for t in self.coordinator.tasks if t.role == "researcher")
        self.assertEqual(researcher.state, WorkerState.RUNNING)

    def test_cancel_button_rejects_the_plan_without_running_anything(self) -> None:
        self._plan()
        self.dialog._on_cancel()
        self.assertTrue(all(t.state == WorkerState.CANCELLED for t in self.coordinator.tasks))

    def test_removing_a_step_updates_both_the_list_and_the_coordinator(self) -> None:
        self.missions.start("compare product A vs product B vs product C and write a report")
        self.coordinator.run("compare product A vs product B vs product C and write a report")
        self.factory.built[0].answer(_plan_json([
            {"role": "researcher", "title": "Research A", "instructions": "a"},
            {"role": "researcher", "title": "Research B", "instructions": "b"},
            {"role": "writer", "title": "Write", "instructions": "write"},
        ]))
        before_count = self.dialog.list_widget.count()
        self.dialog.list_widget.setCurrentRow(1)   # "Research B"
        self.dialog._on_remove()
        self.assertEqual(self.dialog.list_widget.count(), before_count - 1)
        removed = next((t for t in self.coordinator.tasks if t.title == "Research B"), None)
        self.assertIsNone(removed)


class MissionPlanDialogRetrySkipCancelUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.missions, self.db, self._tmp = _make_missions()
        self.missions.start("compare product A vs product B and write a recommendation")
        self.factory = SessionQueueFactory()
        self.coordinator = MissionCoordinator(self.missions, self.factory, CoordinatorLimits())
        self.dialog = WorkstreamsDialog(self.coordinator)

    def tearDown(self) -> None:
        self.dialog.close()
        self.db.close()
        self._tmp.cleanup()

    def _run(self) -> None:
        self.coordinator.run("compare product A vs product B and write a recommendation")
        self.factory.built[0].answer(_plan_json([
            {"role": "researcher", "title": "R", "instructions": "x"},
            {"role": "writer", "title": "Write", "instructions": "write", "depends_on": [1]},
        ]))

    def test_retry_button_is_enabled_only_for_a_failed_node(self) -> None:
        self._run()
        self.factory.built[1].fail("broke")
        for row in range(self.dialog.table.rowCount()):
            if self.dialog.table.item(row, 0).text() == "Researcher":
                self.dialog.table.selectRow(row)
                break
        self.assertTrue(self.dialog.retry_button.isEnabled())

    def test_clicking_retry_relaunches_the_node(self) -> None:
        self._run()
        researcher = next(t for t in self.coordinator.tasks if t.role == "researcher")
        self.factory.built[1].fail("broke")
        for row in range(self.dialog.table.rowCount()):
            if self.dialog.table.item(row, 0).text() == "Researcher":
                self.dialog.table.selectRow(row)
                break
        self.dialog._on_retry()
        self.assertEqual(researcher.state, WorkerState.RUNNING)

    def test_the_detail_panel_shows_the_selected_nodes_information(self) -> None:
        self._run()
        self.missions.save_finding_from_source("A is great", "https://a.example", "A")
        self.factory.built[1].answer("found something")
        for row in range(self.dialog.table.rowCount()):
            if self.dialog.table.item(row, 0).text() == "Researcher":
                self.dialog.table.selectRow(row)
                break
        text = self.dialog.detail_text.toPlainText()
        self.assertIn("Researcher", text)
        self.assertIn("Findings contributed: 1", text)

    def test_cancel_mission_button_stops_the_whole_run(self) -> None:
        self._run()
        self.dialog._on_cancel_mission()
        self.assertTrue(all(t.state == WorkerState.CANCELLED for t in self.coordinator.tasks))


if __name__ == "__main__":
    unittest.main()
