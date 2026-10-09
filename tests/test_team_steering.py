"""Steering a running mission: instructions apply at task boundaries, are shown when they take
effect, never alter a running task, and (when asked) make dependent finished work redo.

Run with:  QT_QPA_PLATFORM=offscreen python tests/run.py tests.test_team_steering
"""

from __future__ import annotations

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.team.engine import Capabilities, TeamEngine  # noqa: E402
from app.team.limits import TeamLimits  # noqa: E402
from app.team.llm import TeamError  # noqa: E402
from app.team.model import Mission, MissionStatus, Source, SourceKind, TaskStatus  # noqa: E402
from tests.test_team_engine import APPROVE, NO_SANDBOX, RESEARCH_WRITE_REVIEW, FakeClient  # noqa: E402

INSTRUCTION = "Write for a nontechnical reader and keep it under 100 words"


def make(client):
    m = Mission(goal="Compare the widgets", sources=[
        Source("S1", SourceKind.TAB, "Widget A", "https://a.example/widget", "Widget A costs $10."),
        Source("S2", SourceKind.TAB, "Widget B", "https://b.example/widget", "Widget B costs $14.")])
    engine = TeamEngine(m, lambda: client, TeamLimits(max_retries=0, max_backoff_s=1.0, max_concurrency=2),
                        Capabilities(NO_SANDBOX))
    return engine, m


def run_with_steer(gate_role, steer_when_entered, text, redo, script=None):
    gate = threading.Event()
    base = {"plan": [RESEARCH_WRITE_REVIEW], "researcher": ["OLD-NOTES [S1]", "NEW-NOTES [S1]"],
            "writer": ["OLD-DRAFT [S1]", "NEW-DRAFT [S1]"], "reviewer": [APPROVE], "final": ["Final [S1]"]}
    base.update(script or {})
    client = FakeClient(base, hold={gate_role: gate})
    engine, m = make(client)
    thread = threading.Thread(target=engine.run)
    thread.start()
    assert client.entered.setdefault(steer_when_entered, threading.Event()).wait(10)
    item = engine.steer(text, redo)
    gate.set()
    thread.join(30)
    assert not thread.is_alive()
    return engine, m, client, item


class Steering(unittest.TestCase):
    def test_an_instruction_never_alters_a_running_task_and_applies_from_the_next(self) -> None:
        engine, m, client, item = run_with_steer("writer", "writer", INSTRUCTION, False)
        self.assertEqual(m.status, MissionStatus.COMPLETED)
        self.assertEqual(len(client.users("writer")), 1)                    # not restarted
        self.assertNotIn(INSTRUCTION, client.users("writer")[0])             # the running writer never saw it
        self.assertIn(INSTRUCTION, client.users("reviewer")[0])              # the next task did
        self.assertIn("THE USER'S UPDATED INSTRUCTIONS", client.users("reviewer")[0])
        self.assertEqual(m.steering[0]["effective_from"], "T3")
        self.assertEqual(m.task("T2").steering, [])
        self.assertEqual(m.task("T3").steering, [1])
        texts = [e.text for e in m.events]
        self.assertTrue(any("instruction #1" in t and "T2 is already running" in t and "next task starts" in t
                            for t in texts))
        self.assertTrue(any("#1 now applies from T3" in t for t in texts))
        self.assertIn(INSTRUCTION, client.users("final")[-1])
        final = m.artifact(m.final_artifact_id).content
        self.assertIn("Your instruction #1", final)
        self.assertIn("applied from T3", final)
        self.assertIn("work finished before it was not redone", final)

    def test_without_redo_finished_work_is_kept(self) -> None:
        _, m, client, _ = run_with_steer("writer", "writer", INSTRUCTION, False)
        self.assertEqual(client.roles().count("researcher"), 1)
        self.assertEqual(client.roles().count("writer"), 1)
        self.assertFalse([a for a in m.artifacts if a.meta.get("replaced")])

    def test_redo_invalidates_finished_work_and_everything_built_on_it(self) -> None:
        engine, m, client, item = run_with_steer("writer", "writer", INSTRUCTION, True)
        self.assertEqual(m.status, MissionStatus.COMPLETED)
        self.assertEqual(client.roles().count("researcher"), 2)             # research redone under the new rule
        self.assertEqual(client.roles().count("writer"), 2)                 # and the draft built on it
        self.assertIn(INSTRUCTION, client.users("researcher")[1])
        self.assertIn("NEW-NOTES", client.users("writer")[1])
        self.assertNotIn("OLD-NOTES", client.users("writer")[1])
        self.assertNotIn("OLD", client.users("reviewer")[0])                # the review never sees stale work
        self.assertIn("NEW-DRAFT", client.users("reviewer")[0])
        old = [a for a in m.artifacts if "OLD" in a.content]
        self.assertTrue(old and all(a.meta.get("replaced") for a in old))   # history kept, marked replaced
        self.assertTrue(any("you changed the requirements" in e.text for e in m.events))
        self.assertIn("was redone", m.artifact(m.final_artifact_id).content)

    def test_steering_is_refused_when_nothing_is_running(self) -> None:
        engine, m = make(FakeClient({}))
        with self.assertRaises(TeamError) as ctx:
            engine.steer("hello")
        self.assertIn("Ask tab", ctx.exception.message)
        with self.assertRaises(TeamError):
            engine._running = True
            engine.steer("   ")

    def test_old_missions_without_a_requirements_marker_are_not_treated_as_stale(self) -> None:
        client = FakeClient({"plan": [RESEARCH_WRITE_REVIEW], "researcher": ["N [S1]"], "writer": ["D [S1]"],
                             "reviewer": [APPROVE], "final": ["F [S1]"]})
        engine, m = make(client)
        engine.run()
        for task in m.tasks:
            task.upstream.pop("__req", None)                                # as saved by an older version
        self.assertEqual(engine._invalidate_stale(), [])


class SteerBar(unittest.TestCase):
    """The panel's steer bar, against the real controller and engine."""

    def test_the_bar_appears_only_while_working_and_shows_when_each_instruction_applies(self) -> None:
        from tests.test_team_ui import TeamUITestCase, happy_client, pump, setUpModule, wait_for

        class Case(TeamUITestCase):
            def runTest(self):  # pragma: no cover - driven below
                pass

        setUpModule()
        case = Case()
        gate = threading.Event()
        client = happy_client()
        client.hold = {"writer": gate}
        case.build(client)
        try:
            self.assertFalse(case.panel.steer_box.isVisibleTo(case.panel))
            case.start_mission()
            self.assertTrue(wait_for(lambda: "writer" in client.entered))
            pump(10)
            self.assertTrue(case.panel.steer_box.isVisibleTo(case.panel))
            self.assertFalse(case.panel.steer_send.isEnabled())
            case.panel.steer_input.setText(INSTRUCTION)
            pump()
            self.assertTrue(case.panel.steer_send.isEnabled())
            case.panel.steer_send.click()
            self.assertTrue(wait_for(lambda: bool(case.controller.snapshot().steering)))
            pump(10)
            note = case.panel.steer_note.text()
            self.assertIn("#1", note)
            self.assertIn("waits for the next task", note)
            self.assertEqual(case.panel.steer_input.text(), "")
            gate.set()
            self.assertTrue(wait_for(lambda: case.controller.snapshot().status == MissionStatus.COMPLETED))
            pump(10)
            self.assertIn("applies from T3", case.panel.steer_note.text())
            self.assertFalse(case.panel.steer_box.isVisibleTo(case.panel))             # finished: nothing to steer
            self.assertIn("now applies from T3", case.panel.activity.toPlainText())
        finally:
            gate.set()
            case.tearDown()


if __name__ == "__main__":
    unittest.main()
