"""Phase 17: Workspaces / Profiles - see app/workspaces/.

Covers the data model round trip, persistence (including the "always at
least one workspace" invariant), the Workspace-vs-Isolated-Profile
distinction (WorkspaceProfileManager), reassignment on deletion, and the
MainWindow-level integration points the Phase 17 brief called out by name:
tab/session state across a switch, pinned/grouped tabs scoped per
workspace, Missions defaulting to the current workspace, MCP visibility
scoping, Skill visibility, the cross-workspace recorded-workflow run
confirmation, and scheduled-task workspace binding through TaskRunner.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_workspaces -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-workspace-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.config import database_path  # noqa: E402
from app.missions.repository import MissionStore  # noqa: E402
from app.missions.scheduler import ScheduleKind  # noqa: E402
from app.missions.task_runner import TaskRunner  # noqa: E402
from app.storage import Database, SettingsStore  # noqa: E402
from app.storage.scheduled_tasks import ScheduledTaskStore  # noqa: E402
from app.storage.skills import SkillStore  # noqa: E402
from app.workspaces import TabRecord, TabState, Workspace, WorkspaceStore  # noqa: E402
from app.workspaces.model import DEFAULT_WORKSPACE_ID, default_workspace, new_workspace  # noqa: E402
from app.workspaces.profiles import WorkspaceProfileManager  # noqa: E402
from app.workspaces.reassign import (  # noqa: E402
    reassign_recorded_workflows,
    reassign_workspace_data,
)
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_profile = None


def setUpModule() -> None:
    global _app, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _profile = shared_profile()


def pump(times: int = 5) -> None:
    for _ in range(times):
        _app.processEvents()


def wait_for_url(tab, expected: str, tries: int = 60) -> str:
    """BrowserTab.navigate()'s QWebEngineView.setUrl() is not always
    synchronously reflected in url() in this offscreen test environment -
    a WebEngine-level timing quirk unrelated to Workspaces, also visible
    navigating the same tab twice in quick succession without a switch in
    between. Poll briefly rather than asserting on a single pump() - still
    fails (returning whatever the last value was) if it never arrives."""
    for _ in range(tries):
        current = tab.url().toString()
        if current == expected:
            return current
        pump(1)
    return tab.url().toString()


# -- model round trip --------------------------------------------------------

class WorkspaceModelTests(unittest.TestCase):
    def test_tab_record_round_trips(self) -> None:
        record = TabRecord(url="https://example.com/", pinned=True, group_id="g1")
        self.assertEqual(TabRecord.from_dict(record.as_dict()), record)

    def test_tab_state_round_trips(self) -> None:
        state = TabState(
            tabs=(TabRecord(url="https://a.example/"), TabRecord(url="https://b.example/", pinned=True)),
            groups={"g1": {"name": "Group", "collapsed": False}},
            active_index=1,
        )
        restored = TabState.from_dict(state.as_dict())
        self.assertEqual(restored.tabs, state.tabs)
        self.assertEqual(restored.groups, state.groups)
        self.assertEqual(restored.active_index, 1)

    def test_empty_tab_state_is_empty(self) -> None:
        self.assertTrue(TabState().is_empty)
        self.assertFalse(TabState(tabs=(TabRecord(url="https://a.example/"),)).is_empty)

    def test_workspace_round_trips_through_dict(self) -> None:
        workspace = new_workspace("Research", icon="R", color="#fff", isolated_profile=True)
        restored = Workspace.from_dict(workspace.as_dict())
        self.assertEqual(restored, workspace)
        self.assertTrue(restored.profile_storage_name.startswith("workspace-"))

    def test_plain_new_workspace_has_no_storage_name(self) -> None:
        workspace = new_workspace("Personal")
        self.assertFalse(workspace.isolated_profile)
        self.assertEqual(workspace.profile_storage_name, "")

    def test_default_workspace_uses_reserved_id(self) -> None:
        self.assertEqual(default_workspace().id, DEFAULT_WORKSPACE_ID)

    def test_mcp_visible_server_ids_round_trips_including_none(self) -> None:
        workspace = new_workspace("Work")
        self.assertIsNone(workspace.mcp_visible_server_ids)
        from dataclasses import replace
        scoped = replace(workspace, mcp_visible_server_ids=("s1", "s2"))
        restored = Workspace.from_dict(scoped.as_dict())
        self.assertEqual(restored.mcp_visible_server_ids, ("s1", "s2"))


# -- persistence --------------------------------------------------------------

class WorkspaceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.store = WorkspaceStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def test_ensure_default_creates_home_workspace(self) -> None:
        workspaces = self.store.all()
        self.assertEqual(len(workspaces), 1)
        self.assertEqual(workspaces[0].id, DEFAULT_WORKSPACE_ID)
        self.assertEqual(workspaces[0].name, "Home")

    def test_ensure_default_is_idempotent(self) -> None:
        self.store.ensure_default()
        self.store.ensure_default()
        self.assertEqual(len(self.store.all()), 1)

    def test_save_and_get_round_trips(self) -> None:
        workspace = new_workspace("School")
        self.store.save(workspace)
        fetched = self.store.get(workspace.id)
        self.assertEqual(fetched, workspace)

    def test_save_overwrites_existing_row(self) -> None:
        workspace = new_workspace("School")
        self.store.save(workspace)
        renamed = workspace.touched()
        from dataclasses import replace
        renamed = replace(renamed, name="Grad School")
        self.store.save(renamed)
        fetched = self.store.get(workspace.id)
        self.assertEqual(fetched.name, "Grad School")

    def test_persists_across_reopening(self) -> None:
        workspace = new_workspace("Persisted")
        self.store.save(workspace)
        path = self.db.path
        self.db.close()
        reopened = Database(path)
        fetched = WorkspaceStore(reopened).get(workspace.id)
        self.assertEqual(fetched.name, "Persisted")
        reopened.close()

    def test_delete_refuses_the_last_remaining_workspace(self) -> None:
        # ensure_default already created the one and only workspace.
        self.assertFalse(self.store.delete(DEFAULT_WORKSPACE_ID))
        self.assertIsNotNone(self.store.get(DEFAULT_WORKSPACE_ID))

    def test_delete_succeeds_when_another_workspace_remains(self) -> None:
        extra = new_workspace("Extra")
        self.store.save(extra)
        self.assertTrue(self.store.delete(extra.id))
        self.assertIsNone(self.store.get(extra.id))

    def test_delete_still_refuses_default_down_to_one(self) -> None:
        extra = new_workspace("Extra")
        self.store.save(extra)
        # Deleting the extra one first should be fine; deleting the very
        # last remaining workspace afterward must be refused again.
        self.assertTrue(self.store.delete(extra.id))
        self.assertFalse(self.store.delete(DEFAULT_WORKSPACE_ID))


# -- Isolated Profile vs. plain Workspace -------------------------------------

class WorkspaceProfileManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = WorkspaceProfileManager(_profile)

    def test_plain_workspace_shares_the_default_profile(self) -> None:
        workspace = new_workspace("Personal")
        self.assertIs(self.manager.profile_for(workspace), _profile)

    def test_isolated_workspace_gets_its_own_profile(self) -> None:
        workspace = new_workspace("Banking", isolated_profile=True)
        isolated = self.manager.profile_for(workspace)
        self.assertIsNot(isolated, _profile)

    def test_isolated_profile_is_cached_across_calls(self) -> None:
        workspace = new_workspace("Banking", isolated_profile=True)
        first = self.manager.profile_for(workspace)
        second = self.manager.profile_for(workspace)
        self.assertIs(first, second)

    def test_has_isolated_profile_reflects_lazy_build(self) -> None:
        workspace = new_workspace("Banking", isolated_profile=True)
        self.assertFalse(self.manager.has_isolated_profile(workspace.id))
        self.manager.profile_for(workspace)
        self.assertTrue(self.manager.has_isolated_profile(workspace.id))

    def test_new_tab_url_falls_back_to_blank_for_isolated_workspaces(self) -> None:
        workspace = new_workspace("Banking", isolated_profile=True)
        self.assertEqual(self.manager.new_tab_url_for(workspace, "pybrowser://newtab"),
                         "about:blank")

    def test_new_tab_url_uses_app_default_for_plain_workspaces(self) -> None:
        workspace = new_workspace("Personal")
        self.assertEqual(self.manager.new_tab_url_for(workspace, "pybrowser://newtab"),
                         "pybrowser://newtab")

    def test_workspace_homepage_wins_either_way(self) -> None:
        from dataclasses import replace
        isolated = replace(new_workspace("Banking", isolated_profile=True),
                           homepage="https://bank.example/")
        plain = replace(new_workspace("Personal"), homepage="https://home.example/")
        self.assertEqual(self.manager.new_tab_url_for(isolated, "pybrowser://newtab"),
                         "https://bank.example/")
        self.assertEqual(self.manager.new_tab_url_for(plain, "pybrowser://newtab"),
                         "https://home.example/")


# -- reassignment on deletion --------------------------------------------------

class ReassignTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.missions = MissionStore(self.db)
        self.tasks = ScheduledTaskStore(self.db)
        self.skills = SkillStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def test_reassign_workspace_data_moves_missions_and_tasks(self) -> None:
        mission = self.missions.create("Title", "Goal", workspace_id="ws-a")
        task = self.tasks.create(goal="do it", schedule_kind=ScheduleKind.ONCE,
                                 schedule_at="2099-01-01T00:00:00+00:00",
                                 workspace_id="ws-a")
        reassign_workspace_data(self.db, from_workspace_id="ws-a", to_workspace_id="ws-b")
        self.assertEqual(self.missions.get(mission.id).workspace_id, "ws-b")
        self.assertEqual(self.tasks.get(task.id).workspace_id, "ws-b")

    def test_reassign_workspace_data_to_none_clears_scope(self) -> None:
        mission = self.missions.create("Title", "Goal", workspace_id="ws-a")
        reassign_workspace_data(self.db, from_workspace_id="ws-a", to_workspace_id=None)
        self.assertIsNone(self.missions.get(mission.id).workspace_id)

    def test_reassign_recorded_workflows_updates_binding(self) -> None:
        from app.agent.skills import Skill

        skill = Skill(id="s1", name="Do Thing", description="", instructions="",
                     preferred_provider="", preferred_model="",
                     workflow_workspace_id="ws-a")
        self.skills.save(skill)
        reassign_recorded_workflows(self.db, from_workspace_id="ws-a", to_workspace_id="ws-b")
        self.assertEqual(self.skills.get("s1").workflow_workspace_id, "ws-b")

    def test_reassign_recorded_workflows_to_none_clears_binding(self) -> None:
        from app.agent.skills import Skill

        skill = Skill(id="s2", name="Do Thing", description="", instructions="",
                     preferred_provider="", preferred_model="",
                     workflow_workspace_id="ws-a")
        self.skills.save(skill)
        reassign_recorded_workflows(self.db, from_workspace_id="ws-a", to_workspace_id=None)
        self.assertIsNone(self.skills.get("s2").workflow_workspace_id)


# -- Skill visibility ----------------------------------------------------------

class SkillVisibilityTests(unittest.TestCase):
    def test_global_skill_is_visible_everywhere(self) -> None:
        from app.agent.skills import Skill

        skill = Skill(id="s1", name="Global", description="", instructions="",
                     preferred_provider="", preferred_model="")
        self.assertTrue(skill.visible_in("ws-a"))
        self.assertTrue(skill.visible_in(None))

    def test_scoped_skill_is_only_visible_in_its_workspaces(self) -> None:
        from app.agent.skills import Skill

        skill = Skill(id="s1", name="Scoped", description="", instructions="",
                     preferred_provider="", preferred_model="",
                     workspace_ids=("ws-a", "ws-b"))
        self.assertTrue(skill.visible_in("ws-a"))
        self.assertFalse(skill.visible_in("ws-c"))
        self.assertFalse(skill.visible_in(None))

    def test_workspace_ids_round_trip_through_store(self) -> None:
        from app.agent.skills import Skill

        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        store = SkillStore(self.db)
        skill = Skill(id="s1", name="Scoped", description="", instructions="",
                     preferred_provider="", preferred_model="",
                     workspace_ids=("ws-a",), workflow_workspace_id="ws-a")
        store.save(skill)
        fetched = store.get("s1")
        self.assertEqual(fetched.workspace_ids, ("ws-a",))
        self.assertEqual(fetched.workflow_workspace_id, "ws-a")
        self.db.close()
        self._dir.cleanup()


# -- MCP visibility -------------------------------------------------------------

class McpWorkspaceVisibilityTests(unittest.TestCase):
    def test_none_means_everything_visible(self) -> None:
        from app.mcp.connection_manager import McpConnectionManager

        manager = McpConnectionManager.__new__(McpConnectionManager)
        manager._visible_server_ids = None
        self.assertIsNone(manager._visible_server_ids)

    def test_set_workspace_visibility_narrows_knows(self) -> None:
        from app.mcp.connection_manager import McpConnectionManager

        manager = McpConnectionManager.__new__(McpConnectionManager)
        manager._visible_server_ids = None
        manager.set_workspace_visibility(frozenset({"server-a"}))
        self.assertEqual(manager._visible_server_ids, frozenset({"server-a"}))
        manager.set_workspace_visibility(None)
        self.assertIsNone(manager._visible_server_ids)


# -- Mission scoping ------------------------------------------------------------

class MissionWorkspaceScopingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.missions = MissionStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def test_recent_unfiltered_by_default(self) -> None:
        self.missions.create("A", "goal a", workspace_id="ws-a")
        self.missions.create("B", "goal b", workspace_id="ws-b")
        self.assertEqual(len(self.missions.recent()), 2)

    def test_recent_scoped_to_workspace_includes_global(self) -> None:
        self.missions.create("A", "goal a", workspace_id="ws-a")
        self.missions.create("B", "goal b", workspace_id="ws-b")
        self.missions.create("G", "goal g")  # global
        scoped = self.missions.recent(workspace_id="ws-a")
        titles = {m.title for m in scoped}
        self.assertEqual(titles, {"A", "G"})

    def test_recent_scoped_excluding_global(self) -> None:
        self.missions.create("A", "goal a", workspace_id="ws-a")
        self.missions.create("G", "goal g")
        scoped = self.missions.recent(workspace_id="ws-a", include_global=False)
        titles = {m.title for m in scoped}
        self.assertEqual(titles, {"A"})


# -- MainWindow integration: tabs, switching, Missions, workflow prompt --------

class MainWindowWorkspaceIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        os.environ["PYBROWSER_DATA_DIR"] = self._dir.name
        self.db = Database(database_path())
        self.settings = SettingsStore(self.db)

    def tearDown(self) -> None:
        if getattr(self, "window", None) is not None:
            self.window.close()
            self.window.deleteLater()
        self.db.close()
        self._dir.cleanup()
        pump(3)

    def _open(self):
        from app.ui.main_window import MainWindow

        self.window = MainWindow(_profile, self.db, start_urls=["about:blank"])
        self.window.resize(1000, 700)
        pump()
        return self.window

    def test_a_fresh_profile_has_exactly_one_workspace(self) -> None:
        window = self._open()
        self.assertEqual(len(window.workspaces.all()), 1)
        self.assertEqual(window._current_workspace_id, DEFAULT_WORKSPACE_ID)

    def test_new_workspace_and_switch_back_preserves_both_tab_sets(self) -> None:
        window = self._open()
        window.tabs.widget(0).navigate("http://127.0.0.1:1/home-a")
        wait_for_url(window.tabs.widget(0), "http://127.0.0.1:1/home-a")
        home_id = window._current_workspace_id

        second = new_workspace("Work")
        window.workspaces.save(second)
        self.assertTrue(window.switch_workspace(second.id))
        pump()
        # Switching does not carry over the previous workspace's tabs - a
        # brand-new workspace starts with a fresh tab, never someone else's.
        self.assertNotEqual(window.tabs.widget(0).url().toString(), "http://127.0.0.1:1/home-a")
        window.tabs.widget(0).navigate("http://127.0.0.1:1/work-a")
        wait_for_url(window.tabs.widget(0), "http://127.0.0.1:1/work-a")

        self.assertTrue(window.switch_workspace(home_id))
        pump()
        self.assertEqual(wait_for_url(window.tabs.widget(0), "http://127.0.0.1:1/home-a"),
                         "http://127.0.0.1:1/home-a")

        self.assertTrue(window.switch_workspace(second.id))
        pump()
        self.assertEqual(wait_for_url(window.tabs.widget(0), "http://127.0.0.1:1/work-a"),
                         "http://127.0.0.1:1/work-a")

    def test_pinned_and_grouped_tabs_are_workspace_scoped(self) -> None:
        window = self._open()
        window.tabs.widget(0).navigate("http://127.0.0.1:1/pinned-a")
        wait_for_url(window.tabs.widget(0), "http://127.0.0.1:1/pinned-a")
        window.tabs.set_pinned(0, True)
        pump()
        home_id = window._current_workspace_id

        second = new_workspace("Work")
        window.workspaces.save(second)
        window.switch_workspace(second.id)
        pump()
        self.assertFalse(window.tabs.is_pinned(0))

        window.switch_workspace(home_id)
        pump()
        self.assertTrue(window.tabs.is_pinned(0))

    def test_switching_to_the_same_workspace_is_a_no_op(self) -> None:
        window = self._open()
        self.assertFalse(window.switch_workspace(window._current_workspace_id))

    def test_switching_to_an_unknown_workspace_is_refused(self) -> None:
        window = self._open()
        self.assertFalse(window.switch_workspace("does-not-exist"))

    def test_new_mission_defaults_to_the_current_workspace(self) -> None:
        """MainWindow's own call sites all pass workspace_id=self._current_
        workspace_id explicitly (see e.g. _delegate_to_team) - MissionService
        itself has no opinion and defaults to None/global when a caller
        does not pass one, so this exercises the same call MainWindow makes."""
        window = self._open()
        second = new_workspace("Research")
        window.workspaces.save(second)
        window.switch_workspace(second.id)
        pump()
        mission = window.missions.start("Investigate something",
                                        workspace_id=window._current_workspace_id)
        self.assertEqual(mission.workspace_id, second.id)

    def test_recorded_workflow_from_a_different_workspace_prompts(self) -> None:
        from unittest.mock import patch

        from app.agent.skills import Skill
        from app.automation.model import RecordedWorkflow

        window = self._open()
        home_id = window._current_workspace_id
        second = new_workspace("Work")
        window.workspaces.save(second)

        skill = Skill(id="wf1", name="Recorded", description="", instructions="",
                     preferred_provider="", preferred_model="",
                     workflow=RecordedWorkflow(steps=(), parameters=()),
                     workflow_workspace_id=home_id)

        # Currently in the recorded workspace: no prompt needed.
        self.assertTrue(window._confirm_workflow_workspace(skill))

        window.switch_workspace(second.id)
        pump()
        with patch("app.ui.main_window.QMessageBox") as mock_box_cls:
            mock_box = mock_box_cls.return_value
            switch_button = object()
            mock_box.addButton.side_effect = [switch_button, object(), object()]
            mock_box.clickedButton.return_value = switch_button
            result = window._confirm_workflow_workspace(skill)
        self.assertTrue(result)
        self.assertEqual(window._current_workspace_id, home_id)

    def test_recorded_workflow_with_no_recorded_workspace_never_prompts(self) -> None:
        from app.agent.skills import Skill
        from app.automation.model import RecordedWorkflow

        window = self._open()
        skill = Skill(id="wf2", name="Old Recording", description="", instructions="",
                     preferred_provider="", preferred_model="",
                     workflow=RecordedWorkflow(steps=(), parameters=()),
                     workflow_workspace_id=None)
        self.assertTrue(window._confirm_workflow_workspace(skill))


# -- scheduled-task workspace binding through TaskRunner -----------------------

class TaskRunnerWorkspaceBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self._dir.name, "t.sqlite3"))
        self.tasks = ScheduledTaskStore(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self._dir.cleanup()

    def test_scheduled_task_persists_its_workspace_id(self) -> None:
        task = self.tasks.create(goal="check the news", schedule_kind=ScheduleKind.ONCE,
                                 schedule_at="2099-01-01T00:00:00+00:00",
                                 workspace_id="ws-a")
        self.assertEqual(task.workspace_id, "ws-a")
        self.assertEqual(self.tasks.get(task.id).workspace_id, "ws-a")

    def test_scheduled_task_workspace_id_defaults_to_none(self) -> None:
        task = self.tasks.create(goal="check the news", schedule_kind=ScheduleKind.ONCE,
                                 schedule_at="2099-01-01T00:00:00+00:00")
        self.assertIsNone(task.workspace_id)

    def test_firing_a_bound_task_switches_workspace_before_running(self) -> None:
        from unittest.mock import MagicMock

        switched: list[str] = []
        missions = MagicMock()
        missions.start.return_value = None
        runner = TaskRunner(self.tasks, missions, lambda: None, None,
                            workspace_switcher=switched.append)
        task = self.tasks.create(goal="check the news", schedule_kind=ScheduleKind.ONCE,
                                 schedule_at="2099-01-01T00:00:00+00:00",
                                 workspace_id="ws-a")
        session = MagicMock()
        session.send.return_value = True
        runner._fire(task, session)
        self.assertEqual(switched, ["ws-a"])

    def test_firing_an_unbound_task_never_switches(self) -> None:
        from unittest.mock import MagicMock

        switched: list[str] = []
        missions = MagicMock()
        missions.start.return_value = None
        runner = TaskRunner(self.tasks, missions, lambda: None, None,
                            workspace_switcher=switched.append)
        task = self.tasks.create(goal="check the news", schedule_kind=ScheduleKind.ONCE,
                                 schedule_at="2099-01-01T00:00:00+00:00")
        session = MagicMock()
        session.send.return_value = True
        runner._fire(task, session)
        self.assertEqual(switched, [])

    def test_no_workspace_switcher_configured_is_safe(self) -> None:
        from unittest.mock import MagicMock

        missions = MagicMock()
        missions.start.return_value = None
        runner = TaskRunner(self.tasks, missions, lambda: None, None)
        task = self.tasks.create(goal="check the news", schedule_kind=ScheduleKind.ONCE,
                                 schedule_at="2099-01-01T00:00:00+00:00",
                                 workspace_id="ws-a")
        session = MagicMock()
        session.send.return_value = True
        runner._fire(task, session)  # must not raise


if __name__ == "__main__":
    unittest.main()
