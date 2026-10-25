"""Replay stops safely: timeouts, late answers, declined approvals, stray signals."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-replay-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.automation import runner as runner_mod  # noqa: E402
from app.automation.model import RecordedStep, SemanticTarget  # noqa: E402
from app.automation.runner import PauseReason, WorkflowRunner  # noqa: E402
from tests.test_automation import (  # noqa: E402
    _FakeBrowser, _FakeSession, _FakeTools, _wait_until, new_workflow)

_app = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication(sys.argv[:1])


class _PendingFuture:
    """A page that never answers (or answers when the test says so)."""

    def __init__(self) -> None:
        self.callback = None

    def then(self, callback):
        self.callback = callback
        return self


class _HangingBrowser(_FakeBrowser):
    def __init__(self) -> None:
        super().__init__()
        self.pending: list[_PendingFuture] = []

    def find_elements(self, queries, role=None, tab_id=None):
        future = _PendingFuture()
        self.pending.append(future)
        return future


class _Result:
    def __init__(self, ok, data):
        self.ok, self.data = ok, data


class ReplaySafety(unittest.TestCase):
    def setUp(self) -> None:
        self.session = _FakeSession()
        self.browser = _HangingBrowser()
        self.runner = WorkflowRunner(self.session, self.browser, _FakeTools())
        self.paused: list[str] = []
        self.runner.paused.connect(lambda reason, _msg: self.paused.append(reason))
        self.click = RecordedStep(id=0, tool_name="browser_click", args={},
                                  target=SemanticTarget(name="Continue"))

    def test_a_page_that_never_answers_times_out_instead_of_hanging(self) -> None:
        orig = runner_mod.RESOLVE_TIMEOUT_MS
        runner_mod.RESOLVE_TIMEOUT_MS = 50
        self.addCleanup(setattr, runner_mod, "RESOLVE_TIMEOUT_MS", orig)
        self.runner.run(new_workflow([self.click]))
        self.assertTrue(self.runner.is_running)
        _wait_until(lambda: self.paused)
        self.assertEqual(self.paused, [PauseReason.TIMED_OUT])
        self.assertFalse(self.runner.is_running)
        self.assertEqual(self.session.calls, [])  # nothing was ever done

    def test_a_late_answer_after_cancel_does_nothing(self) -> None:
        self.runner.run(new_workflow([self.click]))
        self.runner.cancel()
        self.browser.pending[0].callback(_Result(True, {"matches": [{"ref": "e1"}]}))
        _wait_until(lambda: False, 100)
        self.assertEqual(self.session.calls, [])

    def test_a_late_answer_for_an_old_run_cannot_drive_the_new_one(self) -> None:
        self.runner.run(new_workflow([self.click]))
        self.runner.cancel()
        self.runner.run(new_workflow([self.click]))
        self.browser.pending[0].callback(_Result(True, {"matches": [{"ref": "stale"}]}))
        _wait_until(lambda: False, 100)
        self.assertEqual(self.session.calls, [])  # only the second run's own answer counts
        self.browser.pending[1].callback(_Result(True, {"matches": [{"ref": "fresh"}]}))
        _wait_until(lambda: self.session.calls)
        self.assertEqual(self.session.calls[0][0][1]["ref"], "fresh")

    def test_declining_approval_stops_even_an_optional_step(self) -> None:
        class Declining(_FakeSession):
            def run_routine(self, steps):
                self.calls.append(steps)
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, lambda: self.routine_finished.emit([]))
                return True
        runner = WorkflowRunner(Declining(), _FakeBrowser(), _FakeTools())
        paused, done = [], []
        runner.paused.connect(lambda r, _m: paused.append(r))
        runner.completed.connect(lambda: done.append(1))
        runner.run(new_workflow([RecordedStep(id=0, tool_name="browser_navigate",
                                              args={"url": "https://a.example"}, optional=True)]))
        _wait_until(lambda: paused or done)
        self.assertEqual(paused, [PauseReason.DECLINED])
        self.assertEqual(done, [])

    def test_a_skipped_optional_step_is_announced(self) -> None:
        session = _FakeSession()
        session.next_ok = False
        runner = WorkflowRunner(session, _FakeBrowser(), _FakeTools())
        skipped, done = [], []
        runner.step_skipped.connect(skipped.append)
        runner.completed.connect(lambda: done.append(1))
        runner.run(new_workflow([RecordedStep(id=0, tool_name="browser_navigate",
                                              args={"url": "https://a.example"}, optional=True)]))
        _wait_until(lambda: done)
        self.assertEqual(len(skipped), 1)

    def test_a_stray_routine_result_during_resolution_is_ignored(self) -> None:
        self.runner.run(new_workflow([self.click]))
        self.session.routine_finished.emit([{"type": "tool_result", "content": "{}"}])
        self.assertTrue(self.runner.is_running)
        self.assertEqual(self.paused, [])


class _TabbedBrowser(_FakeBrowser):
    def __init__(self) -> None:
        super().__init__()
        self.tab_list = [{"tab_id": 1, "active": True, "url": "https://a.example"}]
        self.finds: list = []

    def list_tabs(self):
        return [dict(t) for t in self.tab_list]

    def find_elements(self, queries, role=None, tab_id=None):
        self.finds.append(tab_id)
        return super().find_elements(queries, role, tab_id)


class _ScriptedSession(_FakeSession):
    """Runs a callback when a routine is dispatched, e.g. to open tabs."""

    def __init__(self, on_run=None) -> None:
        super().__init__()
        self.on_run = on_run

    def run_routine(self, steps):
        if self.on_run is not None:
            self.on_run(steps)
        return super().run_routine(steps)


class TabFollowing(unittest.TestCase):
    def build(self, on_run=None):
        self.browser = _TabbedBrowser()
        self.session = _ScriptedSession(on_run)
        self.runner = WorkflowRunner(self.session, self.browser, _FakeTools())
        self.paused, self.done = [], []
        self.runner.paused.connect(lambda r, _m: self.paused.append(r))
        self.runner.completed.connect(lambda: self.done.append(1))

    def nav(self, i=0):
        return RecordedStep(id=i, tool_name="browser_navigate", args={"url": "https://a.example/x"})

    def click(self, i=0):
        return RecordedStep(id=i, tool_name="browser_click", args={}, target=SemanticTarget(name="Go"))

    def used_tab_ids(self):
        return [call[0][1].get("tab_id") for call in self.session.calls]

    def test_starts_in_the_active_tab_and_ignores_the_user_switching_tabs(self):
        self.build()
        self.browser.tab_list.append({"tab_id": 2, "active": False, "url": "https://b.example"})
        def switch(_steps):
            for t in self.browser.tab_list:
                t["active"] = t["tab_id"] == 2        # the person clicks another tab mid-run
        self.session.on_run = switch
        self.runner.run(new_workflow([self.nav(0), self.nav(1)]))
        _wait_until(lambda: self.done or self.paused)
        self.assertEqual(self.used_tab_ids(), [1, 1])

    def test_follows_exactly_one_tab_opened_by_a_click(self):
        self.build()
        self.browser.matches = [{"ref": "e1"}]
        def open_one(steps):
            if steps[0][0] == "browser_click":
                self.browser.tab_list.append({"tab_id": 7, "active": False, "url": "https://n.example"})
        self.session.on_run = open_one
        self.runner.run(new_workflow([self.click(0), self.nav(1)]))
        _wait_until(lambda: self.done or self.paused)
        self.assertEqual(self.used_tab_ids(), [1, 7])
        self.assertEqual(self.paused, [])

    def test_two_new_tabs_after_a_click_stop_the_replay(self):
        self.build()
        self.browser.matches = [{"ref": "e1"}]
        def open_two(steps):
            self.browser.tab_list += [{"tab_id": 7, "active": False, "url": "u"},
                                      {"tab_id": 8, "active": False, "url": "u"}]
        self.session.on_run = open_two
        self.runner.run(new_workflow([self.click(0), self.nav(1)]))
        _wait_until(lambda: self.paused or self.done)
        self.assertEqual(self.paused, [PauseReason.TAB_AMBIGUOUS])
        self.assertEqual(len(self.session.calls), 1)

    def test_a_tab_the_person_opens_after_a_navigation_is_not_followed(self):
        self.build()
        def stray(_steps):
            self.browser.tab_list.append({"tab_id": 9, "active": False, "url": "u"})
        self.session.on_run = stray
        self.runner.run(new_workflow([self.nav(0), self.nav(1)]))
        _wait_until(lambda: self.done or self.paused)
        self.assertEqual(self.used_tab_ids(), [1, 1])

    def test_closing_the_working_tab_stops_instead_of_acting_elsewhere(self):
        self.build()
        self.browser.tab_list.append({"tab_id": 2, "active": False, "url": "https://b.example"})
        def close(_steps):
            self.browser.tab_list = [t for t in self.browser.tab_list if t["tab_id"] != 1]
        self.session.on_run = close
        self.runner.run(new_workflow([self.nav(0), self.nav(1)]))
        _wait_until(lambda: self.paused or self.done)
        self.assertEqual(self.paused, [PauseReason.TAB_LOST])
        self.assertEqual(len(self.session.calls), 1)

    def test_element_lookup_uses_the_working_tab(self):
        self.build()
        self.browser.matches = [{"ref": "e1"}]
        self.runner.run(new_workflow([self.click(0)]))
        _wait_until(lambda: self.done or self.paused)
        self.assertEqual(self.browser.finds, [1])


if __name__ == "__main__":
    unittest.main()
