"""Phase 10: the Mission Execution Graph as MissionCoordinator persists and
executes it - DAG ordering already covered generically by
tests/test_mission_coordinator.py's dependency/parallel tests; this file
covers what is new: persistence, restart recovery, retry/skip/cancel,
graph-size limits, and role-scoped MCP access.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mission_graph_coordinator -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.missions.coordinator import (  # noqa: E402
    CoordinatorLimits,
    MissionCoordinator,
    WorkerRole,
    WorkerState,
    mcp_tools_for_role,
)
from app.missions.graph import NodeState  # noqa: E402
from app.missions.repository import MissionStore  # noqa: E402
from app.missions.service import MissionService  # noqa: E402
from app.storage import Database, MissionGraphStore  # noqa: E402
from app.storage.mission_graph import recover_after_restart  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class FakeSession(QObject):
    state_changed = Signal(str)
    step_changed = Signal(object)
    confirmation_required = Signal(object)
    error = Signal(str)
    finished = Signal()
    assistant_message = Signal(str)

    def __init__(self, mcp=None) -> None:
        super().__init__()
        self.busy = False
        self.sent: list[str] = []
        self.allowlist = "not set"
        self.config = _FakeConfig()
        self.task_usage = _FakeUsage()
        self._mcp = mcp

    def set_tool_allowlist(self, allowed) -> None:
        self.allowlist = allowed

    def send(self, text: str) -> bool:
        if self.busy:
            return False
        self.busy = True
        self.sent.append(text)
        return True

    def shutdown(self) -> None:
        pass

    def answer(self, text: str) -> None:
        self.assistant_message.emit(text)
        self.busy = False
        self.finished.emit()

    def fail(self, message: str = "broke") -> None:
        self.error.emit(message)
        self.busy = False
        self.finished.emit()


class _FakeConfig:
    def __init__(self) -> None:
        self.limits = _FakeLimits()


class _FakeLimits:
    def __init__(self) -> None:
        self.max_turns = 25
        self.max_tool_calls = 40


class _FakeUsage:
    def __init__(self) -> None:
        self.input_tokens = 10
        self.output_tokens = 5


def _make_missions_and_graph():
    tmp = tempfile.TemporaryDirectory()
    db = Database(os.path.join(tmp.name, "t.sqlite3"))
    missions = MissionService(MissionStore(db), controller=None, tabs=None)
    graph_store = MissionGraphStore(db)
    return missions, graph_store, db, tmp


class SessionQueueFactory:
    def __init__(self, count: int = 20, mcp=None) -> None:
        self.built: list[FakeSession] = []
        self._count = count
        self._mcp = mcp

    def __call__(self):
        if len(self.built) >= self._count:
            return None
        session = FakeSession(mcp=self._mcp)
        self.built.append(session)
        return session


def _plan_json(tasks: list[dict]) -> str:
    return json.dumps({"tasks": tasks})


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.missions, self.graph_store, self.db, self._tmp = _make_missions_and_graph()
        self.missions.start("compare product A vs product B and write a recommendation")
        self.mission_id = self.missions.active.id
        self.factory = SessionQueueFactory()
        self.coordinator = MissionCoordinator(
            self.missions, self.factory, CoordinatorLimits(), graph_store=self.graph_store)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_every_planned_task_becomes_a_persisted_node(self) -> None:
        self.coordinator.run("compare product A vs product B and write a recommendation")
        planner = self.factory.built[0]
        planner.answer(_plan_json([
            {"role": "researcher", "title": "Research A", "instructions": "look up A"},
            {"role": "writer", "title": "Write", "instructions": "write it up",
             "depends_on": [1]},
        ]))
        nodes = self.graph_store.nodes_for_mission(self.mission_id)
        self.assertEqual(len(nodes), 2)
        self.assertEqual({n.role for n in nodes}, {"researcher", "writer"})

    def test_dependencies_persist_as_real_node_ids(self) -> None:
        self.coordinator.run("compare product A vs product B and write a recommendation")
        planner = self.factory.built[0]
        planner.answer(_plan_json([
            {"role": "researcher", "title": "Research A", "instructions": "look up A"},
            {"role": "writer", "title": "Write", "instructions": "write it up",
             "depends_on": [1]},
        ]))
        writer_node = next(n for n in self.graph_store.nodes_for_mission(self.mission_id)
                           if n.role == "writer")
        researcher_node = next(n for n in self.graph_store.nodes_for_mission(self.mission_id)
                               if n.role == "researcher")
        self.assertIn(researcher_node.id, writer_node.dependencies)

    def test_node_state_tracks_the_live_task_through_the_run(self) -> None:
        self.coordinator.run("compare product A vs product B and write a recommendation")
        planner = self.factory.built[0]
        planner.answer(_plan_json([{"role": "writer", "title": "Write", "instructions": "write"}]))
        writer_node_id = self.graph_store.nodes_for_mission(self.mission_id)[0].id
        self.assertEqual(self.graph_store.get(writer_node_id).state, NodeState.RUNNING)
        self.missions.set_result("done", [])
        self.factory.built[1].answer("wrote it")
        self.assertEqual(self.graph_store.get(writer_node_id).state, NodeState.COMPLETED)

    def test_write_attempted_is_persisted_the_moment_a_write_tool_runs(self) -> None:
        from app.agent.session import Step, StepState

        self.coordinator.run("fill out a form and write a report")
        planner = self.factory.built[0]
        planner.answer(_plan_json([
            {"role": "browser_operator", "title": "Form", "instructions": "x"},
            {"role": "writer", "title": "Write", "instructions": "write", "depends_on": [1]},
        ]))
        operator = self.factory.built[1]
        operator.step_changed.emit(Step(index=0, description="x", state=StepState.RUNNING,
                                        tool="browser_click"))
        node_id = next(n.id for n in self.graph_store.nodes_for_mission(self.mission_id)
                      if n.role == "browser_operator")
        self.assertTrue(self.graph_store.get(node_id).write_attempted)


class RestartRecoveryIntoCoordinatorTests(unittest.TestCase):
    """A crash mid-run, and what the *next* coordinator sees when it looks
    at that Mission's graph - never automatic re-execution, only correct
    stored state, per the phase's own PERSISTENCE section."""

    def test_a_stranded_write_node_is_needs_review_and_never_auto_retried(self) -> None:
        missions, graph_store, db, tmp = _make_missions_and_graph()
        try:
            missions.start("fill out a form and write a report")
            mission_id = missions.active.id
            node = graph_store.create_node(
                mission_id, node_type="browse", role="browser_operator",
                title="Form", instructions="x")
            graph_store.record_start(node.id)
            graph_store.set_write_attempted(node.id, True)

            recover_after_restart(graph_store)
            self.assertEqual(graph_store.get(node.id).state, "needs_review")

            # A fresh coordinator over the same Mission must not silently
            # resume/replay this node - it is not even part of its live
            # task list until something explicit (a UI retry) brings it
            # back, which this test does not do.
            factory = SessionQueueFactory()
            coordinator = MissionCoordinator(missions, factory, CoordinatorLimits(),
                                            graph_store=graph_store)
            self.assertEqual(coordinator.tasks, [])
            self.assertEqual(factory.built, [])
        finally:
            db.close()
            tmp.cleanup()

    def test_a_stranded_read_only_node_is_safe_to_resume(self) -> None:
        missions, graph_store, db, tmp = _make_missions_and_graph()
        try:
            missions.start("research something and write a report")
            mission_id = missions.active.id
            node = graph_store.create_node(
                mission_id, node_type="research", role="researcher",
                title="Research", instructions="x")
            graph_store.record_start(node.id)
            recover_after_restart(graph_store)
            self.assertEqual(graph_store.get(node.id).state, "pending")
        finally:
            db.close()
            tmp.cleanup()


class RetrySkipCancelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.missions, self.graph_store, self.db, self._tmp = _make_missions_and_graph()
        self.missions.start("compare product A vs product B and write a recommendation")
        self.factory = SessionQueueFactory()
        self.coordinator = MissionCoordinator(
            self.missions, self.factory, CoordinatorLimits(), graph_store=self.graph_store)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_retry_relaunches_a_failed_node_and_bounds_by_max_retries(self) -> None:
        # A plan with only a Researcher still gets an automatic Writer
        # (a Mission is always guaranteed one) that may launch on its own
        # once the Researcher reaches ANY terminal state - so sessions are
        # tracked by count/delta here, not by "whichever session is last".
        self.coordinator.run("compare product A vs product B and write a recommendation")
        planner = self.factory.built[0]
        planner.answer(_plan_json([{"role": "researcher", "title": "R", "instructions": "x"}]))
        researcher_task = next(t for t in self.coordinator.tasks if t.role == "researcher")
        researcher_session_index = 1   # planner is [0]; the researcher is built next
        self.factory.built[researcher_session_index].fail("network blip")
        self.assertEqual(researcher_task.state, WorkerState.FAILED)
        self.assertEqual(researcher_task.attempt_count, 1)

        # max_retries defaults to 2: the initial attempt plus two retries,
        # three launches in total, before a further retry is refused.
        before = len(self.factory.built)
        self.assertTrue(self.coordinator.retry_node(researcher_task.id))
        self.assertEqual(researcher_task.state, WorkerState.RUNNING)
        self.assertEqual(researcher_task.attempt_count, 2)
        retry_session = self.factory.built[before]   # the new session retry_node just built
        retry_session.fail("still broken")
        self.assertEqual(researcher_task.state, WorkerState.FAILED)

        before = len(self.factory.built)
        self.assertTrue(self.coordinator.retry_node(researcher_task.id))
        self.assertEqual(researcher_task.attempt_count, 3)
        self.factory.built[before].fail("broken again")

        # A fourth launch would exceed max_retries=2 - refused.
        self.assertFalse(self.coordinator.retry_node(researcher_task.id))
        self.assertEqual(researcher_task.attempt_count, 3)

    def test_skip_lets_a_blocked_run_continue_without_retrying(self) -> None:
        self.coordinator.run("compare product A vs product B and write a recommendation")
        planner = self.factory.built[0]
        planner.answer(_plan_json([{"role": "researcher", "title": "R", "instructions": "x"},
                                  {"role": "writer", "title": "Write", "instructions": "write",
                                   "depends_on": [1]}]))
        researcher_task = next(t for t in self.coordinator.tasks if t.role == "researcher")
        self.factory.built[1].fail("broke")
        # Already terminal (FAILED) - skip_node on a terminal task is a
        # deliberate no-op refusal, not an error.
        self.assertFalse(self.coordinator.skip_node(researcher_task.id))

    def test_skip_refuses_a_running_node(self) -> None:
        self.coordinator.run("compare product A vs product B and write a recommendation")
        planner = self.factory.built[0]
        planner.answer(_plan_json([{"role": "writer", "title": "Write", "instructions": "write"}]))
        writer_task = self.coordinator.tasks[0]
        self.assertFalse(self.coordinator.skip_node(writer_task.id))

    def test_cancel_marks_every_non_terminal_task_cancelled(self) -> None:
        self.coordinator.run("compare product A vs product B and write a recommendation")
        planner = self.factory.built[0]
        planner.answer(_plan_json([
            {"role": "researcher", "title": "R1", "instructions": "x"},
            {"role": "researcher", "title": "R2", "instructions": "y"},
            {"role": "writer", "title": "Write", "instructions": "write",
             "depends_on": [1, 2]},
        ]))
        self.coordinator.cancel()
        states = {t.role: t.state for t in self.coordinator.tasks}
        self.assertEqual(states["writer"], WorkerState.CANCELLED)
        for role in ("researcher",):
            self.assertTrue(all(t.state == WorkerState.CANCELLED
                                for t in self.coordinator.tasks if t.role == role))
        for node in self.graph_store.nodes_for_mission(self.missions.active.id):
            self.assertEqual(node.state, "cancelled")


class GraphSizeLimitTests(unittest.TestCase):
    def test_the_graph_never_grows_past_max_graph_nodes(self) -> None:
        missions, graph_store, db, tmp = _make_missions_and_graph()
        try:
            missions.start("compare many long things and write a thorough report")
            factory = SessionQueueFactory()
            limits = CoordinatorLimits(max_workers=10, max_graph_nodes=3)
            coordinator = MissionCoordinator(missions, factory, limits, graph_store=graph_store)
            coordinator.run("compare many long things and write a thorough report")
            planner = factory.built[0]
            planner.answer(_plan_json([
                {"role": "researcher", "title": f"R{i}", "instructions": "x"}
                for i in range(1, 6)
            ] + [{"role": "writer", "title": "Write", "instructions": "write"}]))
            self.assertLessEqual(
                graph_store.count_for_mission(missions.active.id), limits.max_graph_nodes)
        finally:
            db.close()
            tmp.cleanup()

    def test_a_critic_revision_round_is_refused_once_the_graph_is_full(self) -> None:
        missions, graph_store, db, tmp = _make_missions_and_graph()
        try:
            missions.start("compare product A vs product B")
            factory = SessionQueueFactory()
            # planner(not persisted as a node) + critic + writer == 2 nodes;
            # cap at 2 so there is no room for a Critic-requested follow-up.
            limits = CoordinatorLimits(max_graph_nodes=2)
            coordinator = MissionCoordinator(missions, factory, limits, graph_store=graph_store)
            coordinator.run("compare product A vs product B")
            planner = factory.built[0]
            planner.answer(_plan_json([
                {"role": "critic", "title": "Check", "instructions": "check"},
                {"role": "writer", "title": "Write", "instructions": "write"},
            ]))
            critic = factory.built[1]
            critic.answer(json.dumps({"verdict": "unsupported",
                                     "unsupported_claims": ["x"],
                                     "needs_more_research": True}))
            # No follow-up research node was created - the graph was full.
            self.assertEqual(
                graph_store.count_for_mission(missions.active.id), limits.max_graph_nodes)
            factory.built[2].answer("wrote it anyway")
        finally:
            db.close()
            tmp.cleanup()


class RoleScopedMcpAccessTests(unittest.TestCase):
    """Never all MCP tools just because a Mission is multi-agent - see
    mcp_tools_for_role's own docstring."""

    def test_researcher_gets_only_read_only_mcp_tools(self) -> None:
        from app.mcp.types import Sensitivity

        class FakeTool:
            def __init__(self, sensitivity):
                self.sensitivity = sensitivity

        class FakeMcp:
            def schemas(self):
                return [{"name": "srv.read_thing"}, {"name": "srv.write_thing"}]

            def find_tool(self, server_id, tool_name):
                return (FakeTool(Sensitivity.READ_ONLY) if tool_name == "read_thing"
                       else FakeTool(Sensitivity.WRITE))

        from unittest import mock

        with mock.patch("app.mcp.adapter.split_namespaced",
                        side_effect=lambda n: tuple(n.split(".", 1))):
            allowed = mcp_tools_for_role(WorkerRole.RESEARCHER, FakeMcp())
        self.assertIn("srv.read_thing", allowed)
        self.assertNotIn("srv.write_thing", allowed)

    def test_analyst_and_writer_get_no_mcp_tools_at_all(self) -> None:
        class FakeMcp:
            def schemas(self):
                return [{"name": "srv.anything"}]

        self.assertEqual(mcp_tools_for_role(WorkerRole.ANALYST, FakeMcp()), frozenset())
        self.assertEqual(mcp_tools_for_role(WorkerRole.WRITER, FakeMcp()), frozenset())
        self.assertEqual(mcp_tools_for_role(WorkerRole.CRITIC, FakeMcp()), frozenset())

    def test_no_mcp_manager_means_no_mcp_tools(self) -> None:
        self.assertEqual(mcp_tools_for_role(WorkerRole.RESEARCHER, None), frozenset())

    def test_browser_operator_sees_every_currently_exposed_mcp_tool(self) -> None:
        """Visibility only - McpConnectionManager.assess_call still gates
        every actual call with Allow/Ask/Deny, unchanged."""
        class FakeMcp:
            def schemas(self):
                return [{"name": "srv.a"}, {"name": "srv.b"}]

        allowed = mcp_tools_for_role(WorkerRole.BROWSER_OPERATOR, FakeMcp())
        self.assertEqual(allowed, frozenset({"srv.a", "srv.b"}))


if __name__ == "__main__":
    unittest.main()
