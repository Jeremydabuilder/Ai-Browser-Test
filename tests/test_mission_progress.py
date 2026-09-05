"""Mission progress, result and the persisted action log.

Three additions closing the gap between what Py did and what survives a
restart - see the ARCHITECTURE.md section of the same name. The property that
matters here, same as Decision and Ghost Run: `mission_set_progress` and
`mission_save_result` write rows and nothing else - recording an outcome is
never carrying one out.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_mission_progress -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-progress-"))

import app.browser  # noqa: E402,F401

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.missions import MissionService, MissionStore  # noqa: E402
from app.missions.model import (  # noqa: E402
    MAX_ACTION_DESCRIPTION_CHARS,
    MAX_ACTIONS_PER_MISSION,
    MAX_FOLLOW_UP_CHARS,
    MAX_FOLLOW_UPS,
    MAX_PROGRESS_CHARS,
    MAX_RESULT_CHARS,
)
from app.storage.database import Database  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


def _database() -> tuple[Database, str]:
    path = os.path.join(tempfile.mkdtemp(prefix="progress-"), "browser.sqlite3")
    return Database(path), path


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class ProgressStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db, _ = _database()
        self.store = MissionStore(self.db)
        self.mission = self.store.create("Find tennis shoes", "size 8, under $120")

    def tearDown(self) -> None:
        self.db.close()

    def test_setting_progress_is_read_back(self) -> None:
        self.assertTrue(self.store.set_progress(self.mission.id, "Comparing 3 options"))
        self.assertEqual(self.store.get(self.mission.id).progress, "Comparing 3 options")

    def test_progress_defaults_to_empty(self) -> None:
        self.assertEqual(self.store.get(self.mission.id).progress, "")

    def test_an_over_length_label_is_truncated_not_refused(self) -> None:
        label = "x" * (MAX_PROGRESS_CHARS + 50)
        self.assertTrue(self.store.set_progress(self.mission.id, label))
        got = self.store.get(self.mission.id).progress
        self.assertEqual(len(got), MAX_PROGRESS_CHARS)

    def test_setting_progress_touches_updated_at(self) -> None:
        before = self.store.get(self.mission.id).updated_at
        self.store.set_progress(self.mission.id, "Reading reviews")
        self.assertGreaterEqual(self.store.get(self.mission.id).updated_at, before)


class ResultStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db, _ = _database()
        self.store = MissionStore(self.db)
        self.mission = self.store.create("Find tennis shoes", "size 8, under $120")

    def tearDown(self) -> None:
        self.db.close()

    def test_saving_a_result_is_read_back(self) -> None:
        self.assertTrue(self.store.set_result(self.mission.id, "Nike Vapor 12 wins"))
        got = self.store.get(self.mission.id)
        self.assertEqual(got.result, "Nike Vapor 12 wins")
        self.assertTrue(got.has_result)

    def test_a_table_shaped_result_keeps_its_line_breaks(self) -> None:
        # collapse() (used for a finding, a single-line fact) would flatten
        # every newline to a space - fine for a fact, fatal for a table the
        # Mission Library needs to split back into rows.
        table = "Contest | Deadline\n--- | ---\nA | Jan 1\nB | Feb 2"
        self.store.set_result(self.mission.id, table)
        self.assertEqual(self.store.get(self.mission.id).result, table)

    def test_leading_and_trailing_blank_lines_are_trimmed(self) -> None:
        self.store.set_result(self.mission.id, "\n\n  Nike wins  \n\n")
        self.assertEqual(self.store.get(self.mission.id).result, "Nike wins")

    def test_no_result_means_has_result_is_false(self) -> None:
        self.assertFalse(self.store.get(self.mission.id).has_result)

    def test_an_over_length_result_is_refused(self) -> None:
        self.assertFalse(self.store.set_result(self.mission.id, "x" * (MAX_RESULT_CHARS + 1)))
        self.assertEqual(self.store.get(self.mission.id).result, "")

    def test_follow_ups_round_trip(self) -> None:
        self.store.set_result(self.mission.id, "done", follow_ups=["check price next week"])
        self.assertEqual(self.store.get(self.mission.id).follow_ups,
                         ("check price next week",))

    def test_follow_ups_none_leaves_existing_ones_alone(self) -> None:
        self.store.set_result(self.mission.id, "first", follow_ups=["a"])
        self.store.set_result(self.mission.id, "second", follow_ups=None)
        got = self.store.get(self.mission.id)
        self.assertEqual(got.result, "second")
        self.assertEqual(got.follow_ups, ("a",))

    def test_follow_ups_empty_list_clears_them(self) -> None:
        self.store.set_result(self.mission.id, "first", follow_ups=["a"])
        self.store.set_result(self.mission.id, "second", follow_ups=[])
        self.assertEqual(self.store.get(self.mission.id).follow_ups, ())

    def test_too_many_follow_ups_is_refused(self) -> None:
        many = [f"item {n}" for n in range(MAX_FOLLOW_UPS + 1)]
        self.assertFalse(self.store.set_result(self.mission.id, "text", follow_ups=many))

    def test_an_over_length_follow_up_is_refused(self) -> None:
        follow_ups = ["x" * (MAX_FOLLOW_UP_CHARS + 1)]
        self.assertFalse(self.store.set_result(self.mission.id, "text", follow_ups=follow_ups))

    def test_blank_follow_ups_are_dropped_silently(self) -> None:
        self.store.set_result(self.mission.id, "text", follow_ups=["", "   ", "real one"])
        self.assertEqual(self.store.get(self.mission.id).follow_ups, ("real one",))


class ActionLogStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db, _ = _database()
        self.store = MissionStore(self.db)
        self.mission = self.store.create("Find tennis shoes", "size 8, under $120")

    def tearDown(self) -> None:
        self.db.close()

    def test_recording_an_action_is_read_back(self) -> None:
        action = self.store.record_action(self.mission.id, "Searching Google",
                                          tool_name="browser_navigate")
        self.assertIsNotNone(action)
        self.assertEqual(action.description, "Searching Google")
        self.assertEqual(action.tool_name, "browser_navigate")
        self.assertEqual(action.outcome, "done")

    def test_actions_come_back_oldest_first(self) -> None:
        self.store.record_action(self.mission.id, "first")
        self.store.record_action(self.mission.id, "second")
        got = self.store.actions(self.mission.id)
        self.assertEqual([a.description for a in got], ["first", "second"])

    def test_an_empty_description_records_nothing(self) -> None:
        self.assertIsNone(self.store.record_action(self.mission.id, "   "))
        self.assertEqual(self.store.actions(self.mission.id), [])

    def test_an_over_length_description_is_truncated_not_refused(self) -> None:
        action = self.store.record_action(self.mission.id, "x" * (MAX_ACTION_DESCRIPTION_CHARS + 50))
        self.assertEqual(len(action.description), MAX_ACTION_DESCRIPTION_CHARS)

    def test_the_log_is_trimmed_rather_than_refused_once_full(self) -> None:
        for n in range(MAX_ACTIONS_PER_MISSION + 5):
            self.store.record_action(self.mission.id, f"step {n}")
        got = self.store.actions(self.mission.id, limit=MAX_ACTIONS_PER_MISSION + 10)
        self.assertEqual(len(got), MAX_ACTIONS_PER_MISSION)
        # The oldest were dropped, not the newest.
        self.assertEqual(got[-1].description, f"step {MAX_ACTIONS_PER_MISSION + 4}")

    def test_a_mission_read_with_pages_carries_its_actions(self) -> None:
        self.store.record_action(self.mission.id, "Opening a tab")
        got = self.store.get(self.mission.id)
        self.assertEqual(len(got.actions), 1)

    def test_a_mission_read_without_pages_carries_no_actions(self) -> None:
        self.store.record_action(self.mission.id, "Opening a tab")
        got = self.store.get(self.mission.id, with_pages=False)
        self.assertEqual(got.actions, ())


class PersistenceTests(unittest.TestCase):
    def test_progress_result_and_actions_survive_a_restart(self) -> None:
        db, path = _database()
        store = MissionStore(db)
        mission = store.create("Find tennis shoes", "size 8, under $120")
        store.set_progress(mission.id, "Comparing 3 options")
        store.set_result(mission.id, "Nike Vapor 12 wins", follow_ups=["check price"])
        store.record_action(mission.id, "Opening Tennis Warehouse")
        db.close()

        reopened = Database(path)
        try:
            got = MissionStore(reopened).get(mission.id)
            self.assertEqual(got.progress, "Comparing 3 options")
            self.assertEqual(got.result, "Nike Vapor 12 wins")
            self.assertEqual(got.follow_ups, ("check price",))
            self.assertEqual(len(got.actions), 1)
            self.assertEqual(got.actions[0].description, "Opening Tennis Warehouse")
        finally:
            reopened.close()

    def test_a_v10_profile_gains_the_new_columns_and_table(self) -> None:
        import sqlite3

        from app.storage.database import SCHEMA_VERSION

        db, path = _database()
        store = MissionStore(db)
        mission = store.create("Find tennis shoes", "size 8, under $120")
        store.add_finding(mission.id, "A fact")
        db.close()

        conn = sqlite3.connect(path)
        conn.execute("ALTER TABLE missions DROP COLUMN progress")
        conn.execute("ALTER TABLE missions DROP COLUMN result")
        conn.execute("ALTER TABLE missions DROP COLUMN follow_ups")
        conn.execute("DROP TABLE IF EXISTS mission_actions")
        conn.execute("PRAGMA user_version=10")
        conn.commit()
        conn.close()

        upgraded = Database(path)
        try:
            self.assertEqual(upgraded.query("PRAGMA user_version")[0][0], SCHEMA_VERSION)
            store = MissionStore(upgraded)
            got = store.get(mission.id)
            self.assertEqual(got.progress, "")
            self.assertEqual(got.result, "")
            self.assertEqual(len(got.findings), 1)
            self.assertTrue(store.set_progress(mission.id, "still works"))
            self.assertIsNotNone(store.record_action(mission.id, "still works too"))
        finally:
            upgraded.close()


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db, _ = _database()
        self.service = MissionService(MissionStore(self.db))
        self.mission = self.service.start("find tennis shoes")

    def tearDown(self) -> None:
        self.db.close()

    def test_set_progress_with_no_active_mission(self) -> None:
        self.service.leave()
        self.assertEqual(self.service.set_progress("x")["status"], "no_mission")

    def test_set_result_with_no_active_mission(self) -> None:
        self.service.leave()
        self.assertEqual(self.service.set_result("x")["status"], "no_mission")

    def test_set_result_reports_too_long(self) -> None:
        result = self.service.set_result("x" * (MAX_RESULT_CHARS + 1))
        self.assertEqual(result["status"], "too_long")
        self.assertEqual(result["field"], "text")

    def test_set_result_reports_which_field_when_a_follow_up_is_the_problem(self) -> None:
        # A follow-up over length must not be reported as "the result is too
        # long" - that names the wrong thing for the caller to fix.
        result = self.service.set_result("fine", follow_ups=["x" * (MAX_FOLLOW_UP_CHARS + 1)])
        self.assertEqual(result["status"], "too_long")
        self.assertEqual(result["field"], "follow_ups")

    def test_set_result_reports_too_many_follow_ups_distinctly(self) -> None:
        many = [f"item {n}" for n in range(MAX_FOLLOW_UPS + 1)]
        result = self.service.set_result("fine", follow_ups=many)
        self.assertEqual(result["status"], "too_long")
        self.assertEqual(result["field"], "follow_ups")

    def test_set_result_saves_and_refreshes_the_active_mission(self) -> None:
        self.assertEqual(self.service.set_result("Nike wins")["status"], "saved")
        self.assertEqual(self.service.active.result, "Nike wins")

    def test_record_agent_step_ignores_running_state(self) -> None:
        class FakeStep:
            state = "running"
            description = "Searching"
            tool = "browser_navigate"

        self.service.record_agent_step(FakeStep())
        self.assertEqual(self.service.actions(), [])

    def test_record_agent_step_persists_a_done_step(self) -> None:
        class FakeStep:
            state = "done"
            description = "Searching Google"
            tool = "browser_navigate"

        self.service.record_agent_step(FakeStep())
        got = self.service.actions()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].description, "Searching Google")

    def test_record_agent_step_does_nothing_with_no_active_mission(self) -> None:
        self.service.leave()

        class FakeStep:
            state = "done"
            description = "Searching Google"
            tool = ""

        self.service.record_agent_step(FakeStep())  # must not raise
        self.assertEqual(self.service.actions(self.mission.id), [])


# ---------------------------------------------------------------------------
# Blockers: a mission that is waiting on the user says so in its own record,
# not only in the live confirmation prompt.
# ---------------------------------------------------------------------------


class BlockerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db, _ = _database()
        self.service = MissionService(MissionStore(self.db))
        self.mission = self.service.start("find tennis shoes")

    def tearDown(self) -> None:
        self.db.close()

    def test_awaiting_confirmation_sets_a_waiting_progress_label(self) -> None:
        self.service.set_progress("Comparing 3 options")
        self.service.on_agent_state_changed("awaiting_confirmation")
        self.assertEqual(self.service.store.get(self.mission.id).progress,
                         "Waiting for your approval")

    def test_the_previous_progress_is_restored_once_resolved(self) -> None:
        self.service.set_progress("Comparing 3 options")
        self.service.on_agent_state_changed("awaiting_confirmation")
        self.service.on_agent_state_changed("acting")
        self.assertEqual(self.service.store.get(self.mission.id).progress,
                         "Comparing 3 options")

    def test_a_decline_restores_progress_too(self) -> None:
        # The session moves to a different state either way (approve ->
        # acting, decline -> whatever comes next) - restoring must not
        # depend on which one it was.
        self.service.set_progress("Comparing 3 options")
        self.service.on_agent_state_changed("awaiting_confirmation")
        self.service.on_agent_state_changed("thinking")
        self.assertEqual(self.service.store.get(self.mission.id).progress,
                         "Comparing 3 options")

    def test_repeated_awaiting_confirmation_does_not_overwrite_the_saved_label(self) -> None:
        # A second sensitive action while still blocked (should not happen,
        # but must not silently replace "Comparing 3 options" with "Waiting
        # for your approval" as the thing that gets restored).
        self.service.set_progress("Comparing 3 options")
        self.service.on_agent_state_changed("awaiting_confirmation")
        self.service.on_agent_state_changed("awaiting_confirmation")
        self.service.on_agent_state_changed("acting")
        self.assertEqual(self.service.store.get(self.mission.id).progress,
                         "Comparing 3 options")

    def test_ordinary_state_changes_do_not_touch_progress(self) -> None:
        self.service.set_progress("Comparing 3 options")
        self.service.on_agent_state_changed("thinking")
        self.service.on_agent_state_changed("acting")
        self.assertEqual(self.service.store.get(self.mission.id).progress,
                         "Comparing 3 options")

    def test_nothing_happens_with_no_active_mission(self) -> None:
        self.service.leave()
        self.service.on_agent_state_changed("awaiting_confirmation")  # must not raise
        self.assertIsNone(self.service.active)


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.agent.tools import ToolRegistry

        self.db, _ = _database()
        self.service = MissionService(MissionStore(self.db))
        self.mission = self.service.start("find tennis shoes")
        self.tools = ToolRegistry(None, None, self.service)

    def tearDown(self) -> None:
        self.db.close()

    def test_set_progress_tool_updates_the_mission(self) -> None:
        result = self.tools.run("mission_set_progress", {"label": "Comparing 3 options"}).immediate
        self.assertTrue(result["ok"])
        self.assertEqual(self.service.store.get(self.mission.id).progress,
                         "Comparing 3 options")

    def test_save_result_tool_updates_the_mission(self) -> None:
        result = self.tools.run("mission_save_result",
                               {"text": "Nike wins", "follow_ups": ["check price"]}).immediate
        self.assertTrue(result["ok"])
        got = self.service.store.get(self.mission.id)
        self.assertEqual(got.result, "Nike wins")
        self.assertEqual(got.follow_ups, ("check price",))

    def test_save_result_tool_reports_too_long(self) -> None:
        result = self.tools.run("mission_save_result", {"text": "x" * (MAX_RESULT_CHARS + 1)}).immediate
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "TOO_LONG")
        self.assertIn("result is too long", result["error"]["message"])

    def test_save_result_tool_names_a_follow_up_not_the_result(self) -> None:
        # Telling the model "the result is too long" when a follow-up was
        # the actual problem gives it the wrong thing to fix.
        result = self.tools.run(
            "mission_save_result",
            {"text": "fine", "follow_ups": ["x" * (MAX_FOLLOW_UP_CHARS + 1)]}).immediate
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "TOO_LONG")
        self.assertIn("follow-up", result["error"]["message"])
        self.assertNotIn("The result is too long", result["error"]["message"])

    def test_label_is_required(self) -> None:
        from app.agent.tools import ToolError

        with self.assertRaises(ToolError):
            self.tools.run("mission_set_progress", {})

    def test_text_is_required(self) -> None:
        from app.agent.tools import ToolError

        with self.assertRaises(ToolError):
            self.tools.run("mission_save_result", {})

    def test_follow_ups_must_be_a_list_of_strings(self) -> None:
        from app.agent.tools import ToolError

        with self.assertRaises(ToolError):
            self.tools.run("mission_save_result", {"text": "x", "follow_ups": "not a list"})
        with self.assertRaises(ToolError):
            self.tools.run("mission_save_result", {"text": "x", "follow_ups": [1, 2]})

    def test_both_new_tools_are_local_writes_needing_no_confirmation(self) -> None:
        for name, args in (("mission_set_progress", {"label": "x"}),
                          ("mission_save_result", {"text": "x"})):
            decision = self.tools.assess(name, args)
            self.assertFalse(decision["requires_confirmation"])
            self.assertEqual(decision["level"], "normal")

    def test_neither_tool_reaches_the_browser_controller(self) -> None:
        # Structural proof, not a promise: this registry was built with no
        # browser controller at all, and both tools still work - so neither
        # can be reaching into one to perform anything.
        self.assertIsNone(self.tools._browser)
        self.tools.run("mission_set_progress", {"label": "x"})
        self.tools.run("mission_save_result", {"text": "x"})


if __name__ == "__main__":
    unittest.main()
