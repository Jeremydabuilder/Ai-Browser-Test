"""Routines: teaching Py a sequence once, and running it again safely.

The property that matters most is that playback shares its whole execution
path with an ordinary model-issued tool call - assess(), the confirmation
prompt, _execute(). A recorded step that needed the user's approval must need
it again every time the Routine runs; recording is never a way to skip the
gate.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_routines -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-routines-"))

import app.browser  # noqa: E402,F401

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.config import AgentConfig  # noqa: E402
from app.agent.session import AgentSession  # noqa: E402
from app.browser.controller import BrowserController  # noqa: E402
from app.browser.missions_page import summarise  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from app.missions import MissionService, MissionStore  # noqa: E402
from app.routines import RoutineService, RoutineStore  # noqa: E402
from app.routines.model import MAX_STEPS
from app.storage.database import SCHEMA_VERSION, Database  # noqa: E402
from tests.fake_claude import ScriptedClaude, calls, says  # noqa: E402
from tests.fixture_server import FixtureServer  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_profile = None
_server: FixtureServer | None = None


def setUpModule() -> None:
    global _app, _profile, _server
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _profile = shared_profile()
    _server = FixtureServer()


def tearDownModule() -> None:
    if _server is not None:
        _server.stop()
    if _app is not None:
        for _ in range(3):
            _app.processEvents()


def _database() -> tuple[Database, str]:
    path = os.path.join(tempfile.mkdtemp(prefix="routines-"), "browser.sqlite3")
    return Database(path), path


class _Rig:
    def __init__(self) -> None:
        self.db, self.path = _database()
        self.missions = MissionService(MissionStore(self.db))
        self.routines = RoutineService(RoutineStore(self.db))
        self.mission = self.missions.start("teach a routine")

    def close(self) -> None:
        self.db.close()


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


class RecordingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _Rig()

    def tearDown(self) -> None:
        self.rig.close()

    def test_only_browser_tools_are_recorded(self) -> None:
        self.rig.routines.begin_recording(self.rig.mission.id)
        self.rig.routines.record_step("browser_navigate", {"url": "https://a.example"})
        self.rig.routines.record_step("mission_save_finding", {"text": "a fact"})
        routine = self.rig.routines.stop_recording("Routine")
        self.assertEqual([s.tool_name for s in routine.steps], ["browser_navigate"])

    def test_nothing_is_recorded_before_recording_starts(self) -> None:
        self.rig.routines.record_step("browser_navigate", {"url": "https://a.example"})
        self.assertFalse(self.rig.routines.is_recording)

    def test_stopping_with_nothing_recorded_saves_nothing(self) -> None:
        self.rig.routines.begin_recording(self.rig.mission.id)
        self.assertIsNone(self.rig.routines.stop_recording("Empty"))

    def test_discarding_keeps_nothing(self) -> None:
        self.rig.routines.begin_recording(self.rig.mission.id)
        self.rig.routines.record_step("browser_navigate", {"url": "https://a.example"})
        self.rig.routines.discard_recording()
        self.assertEqual(self.rig.routines.for_mission(self.rig.mission.id), [])

    def test_recording_twice_is_refused(self) -> None:
        self.assertTrue(self.rig.routines.begin_recording(self.rig.mission.id))
        self.assertFalse(self.rig.routines.begin_recording(self.rig.mission.id))

    def test_a_routine_needs_a_name(self) -> None:
        self.rig.routines.begin_recording(self.rig.mission.id)
        self.rig.routines.record_step("browser_navigate", {"url": "https://a.example"})
        self.assertIsNone(self.rig.routines.stop_recording("   "))

    def test_steps_are_capped(self) -> None:
        self.rig.routines.begin_recording(self.rig.mission.id)
        for _ in range(MAX_STEPS + 5):
            self.rig.routines.record_step("browser_get_page", {})
        self.assertIsNone(self.rig.routines.stop_recording("Too many"))

    def test_a_routine_belongs_to_the_mission_it_was_taught_on(self) -> None:
        other = self.rig.missions.start("a different goal")
        self.rig.routines.begin_recording(other.id)
        self.rig.routines.record_step("browser_navigate", {"url": "https://a.example"})
        self.rig.routines.stop_recording("Elsewhere")
        self.assertEqual(self.rig.routines.for_mission(self.rig.mission.id), [])
        self.assertEqual(len(self.rig.routines.for_mission(other.id)), 1)


class PersistenceTests(unittest.TestCase):
    def test_a_routine_survives_a_restart(self) -> None:
        rig = _Rig()
        rig.routines.begin_recording(rig.mission.id)
        rig.routines.record_step("browser_navigate", {"url": "https://a.example"},
                                 "Opening a.example")
        rig.routines.record_step("browser_get_page", {})
        rig.routines.stop_recording("Check status")
        mission_id, path = rig.mission.id, rig.path
        rig.close()

        reopened = Database(path)
        try:
            routines = RoutineStore(reopened).for_mission(mission_id)
            self.assertEqual(len(routines), 1)
            self.assertEqual(routines[0].name, "Check status")
            self.assertEqual([s.tool_name for s in routines[0].steps],
                             ["browser_navigate", "browser_get_page"])
            self.assertEqual(routines[0].steps[0].description, "Opening a.example")
        finally:
            reopened.close()

    def test_deleting_a_mission_takes_its_routines(self) -> None:
        rig = _Rig()
        try:
            rig.routines.begin_recording(rig.mission.id)
            rig.routines.record_step("browser_navigate", {"url": "https://a.example"})
            routine = rig.routines.stop_recording("Routine")
            rig.missions.store.delete(rig.mission.id)
            self.assertIsNone(rig.routines.get(routine.id))
        finally:
            rig.close()

    def test_a_v7_profile_gains_routine_tables(self) -> None:
        import sqlite3

        rig = _Rig()
        path = rig.path
        rig.close()
        conn = sqlite3.connect(path)
        for table in ("routine_steps", "routines"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("PRAGMA user_version=7")
        conn.commit()
        conn.close()
        upgraded = Database(path)
        try:
            self.assertEqual(upgraded.query("PRAGMA user_version")[0][0], SCHEMA_VERSION)
        finally:
            upgraded.close()


# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------


class VariableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rig = _Rig()
        self.rig.routines.begin_recording(self.rig.mission.id)
        self.rig.routines.record_step("browser_navigate", {"url": "https://a.example/from"})
        self.rig.routines.record_step("browser_type",
                                      {"ref": "s1:e1", "text": "hello"})
        self.routine = self.rig.routines.stop_recording("With variables")

    def tearDown(self) -> None:
        self.rig.close()

    def test_unchanged_playback_reproduces_what_was_taught(self) -> None:
        self.assertEqual(self.routine.resolve(),
                         [("browser_navigate", {"url": "https://a.example/from"}),
                          ("browser_type", {"ref": "s1:e1", "text": "hello"})])

    def test_overriding_a_slot_changes_only_that_value(self) -> None:
        slot = self.routine.steps[0].slot("url")
        resolved = self.routine.resolve({slot: "https://b.example/to"})
        self.assertEqual(resolved[0][1]["url"], "https://b.example/to")
        self.assertEqual(resolved[1][1]["text"], "hello")

    def test_ref_is_never_offered_as_a_variable(self) -> None:
        # A ref is a coordinate into a page snapshot that may no longer exist,
        # not something a person meant to vary between runs.
        self.assertNotIn("ref", self.routine.steps[1].variable_keys())

    def test_an_override_for_a_ref_is_ignored(self) -> None:
        fake_slot = self.routine.steps[1].slot("ref")
        resolved = self.routine.resolve({fake_slot: "s9:e9"})
        self.assertEqual(resolved[1][1]["ref"], "s1:e1")

    def test_resolving_never_adds_an_argument_that_was_not_recorded(self) -> None:
        resolved = self.routine.resolve({"nonsense-slot": "x"})
        self.assertEqual(set(resolved[0][1]), {"url"})


# ---------------------------------------------------------------------------
# Playback, through the real agent session
# ---------------------------------------------------------------------------


class PlaybackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db, _ = _database()
        self.tabs = TabManager(_profile, "about:blank")
        self.controller = BrowserController(self.tabs)
        self.mission_service = MissionService(MissionStore(self.db), self.controller, self.tabs)
        self.routine_service = RoutineService(RoutineStore(self.db))
        self.mission = self.mission_service.start("teach a routine")
        self.tabs.new_tab(_server.url("index"))
        QTest.qWait(1200)

    def tearDown(self) -> None:
        self.tabs.deleteLater()
        QTest.qWait(10)
        self.db.close()

    def _record_via_agent(self, script) -> "object":
        session = AgentSession(self.controller, ScriptedClaude(script), AgentConfig())
        session.step_recorder = self.routine_service.record_step
        self.routine_service.begin_recording(self.mission.id)
        session.send("do the task")
        for _ in range(400):
            if not session.busy:
                break
            QTest.qWait(15)
        session.shutdown()
        return self.routine_service.stop_recording("Recorded routine")

    def test_recording_captures_the_agents_real_tool_calls(self) -> None:
        routine = self._record_via_agent([
            calls("browser_open_tab", {"url": _server.url("second")}),
            calls("browser_get_page", {}),
            says("done"),
        ])
        self.assertEqual([s.tool_name for s in routine.steps],
                         ["browser_open_tab", "browser_get_page"])

    def test_running_a_routine_performs_the_recorded_actions(self) -> None:
        routine = self._record_via_agent([
            calls("browser_open_tab", {"url": _server.url("second")}),
            says("done"),
        ])
        player = AgentSession(self.controller, ScriptedClaude([]), AgentConfig())
        finished = []
        player.routine_finished.connect(lambda results: finished.append(results))
        started = player.run_routine(routine.resolve())
        self.assertTrue(started)
        for _ in range(400):
            if finished:
                break
            QTest.qWait(15)
        player.shutdown()
        self.assertTrue(finished)
        self.assertEqual(self.tabs.current_tab().url().toString(), _server.url("second"))

    def test_running_with_an_overridden_url_visits_the_new_url(self) -> None:
        routine = self._record_via_agent([
            calls("browser_open_tab", {"url": _server.url("second")}),
            says("done"),
        ])
        slot = routine.steps[0].slot("url")
        player = AgentSession(self.controller, ScriptedClaude([]), AgentConfig())
        finished = []
        player.routine_finished.connect(lambda results: finished.append(results))
        player.run_routine(routine.resolve({slot: _server.url("results")}))
        for _ in range(400):
            if finished:
                break
            QTest.qWait(15)
        player.shutdown()
        self.assertEqual(self.tabs.current_tab().url().toString(), _server.url("results"))

    def test_playback_never_records_a_second_copy_of_itself(self) -> None:
        routine = self._record_via_agent([
            calls("browser_open_tab", {"url": _server.url("second")}),
            says("done"),
        ])
        player = AgentSession(self.controller, ScriptedClaude([]), AgentConfig())
        player.step_recorder = self.routine_service.record_step
        self.routine_service.begin_recording(self.mission.id)
        finished = []
        player.routine_finished.connect(lambda results: finished.append(results))
        player.run_routine(routine.resolve())
        for _ in range(400):
            if finished:
                break
            QTest.qWait(15)
        player.shutdown()
        self.assertEqual(self.routine_service.recorded_count, 0)
        self.routine_service.discard_recording()

    def test_a_flagged_step_still_requires_confirmation_on_replay(self) -> None:
        # The whole safety property: teaching a step once must not exempt it
        # from approval on every future run.
        player = AgentSession(self.controller, ScriptedClaude([]), AgentConfig())
        asked = []
        player.confirmation_required.connect(lambda request: asked.append(request))
        player.run_routine([("browser_navigate",
                             {"url": _server.url("index") + "/file.exe"})])
        QTest.qWait(400)
        self.assertTrue(asked, "a flagged navigation must still ask during playback")
        player.shutdown()

    def test_declining_during_playback_stops_that_step_cleanly(self) -> None:
        player = AgentSession(self.controller, ScriptedClaude([]), AgentConfig())
        finished = []
        player.routine_finished.connect(lambda results: finished.append(results))
        player.run_routine([("browser_navigate",
                             {"url": _server.url("index") + "/file.exe"})])
        for _ in range(200):
            if player.state == "awaiting_confirmation":
                break
            QTest.qWait(15)
        player.resolve_confirmation(False)
        for _ in range(200):
            if finished:
                break
            QTest.qWait(15)
        player.shutdown()
        self.assertTrue(finished)
        self.assertIn("USER_DECLINED", finished[0][0]["content"])

    def test_run_routine_refuses_while_busy(self) -> None:
        session = AgentSession(self.controller, ScriptedClaude([says("ok")]), AgentConfig())
        session.send("hello")
        self.assertFalse(session.run_routine([("browser_get_page", {})]))
        for _ in range(200):
            if not session.busy:
                break
            QTest.qWait(15)
        session.shutdown()

    def test_running_an_empty_step_list_does_nothing(self) -> None:
        session = AgentSession(self.controller, ScriptedClaude([]), AgentConfig())
        self.assertFalse(session.run_routine([]))
        session.shutdown()


# ---------------------------------------------------------------------------
# The mission page
# ---------------------------------------------------------------------------


class PageTests(unittest.TestCase):
    def test_routines_are_listed_on_the_mission_detail_payload(self) -> None:
        rig = _Rig()
        try:
            rig.routines.begin_recording(rig.mission.id)
            rig.routines.record_step("browser_navigate", {"url": "https://a.example"})
            rig.routines.stop_recording("Check status")
            row = summarise(rig.missions.store.get(rig.mission.id), with_detail=True,
                            routines=rig.routines.for_mission(rig.mission.id))
            self.assertEqual(row["routineList"], [{"id": 1, "name": "Check status",
                                                    "steps": 1}])
        finally:
            rig.close()

    def test_no_routines_means_an_empty_list_not_a_missing_key(self) -> None:
        rig = _Rig()
        try:
            row = summarise(rig.missions.store.get(rig.mission.id), with_detail=True)
            self.assertEqual(row["routineList"], [])
        finally:
            rig.close()

    def test_hostile_routine_names_cannot_break_out_of_the_data_block(self) -> None:
        from app.browser.missions_page import LibraryData, render

        rig = _Rig()
        try:
            rig.routines.begin_recording(rig.mission.id)
            rig.routines.record_step("browser_navigate", {"url": "https://a.example"})
            rig.routines.stop_recording("</script><img src=x onerror=alert(1)>")
            row = summarise(rig.missions.store.get(rig.mission.id), with_detail=True,
                            routines=rig.routines.for_mission(rig.mission.id))
            data = LibraryData(detail=row)
            payload = render(data, dark=False).split(
                '<script id="data" type="application/json">')[1].split("</script>")[0]
            self.assertNotIn("</script>", payload)
            self.assertNotIn("<img", payload)
        finally:
            rig.close()


if __name__ == "__main__":
    unittest.main()
