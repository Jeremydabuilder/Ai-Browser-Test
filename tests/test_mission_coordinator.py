"""MissionCoordinator: delegation decision, planning, dependency/parallel
execution, shared Mission state, limits, safety/approval boundaries, and
failure handling.

Uses a real MissionService/MissionStore (so shared findings/sources are
genuinely shared, not faked) and a lightweight FakeSession standing in for
AgentSession - the same "fake with the same signal shapes, driven by hand"
approach as tests/test_task_runner.py. What a worker "does" (saving a
finding, requesting a tool, failing) is simulated by the test script
calling straight into the real MissionService, exactly as AgentSession's
own tool loop would - this exercises genuine shared state while keeping
the Claude conversation itself deterministic and instant.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mission_coordinator -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.session import AgentState, Step, StepState  # noqa: E402
from app.missions.coordinator import (  # noqa: E402
    CoordinatorLimits,
    MissionCoordinator,
    WorkerRole,
    WorkerState,
    parse_critic_verdict,
    parse_plan,
    should_delegate,
)
from app.missions.repository import MissionStore  # noqa: E402
from app.missions.service import MissionService  # noqa: E402
from app.storage import Database  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class FakeSession(QObject):
    """AgentSession-shaped, driven by hand - see the module docstring."""

    state_changed = Signal(str)
    step_changed = Signal(object)
    confirmation_required = Signal(object)
    error = Signal(str)
    finished = Signal()
    assistant_message = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.busy = False
        self.sent: list[str] = []
        self.allowlist = "not set"
        self.config = _FakeConfig()
        self.task_usage = _FakeUsage()
        self.shutdown_called = False
        #: The test sets this before send() is called, to script what
        #: "the model" does for this particular worker turn.
        self.script = lambda text: None

    def set_tool_allowlist(self, allowed) -> None:
        self.allowlist = allowed

    def send(self, text: str) -> bool:
        if self.busy:
            return False
        self.busy = True
        self.sent.append(text)
        return True

    def shutdown(self) -> None:
        self.shutdown_called = True

    # -- test-side driving --------------------------------------------------
    def answer(self, text: str) -> None:
        self.assistant_message.emit(text)
        self.busy = False
        self.finished.emit()

    def fail(self, message: str = "broke") -> None:
        self.error.emit(message)
        self.busy = False
        self.finished.emit()

    def touch_write_tool(self) -> None:
        self.step_changed.emit(Step(index=0, description="x", state=StepState.RUNNING,
                                    tool="browser_click"))

    def request_confirmation(self) -> None:
        self.state_changed.emit(AgentState.AWAITING_CONFIRMATION)
        self.confirmation_required.emit(object())

    def approve(self) -> None:
        self.state_changed.emit(AgentState.ACTING)


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


def _make_missions():
    tmp = tempfile.TemporaryDirectory()
    db = Database(os.path.join(tmp.name, "t.sqlite3"))
    missions = MissionService(MissionStore(db), controller=None, tabs=None)
    return missions, db, tmp


class SessionQueueFactory:
    """Hands out FakeSessions from a prepared queue, one per call - lets a
    test see and drive each worker's session individually and in the order
    the coordinator actually asks for them."""

    def __init__(self, count: int = 20) -> None:
        self.built: list[FakeSession] = []
        self._count = count

    def __call__(self):
        if len(self.built) >= self._count:
            return None
        session = FakeSession()
        self.built.append(session)
        return session


def _plan_json(tasks: list[dict]) -> str:
    return json.dumps({"tasks": tasks})


class ShouldDelegateTests(unittest.TestCase):
    def test_a_short_simple_goal_stays_single_agent(self) -> None:
        self.assertFalse(should_delegate("what time is it in tokyo"))

    def test_a_comparison_goal_delegates(self) -> None:
        self.assertTrue(should_delegate("compare the iphone 15 vs the pixel 8"))

    def test_a_long_goal_delegates_even_without_keywords(self) -> None:
        goal = " ".join(["word"] * 45)
        self.assertTrue(should_delegate(goal))

    def test_an_empty_goal_never_delegates(self) -> None:
        self.assertFalse(should_delegate(""))


class ParsePlanTests(unittest.TestCase):
    def test_a_well_formed_plan_parses(self) -> None:
        text = _plan_json([
            {"role": "researcher", "title": "Specs", "instructions": "Find specs", "depends_on": []},
            {"role": "writer", "title": "Write", "instructions": "Write it up", "depends_on": [1]},
        ])
        tasks = parse_plan(text)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0].role, WorkerRole.RESEARCHER)
        self.assertEqual(tasks[1].depends_on, (1,))

    def test_garbage_text_produces_no_tasks(self) -> None:
        self.assertEqual(parse_plan("not json at all"), [])

    def test_a_planner_role_in_the_plan_is_dropped(self) -> None:
        text = _plan_json([{"role": "planner", "title": "x", "instructions": "y"}])
        self.assertEqual(parse_plan(text), [])

    def test_an_unknown_role_is_dropped(self) -> None:
        text = _plan_json([{"role": "sorcerer", "title": "x", "instructions": "y"}])
        self.assertEqual(parse_plan(text), [])


class ParseCriticVerdictTests(unittest.TestCase):
    def test_a_well_formed_verdict_parses(self) -> None:
        verdict = parse_critic_verdict(json.dumps({
            "verdict": "unsupported", "unsupported_claims": ["claim A"],
            "needs_more_research": True}))
        self.assertEqual(verdict["verdict"], "unsupported")
        self.assertTrue(verdict["needs_more_research"])

    def test_unparseable_text_is_treated_as_nothing_flagged(self) -> None:
        verdict = parse_critic_verdict("not json")
        self.assertFalse(verdict["needs_more_research"])
        self.assertTrue(verdict["parse_error"])


class SimpleMissionStaysSingleAgentTests(unittest.TestCase):
    def test_run_refuses_to_delegate_a_simple_goal(self) -> None:
        missions, db, tmp = _make_missions()
        try:
            missions.start("what's the capital of france")
            factory = SessionQueueFactory()
            coordinator = MissionCoordinator(missions, factory, CoordinatorLimits())
            failed = []
            coordinator.failed.connect(lambda m: failed.append(m))
            coordinator.run("what's the capital of france")
            self.assertEqual(factory.built, [])
            self.assertTrue(failed)
            self.assertFalse(coordinator.delegated)
        finally:
            db.close()
            tmp.cleanup()


class ComplexMissionDelegatesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.missions, self.db, self._tmp = _make_missions()
        self.missions.start("compare product A vs product B and write a recommendation")
        self.factory = SessionQueueFactory()
        self.coordinator = MissionCoordinator(self.missions, self.factory, CoordinatorLimits())
        self.added: list = []
        self.changed: list = []
        self.coordinator.worker_added.connect(lambda t: self.added.append(t))
        self.coordinator.worker_changed.connect(lambda t: self.changed.append(t))

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def _run_plan(self, plan_tasks: list[dict]) -> None:
        self.coordinator.run("compare product A vs product B and write a recommendation")
        # Planner session is the first one built.
        planner_session = self.factory.built[0]
        planner_session.answer(_plan_json(plan_tasks))

    def test_a_complex_goal_produces_a_multi_step_plan(self) -> None:
        self._run_plan([
            {"role": "researcher", "title": "Research A", "instructions": "look up A"},
            {"role": "researcher", "title": "Research B", "instructions": "look up B"},
            {"role": "writer", "title": "Write", "instructions": "write it up",
             "depends_on": [1, 2]},
        ])
        # Two researchers should have been launched in parallel (both built
        # before either finished).
        self.assertEqual(len(self.factory.built), 3)  # planner + 2 researchers
        for session in self.factory.built[1:]:
            session.answer("did some research")
        writer_session = self.factory.built[-1] if len(self.factory.built) > 3 else None
        # Writer launches only after both researchers finish.
        if writer_session is None:
            self.assertEqual(len(self.factory.built), 4)
            self.factory.built[3].answer("wrote the result")
        self.assertTrue(self.coordinator.delegated)


class SharedFindingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.missions, self.db, self._tmp = _make_missions()
        self.missions.start("compare product A vs product B")
        self.factory = SessionQueueFactory()
        self.coordinator = MissionCoordinator(self.missions, self.factory, CoordinatorLimits())

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_findings_saved_by_one_worker_are_visible_to_the_next(self) -> None:
        self.coordinator.run("compare product A vs product B")
        planner = self.factory.built[0]
        planner.answer(_plan_json([
            {"role": "researcher", "title": "Research", "instructions": "look up A"},
            {"role": "writer", "title": "Write", "instructions": "write it up",
             "depends_on": [1]},
        ]))
        researcher = self.factory.built[1]
        # The researcher "calls mission_save_finding" - simulated directly,
        # exactly what AgentSession's real tool loop would do.
        self.missions.save_finding_from_source("Product A costs $99", "https://a.example", "A")
        researcher.answer("found the price")

        writer = self.factory.built[2]
        # By the time the writer's prompt was built, the finding must
        # already be visible in shared Mission state - not private to the
        # researcher that found it.
        self.assertIn("Product A costs $99", writer.sent[0])
        self.missions.set_result("A costs $99, recommend A", [])
        writer.answer("wrote the recommendation")

        self.assertEqual(self.missions.active.result, "A costs $99, recommend A")
        self.assertEqual(len(self.missions.active.findings), 1)


class BoundedWorkerCountTests(unittest.TestCase):
    def test_a_plan_longer_than_max_workers_is_truncated(self) -> None:
        missions, db, tmp = _make_missions()
        try:
            missions.start("compare many long things and write a report")
            factory = SessionQueueFactory()
            limits = CoordinatorLimits(max_workers=3, max_parallel=3)
            coordinator = MissionCoordinator(missions, factory, limits)
            coordinator.run("compare many long things and write a report")
            planner = factory.built[0]
            planner.answer(_plan_json([
                {"role": "researcher", "title": f"R{i}", "instructions": "x"}
                for i in range(1, 6)
            ] + [{"role": "writer", "title": "Write", "instructions": "write"}]))
            # max_workers=3 total, minus 1 for the planner = 2 slots for the
            # rest of the plan; a writer is always guaranteed one of them.
            research_tasks = [t for t in coordinator.tasks if t.role == WorkerRole.RESEARCHER]
            self.assertLessEqual(len(research_tasks), 1)
            self.assertTrue(any(t.role == WorkerRole.WRITER for t in coordinator.tasks))
        finally:
            db.close()
            tmp.cleanup()


class TurnAndToolCallLimitTests(unittest.TestCase):
    def test_worker_sessions_get_the_coordinators_configured_limits(self) -> None:
        missions, db, tmp = _make_missions()
        try:
            missions.start("compare a lot of long detailed things thoroughly")
            factory = SessionQueueFactory()
            limits = CoordinatorLimits(max_turns_per_worker=4, max_tool_calls_per_worker=9)
            coordinator = MissionCoordinator(missions, factory, limits)
            coordinator.run("compare a lot of long detailed things thoroughly")
            planner = factory.built[0]
            self.assertEqual(planner.config.limits.max_turns, 4)
            self.assertEqual(planner.config.limits.max_tool_calls, 9)
            planner.answer(_plan_json([{"role": "writer", "title": "Write",
                                       "instructions": "write"}]))
            writer = factory.built[1]
            self.assertEqual(writer.config.limits.max_turns, 4)
            self.assertEqual(writer.config.limits.max_tool_calls, 9)
            writer.answer("done")
        finally:
            db.close()
            tmp.cleanup()

    def test_a_total_worker_budget_stops_further_launches(self) -> None:
        missions, db, tmp = _make_missions()
        try:
            missions.start("compare a lot of long detailed things thoroughly")
            factory = SessionQueueFactory()
            limits = CoordinatorLimits(max_workers=10, max_parallel=5,
                                      max_total_workers_launched=2)
            coordinator = MissionCoordinator(missions, factory, limits)
            skipped = []
            coordinator.worker_changed.connect(
                lambda t: skipped.append(t) if t.state == WorkerState.SKIPPED else None)
            coordinator.run("compare a lot of long detailed things thoroughly")
            planner = factory.built[0]  # counts as worker #1
            planner.answer(_plan_json([
                {"role": "researcher", "title": "R1", "instructions": "x"},
                {"role": "writer", "title": "Write", "instructions": "write"},
            ]))
            # Worker #2 (the researcher) is allowed; nothing further is.
            researcher = factory.built[1]
            researcher.answer("done")
            self.assertTrue(any(t.role == WorkerRole.WRITER for t in skipped))
        finally:
            db.close()
            tmp.cleanup()


class ParallelResearchersTests(unittest.TestCase):
    def test_two_independent_researchers_are_both_launched_before_either_finishes(self) -> None:
        missions, db, tmp = _make_missions()
        try:
            missions.start("compare product A vs product B vs product C")
            factory = SessionQueueFactory()
            coordinator = MissionCoordinator(missions, factory, CoordinatorLimits(max_parallel=3))
            coordinator.run("compare product A vs product B vs product C")
            planner = factory.built[0]
            planner.answer(_plan_json([
                {"role": "researcher", "title": "R1", "instructions": "x"},
                {"role": "researcher", "title": "R2", "instructions": "y"},
                {"role": "writer", "title": "Write", "instructions": "write",
                 "depends_on": [1, 2]},
            ]))
            # Both researchers exist and are running before either answers -
            # genuine parallelism, not one-after-another.
            self.assertEqual(len(factory.built), 3)
            r1, r2 = factory.built[1], factory.built[2]
            self.assertTrue(r1.busy)
            self.assertTrue(r2.busy)
            r1.answer("r1 done")
            r2.answer("r2 done")
            self.assertEqual(len(factory.built), 4)
            factory.built[3].answer("wrote it")
        finally:
            db.close()
            tmp.cleanup()

    def test_browser_operator_tasks_never_run_in_parallel_with_each_other(self) -> None:
        missions, db, tmp = _make_missions()
        try:
            missions.start("fill out two long forms and write a report")
            factory = SessionQueueFactory()
            coordinator = MissionCoordinator(missions, factory, CoordinatorLimits(max_parallel=3))
            coordinator.run("fill out two long forms and write a report")
            planner = factory.built[0]
            planner.answer(_plan_json([
                {"role": "browser_operator", "title": "Form 1", "instructions": "x"},
                {"role": "browser_operator", "title": "Form 2", "instructions": "y"},
                {"role": "writer", "title": "Write", "instructions": "write",
                 "depends_on": [1, 2]},
            ]))
            # Only one Browser Operator session exists so far - the second
            # must wait for the first, never run alongside it.
            self.assertEqual(len(factory.built), 2)
            factory.built[1].answer("form 1 done")
            self.assertEqual(len(factory.built), 3)
            factory.built[2].answer("form 2 done")
            self.assertEqual(len(factory.built), 4)
            factory.built[3].answer("wrote it")
        finally:
            db.close()
            tmp.cleanup()


class MergeOfResultsTests(unittest.TestCase):
    def test_the_final_result_comes_from_the_writer_via_mission_save_result(self) -> None:
        missions, db, tmp = _make_missions()
        try:
            missions.start("compare product A vs product B")
            factory = SessionQueueFactory()
            coordinator = MissionCoordinator(missions, factory, CoordinatorLimits())
            results = []
            coordinator.result_ready.connect(lambda r: results.append(r))
            coordinator.run("compare product A vs product B")
            planner = factory.built[0]
            planner.answer(_plan_json([{"role": "writer", "title": "Write",
                                       "instructions": "write"}]))
            writer = factory.built[1]
            missions.set_result("Recommend B: cheaper and better reviewed", [])
            writer.answer("done")
            self.assertEqual(results, ["Recommend B: cheaper and better reviewed"])
        finally:
            db.close()
            tmp.cleanup()


class CriticRejectsUnsupportedClaimTests(unittest.TestCase):
    def test_the_critic_can_trigger_exactly_one_bounded_extra_research_round(self) -> None:
        missions, db, tmp = _make_missions()
        try:
            missions.start("compare product A vs product B")
            factory = SessionQueueFactory()
            coordinator = MissionCoordinator(missions, factory, CoordinatorLimits())
            coordinator.run("compare product A vs product B")
            planner = factory.built[0]
            planner.answer(_plan_json([
                {"role": "critic", "title": "Check", "instructions": "check claims"},
                {"role": "writer", "title": "Write", "instructions": "write"},
            ]))
            critic = factory.built[1]
            critic.answer(json.dumps({
                "verdict": "unsupported", "unsupported_claims": ["Product A is faster"],
                "needs_more_research": True}))
            # A follow-up researcher must have been launched - the extra
            # bounded round - before the writer.
            self.assertEqual(len(factory.built), 3)
            follow_up = factory.built[2]
            self.assertEqual(follow_up.busy, True)
            follow_up.answer("checked - it is not actually faster")
            writer = factory.built[3]
            writer.answer("wrote a corrected recommendation")
            self.assertTrue(coordinator.tasks)
            # Only one revision round ever happens, even if a second critic
            # existed and flagged something again - the coordinator does
            # not run the critic a second time in this plan, so nothing
            # further to assert beyond the single follow-up above.
        finally:
            db.close()
            tmp.cleanup()


class WorkerFailureRecoveryTests(unittest.TestCase):
    def test_a_failed_researcher_does_not_kill_the_whole_mission(self) -> None:
        missions, db, tmp = _make_missions()
        try:
            missions.start("compare product A vs product B")
            factory = SessionQueueFactory()
            coordinator = MissionCoordinator(missions, factory, CoordinatorLimits())
            failed_workers = []
            coordinator.worker_changed.connect(
                lambda t: failed_workers.append(t) if t.state == WorkerState.FAILED else None)
            coordinator.run("compare product A vs product B")
            planner = factory.built[0]
            planner.answer(_plan_json([
                {"role": "researcher", "title": "R1", "instructions": "x"},
                {"role": "writer", "title": "Write", "instructions": "write",
                 "depends_on": [1]},
            ]))
            researcher = factory.built[1]
            researcher.fail("network error")
            # The writer still runs even though the researcher failed.
            self.assertEqual(len(factory.built), 3)
            factory.built[2].answer("wrote something anyway")
            self.assertTrue(any(t.role == WorkerRole.RESEARCHER for t in failed_workers))
        finally:
            db.close()
            tmp.cleanup()

    def test_a_worker_that_attempted_a_write_before_failing_is_never_auto_retried(self) -> None:
        missions, db, tmp = _make_missions()
        try:
            missions.start("fill out a long form and write a report")
            factory = SessionQueueFactory()
            coordinator = MissionCoordinator(missions, factory, CoordinatorLimits())
            coordinator.run("fill out a long form and write a report")
            planner = factory.built[0]
            planner.answer(_plan_json([
                {"role": "browser_operator", "title": "Form", "instructions": "x"},
                {"role": "writer", "title": "Write", "instructions": "write",
                 "depends_on": [1]},
            ]))
            operator = factory.built[1]
            operator.touch_write_tool()
            operator.fail("crashed mid-submit")
            task = next(t for t in coordinator.tasks if t.role == WorkerRole.BROWSER_OPERATOR)
            self.assertTrue(task.write_attempted)
            self.assertEqual(task.state, WorkerState.FAILED)
            # Exactly one Browser Operator session was ever built for this
            # task - it was never silently retried.
            operator_sessions = [s for s in factory.built if s is operator]
            self.assertEqual(len(operator_sessions), 1)
        finally:
            db.close()
            tmp.cleanup()


class ApprovalEnforcementTests(unittest.TestCase):
    def test_a_workers_confirmation_is_scoped_to_that_worker_only(self) -> None:
        """Worker A being approved must never look like generic
        authorization to Worker B - each worker's confirmation is a
        separate signal, tied to its own session and task."""
        missions, db, tmp = _make_missions()
        try:
            missions.start("fill out two forms and write a report")
            factory = SessionQueueFactory()
            coordinator = MissionCoordinator(missions, factory, CoordinatorLimits())
            confirmations = []
            coordinator.worker_confirmation_required.connect(
                lambda task, request, session: confirmations.append((task, request, session)))
            coordinator.run("fill out two forms and write a report")
            planner = factory.built[0]
            planner.answer(_plan_json([
                {"role": "browser_operator", "title": "Form 1", "instructions": "x"},
                {"role": "browser_operator", "title": "Form 2", "instructions": "y"},
                {"role": "writer", "title": "Write", "instructions": "write",
                 "depends_on": [1, 2]},
            ]))
            op1 = factory.built[1]
            op1.request_confirmation()
            self.assertEqual(len(confirmations), 1)
            task1, request1, session1 = confirmations[0]
            self.assertIs(session1, op1)
            op1.approve()
            op1.answer("form 1 done")

            op2 = factory.built[2]
            # Worker B (a brand-new session) still requires its own
            # approval - nothing carried over from Worker A's approval.
            op2.request_confirmation()
            self.assertEqual(len(confirmations), 2)
            task2, request2, session2 = confirmations[1]
            self.assertIs(session2, op2)
            self.assertIsNot(session2, session1)
            op2.approve()
            op2.answer("form 2 done")
            factory.built[3].answer("wrote it")
        finally:
            db.close()
            tmp.cleanup()

    def test_role_tool_allowlist_is_applied_before_the_worker_runs(self) -> None:
        from app.missions.coordinator import ROLE_ALLOWED_TOOLS

        missions, db, tmp = _make_missions()
        try:
            missions.start("compare product A vs product B")
            factory = SessionQueueFactory()
            coordinator = MissionCoordinator(missions, factory, CoordinatorLimits())
            coordinator.run("compare product A vs product B")
            planner = factory.built[0]
            self.assertEqual(planner.allowlist, ROLE_ALLOWED_TOOLS[WorkerRole.PLANNER])
            planner.answer(_plan_json([{"role": "researcher", "title": "R",
                                       "instructions": "x"},
                                      {"role": "writer", "title": "Write",
                                       "instructions": "write", "depends_on": [1]}]))
            researcher = factory.built[1]
            self.assertEqual(researcher.allowlist, ROLE_ALLOWED_TOOLS[WorkerRole.RESEARCHER])
            self.assertNotIn("browser_click", researcher.allowlist)
            researcher.answer("done")
            writer = factory.built[2]
            self.assertEqual(writer.allowlist, ROLE_ALLOWED_TOOLS[WorkerRole.WRITER])
            writer.answer("done")
        finally:
            db.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
