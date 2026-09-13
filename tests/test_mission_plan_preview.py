"""Phase 10 PLAN TRANSPARENCY: "Py plans to do N steps" before anything
runs, with simple editing (rename / remove an optional step) - see
MissionCoordinator.require_plan_approval, start_execution, reject_plan,
rename_task, remove_task.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mission_plan_preview -v
"""

from __future__ import annotations

import json
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


class PlanPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.missions, self.db, self._tmp = _make_missions()
        self.missions.start("compare product A vs product B and write a recommendation")
        self.factory = SessionQueueFactory()
        self.coordinator = MissionCoordinator(
            self.missions, self.factory, CoordinatorLimits(), require_plan_approval=True)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _plan(self) -> None:
        self.coordinator.run("compare product A vs product B and write a recommendation")
        self.factory.built[0].answer(_plan_json([
            {"role": "researcher", "title": "Research A", "instructions": "look up A"},
            {"role": "writer", "title": "Write", "instructions": "write it up",
             "depends_on": [1]},
        ]))

    def test_the_plan_is_built_but_nothing_runs_until_start_execution(self) -> None:
        seen = []
        self.coordinator.plan_ready.connect(lambda: seen.append(True))
        self._plan()
        self.assertTrue(seen)
        # Only the Planner session exists - the Researcher has not been
        # launched, even though its dependencies are already satisfied.
        self.assertEqual(len(self.factory.built), 1)
        self.assertTrue(all(t.state == WorkerState.QUEUED for t in self.coordinator.tasks))

    def test_start_execution_begins_the_run(self) -> None:
        self._plan()
        self.assertTrue(self.coordinator.start_execution())
        self.assertEqual(len(self.factory.built), 2)
        researcher = next(t for t in self.coordinator.tasks if t.role == "researcher")
        self.assertEqual(researcher.state, WorkerState.RUNNING)

    def test_start_execution_is_a_no_op_once_already_started(self) -> None:
        self._plan()
        self.assertTrue(self.coordinator.start_execution())
        self.assertFalse(self.coordinator.start_execution())

    def test_reject_plan_cancels_everything_without_running_anything(self) -> None:
        self._plan()
        self.coordinator.reject_plan()
        self.assertTrue(all(t.state == WorkerState.CANCELLED for t in self.coordinator.tasks))
        self.assertEqual(len(self.factory.built), 1)   # only the Planner ever ran

    def test_renaming_a_step_before_start_is_reflected_in_the_task(self) -> None:
        self._plan()
        researcher = next(t for t in self.coordinator.tasks if t.role == "researcher")
        self.assertTrue(self.coordinator.rename_task(researcher.id, "Look up pricing"))
        self.assertEqual(researcher.title, "Look up pricing")

    def test_renaming_after_start_is_refused(self) -> None:
        self._plan()
        self.coordinator.start_execution()
        researcher = next(t for t in self.coordinator.tasks if t.role == "researcher")
        self.assertFalse(self.coordinator.rename_task(researcher.id, "New title"))

    def test_removing_an_optional_step_drops_it_from_the_plan(self) -> None:
        self.missions.start("compare product A vs product B vs product C and write a report")
        self.coordinator.run("compare product A vs product B vs product C and write a report")
        self.factory.built[0].answer(_plan_json([
            {"role": "researcher", "title": "Research A", "instructions": "a"},
            {"role": "researcher", "title": "Research B", "instructions": "b"},
            {"role": "writer", "title": "Write", "instructions": "write",
             "depends_on": [1, 2]},
        ]))
        removable = next(t for t in self.coordinator.tasks
                         if t.role == "researcher" and t.title == "Research B")
        self.assertTrue(self.coordinator.remove_task(removable.id))
        self.assertNotIn(removable, self.coordinator.tasks)

    def test_the_writer_can_never_be_removed(self) -> None:
        self._plan()
        writer = next(t for t in self.coordinator.tasks if t.role == "writer")
        self.assertFalse(self.coordinator.remove_task(writer.id))

    def test_a_task_something_non_writer_depends_on_cannot_be_removed(self) -> None:
        self.missions.start("compare product A vs product B and analyze the findings")
        self.coordinator.run("compare product A vs product B and analyze the findings")
        self.factory.built[0].answer(_plan_json([
            {"role": "researcher", "title": "Research A", "instructions": "a"},
            {"role": "analyst", "title": "Compare", "instructions": "compare",
             "depends_on": [1]},
            {"role": "writer", "title": "Write", "instructions": "write"},
        ]))
        researcher = next(t for t in self.coordinator.tasks if t.role == "researcher")
        # The Analyst genuinely needs the Researcher's output - removing
        # it would strand a real (non-Writer) requirement, so it is refused.
        self.assertFalse(self.coordinator.remove_task(researcher.id))

    def test_a_task_only_the_writer_depends_on_can_still_be_removed(self) -> None:
        self._plan()
        researcher = next(t for t in self.coordinator.tasks if t.role == "researcher")
        # Only the Writer depends on this one, and the Writer already
        # tolerates a dependency that never ran - "optional" in practice.
        self.assertTrue(self.coordinator.remove_task(researcher.id))

    def test_without_require_plan_approval_execution_starts_immediately(self) -> None:
        """The default (Phase 9 behaviour) is unchanged."""
        missions, db, tmp = _make_missions()
        try:
            missions.start("compare product A vs product B")
            factory = SessionQueueFactory()
            coordinator = MissionCoordinator(missions, factory, CoordinatorLimits())
            coordinator.run("compare product A vs product B")
            factory.built[0].answer(_plan_json([{"role": "writer", "title": "Write",
                                                 "instructions": "write"}]))
            self.assertEqual(len(factory.built), 2)
            self.assertEqual(coordinator.tasks[0].state, WorkerState.RUNNING)
        finally:
            db.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
