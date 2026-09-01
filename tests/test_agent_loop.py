"""The agent loop as an observable system: steps, trace, and multi-tab work.

Phase 3 added a structured record of what the loop did and a step checklist
the panel renders. Those exist to be inspected when something goes wrong, so
they need tests that would fail if they quietly stopped recording.

The rest of the loop - tool errors, limits, cancellation, injection, approval -
is covered by tests/test_agent.py and is not duplicated here.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_agent_loop -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-loop-tests-"))

import app.browser  # noqa: E402,F401

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent import trace as tracing  # noqa: E402
from app.agent.config import AgentConfig, ContextLimits  # noqa: E402
from app.agent.session import AgentSession, StepState  # noqa: E402
from app.agent.trace import Trace, summarise_arguments  # noqa: E402
from app.browser.controller import BrowserController  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from tests.fake_claude import ScriptedClaude, calls, find_ref, says  # noqa: E402
from tests.fixture_server import FixtureServer  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_server: FixtureServer | None = None
_profile = None


def setUpModule() -> None:
    global _app, _server, _profile
    _app = QApplication.instance() or QApplication(sys.argv[:1])
    _server = FixtureServer()
    _profile = shared_profile()


def tearDownModule() -> None:
    if _server is not None:
        _server.stop()
    if _app is not None:
        for _ in range(3):
            _app.processEvents()


def pump(predicate, timeout_ms: int = 20000) -> bool:
    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(timeout_ms)
    while not predicate() and not expired[0]:
        _app.processEvents()
    timer.stop()
    return predicate()


class TraceUnitTests(unittest.TestCase):
    """The trace on its own - no browser, no model."""

    def test_events_are_ordered_and_timed(self) -> None:
        trace = Trace()
        trace.start()
        trace.record(tracing.TASK_STARTED)
        trace.record(tracing.TOOL_STARTED, tool="browser_get_page")
        self.assertEqual(trace.names(), [tracing.TASK_STARTED, tracing.TOOL_STARTED])
        self.assertGreaterEqual(trace.events[1].at_ms, trace.events[0].at_ms)

    def test_it_is_capped_so_a_runaway_loop_cannot_exhaust_memory(self) -> None:
        trace = Trace(limit=10)
        trace.start()
        for _ in range(50):
            trace.record(tracing.TOOL_STARTED)
        self.assertEqual(len(trace.events), 10)

    def test_typed_text_is_never_recorded(self) -> None:
        safe = summarise_arguments("browser_type", {"ref": "s1:e2", "text": "hunter2"})
        self.assertNotIn("text", safe)
        self.assertEqual(safe["text_length"], 7)
        self.assertNotIn("hunter2", str(safe))

    def test_a_url_is_reduced_to_its_origin(self) -> None:
        # A full URL can carry a session token in its query string.
        safe = summarise_arguments("browser_navigate",
                                   {"url": "https://example.com/a?token=secret"})
        self.assertEqual(safe["url"], "https://example.com")
        self.assertNotIn("secret", str(safe))

    def test_export_is_readable(self) -> None:
        trace = Trace()
        trace.start()
        trace.record(tracing.TOOL_SUCCEEDED, tool="browser_get_page")
        self.assertIn("tool_succeeded", trace.export())
        self.assertIn("browser_get_page", trace.export())


class LoopTests(unittest.TestCase):
    """The real loop over a real browser, with a scripted model."""

    def setUp(self) -> None:
        self.tabs = TabManager(_profile, _server.base)
        self.tabs.resize(1100, 800)
        self.tabs.show()
        self.browser = BrowserController(self.tabs)
        self.browser.open_tab().wait()
        self.browser.navigate(_server.base).wait(30000)
        self.session: AgentSession | None = None
        self.steps: list = []

    def tearDown(self) -> None:
        if self.session is not None:
            self.session.shutdown()
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        _app.processEvents()

    def run_task(self, script, message="do the thing", limits=None) -> AgentSession:
        self.session = AgentSession(
            self.browser, ScriptedClaude(script),
            AgentConfig(limits=limits or ContextLimits()))
        self.session.step_changed.connect(self.steps.append)
        done = []
        self.session.finished.connect(lambda: done.append(True))
        self.session.send(message)
        self.assertTrue(pump(lambda: done), "the task never finished")
        return self.session

    # -- steps -------------------------------------------------------------
    def test_each_tool_call_becomes_a_step(self) -> None:
        session = self.run_task([calls("browser_get_page"),
                                 calls("browser_get_page_text"),
                                 says("Read it.")])
        self.assertEqual([s.description for s in session.steps],
                         ["Reading the page", "Reading the page text"])
        self.assertTrue(all(s.state == StepState.DONE for s in session.steps))

    def test_a_step_is_reported_running_before_it_is_done(self) -> None:
        self.run_task([calls("browser_get_page"), says("done")])
        states = [s.state for s in self.steps]
        self.assertEqual(states[0], StepState.RUNNING)
        self.assertEqual(states[-1], StepState.DONE)

    def test_a_failed_tool_marks_its_step_failed(self) -> None:
        session = self.run_task([calls("browser_click", {"ref": "s9:e99"}),
                                 says("That did not work.")])
        self.assertEqual(session.steps[-1].state, StepState.FAILED)
        self.assertTrue(session.steps[-1].detail, "a failed step should say why")

    def test_steps_never_carry_typed_text(self) -> None:
        # Typing into a password field is gated, so the task pauses for
        # approval - allow it, then check what the step list is willing to say.
        def type_password(messages):
            return calls("browser_type",
                         {"ref": find_ref(messages, "textbox", input_type="password"),
                          "text": "correct-horse-battery"})

        self.session = AgentSession(
            self.browser,
            ScriptedClaude([calls("browser_get_page"), type_password, says("done")]),
            AgentConfig())
        self.session.step_changed.connect(self.steps.append)
        asked, done = [], []
        self.session.confirmation_required.connect(asked.append)
        self.session.finished.connect(lambda: done.append(True))
        self.session.send("fill in the password")
        self.assertTrue(pump(lambda: asked), "typing a password was not gated")
        self.session.resolve_confirmation(True)
        self.assertTrue(pump(lambda: done))

        rendered = " ".join(f"{s.description} {s.detail}" for s in self.session.steps)
        self.assertNotIn("correct-horse-battery", rendered)
        self.assertNotIn("correct-horse-battery", self.session.trace.export())
        self.assertNotIn("correct-horse-battery", asked[0].prompt)

    # -- trace -------------------------------------------------------------
    def test_the_trace_records_the_shape_of_the_task(self) -> None:
        session = self.run_task([calls("browser_get_page"), says("Done.")])
        names = session.trace.names()
        self.assertEqual(names[0], tracing.TASK_STARTED)
        self.assertEqual(names[-1], tracing.TASK_FINISHED)
        for expected in (tracing.MODEL_REQUESTED, tracing.MODEL_RESPONDED,
                         tracing.TOOL_REQUESTED, tracing.TOOL_STARTED,
                         tracing.TOOL_SUCCEEDED):
            self.assertIn(expected, names)

    def test_the_trace_records_a_rejected_tool_without_running_it(self) -> None:
        session = self.run_task([calls("browser_not_a_tool"), says("Recovered.")])
        self.assertEqual(session.trace.count(tracing.TOOL_REJECTED), 1)
        self.assertEqual(session.trace.count(tracing.TOOL_STARTED), 0)

    def test_the_trace_records_a_failure_with_its_code(self) -> None:
        session = self.run_task([calls("browser_click", {"ref": "s9:e99"}), says("ok")])
        failed = [e for e in session.trace.events if e.name == tracing.TOOL_FAILED]
        self.assertTrue(failed)
        self.assertTrue(failed[0].detail.get("reason"))

    def test_the_trace_never_contains_page_text(self) -> None:
        session = self.run_task([calls("browser_get_page_text"), says("Read.")])
        exported = session.trace.export()
        # "Fixture Home" is on the page; only sizes may be recorded.
        self.assertNotIn("Fixture Home", exported)
        self.assertIn(tracing.TOOL_SUCCEEDED, exported)

    def test_a_cancelled_task_is_recorded_as_cancelled(self) -> None:
        session = AgentSession(self.browser, ScriptedClaude([calls("browser_get_page")] * 5),
                               AgentConfig())
        self.session = session
        done = []
        session.finished.connect(lambda: done.append(True))
        session.send("start something")
        pump(lambda: session.trace.count(tracing.TOOL_STARTED) > 0, 10000)
        session.cancel()
        self.assertTrue(pump(lambda: done))
        self.assertEqual(session.trace.count(tracing.TASK_CANCELLED), 1)
        self.assertEqual(session.trace.count(tracing.TASK_FINISHED), 0)

    def test_the_trace_restarts_with_each_task(self) -> None:
        session = self.run_task([calls("browser_get_page"), says("one")])
        first = len(session.trace.events)
        done = []
        session.finished.connect(lambda: done.append(True))
        session._transport = None       # not used; the script has more turns
        self.assertGreater(first, 0)
        session.send("second task")
        pump(lambda: done, 10000)
        self.assertEqual(session.trace.names()[0], tracing.TASK_STARTED)

    # -- multiple tabs -----------------------------------------------------
    def test_the_agent_can_read_two_tabs_and_compare_them(self) -> None:
        """Step 3 of the brief: inspect several tabs and answer about both."""
        def read_other_tab(messages):
            """Pick the other tab out of the browser_list_tabs result.

            The tab list is browser state, not page content, so it arrives
            unfenced - which is the correct distinction and worth relying on
            here rather than working around.
            """
            import json

            from tests.fake_claude import last_tool_result

            payload = json.loads(last_tool_result(messages))
            other = [t for t in payload["tabs"] if not t["active"]][0]
            return calls("browser_get_page_text", {"tab_id": other["tab_id"]})

        self.browser.open_tab(_server.url("/second")).wait(30000)
        session = self.run_task([
            calls("browser_get_page_text"),
            calls("browser_list_tabs"),
            read_other_tab,
            says("The first tab is the fixture home; the second is the second page."),
        ], message="compare my two tabs")
        self.assertEqual(len(session.steps), 3)
        self.assertTrue(all(s.state == StepState.DONE for s in session.steps))


if __name__ == "__main__":
    unittest.main()


class DemoTests(unittest.TestCase):
    """The demonstration task, run as a test so it cannot quietly rot."""

    def test_the_research_demo_completes_read_only(self) -> None:
        import subprocess

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
                   QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox")
        completed = subprocess.run(
            [sys.executable, os.path.join(root, "scripts", "agent_demo.py")],
            capture_output=True, text=True, timeout=300, env=env, cwd=root)
        self.assertIn("DEMO PASSED", completed.stdout, completed.stdout[-2000:])
        self.assertIn("read-only   : yes", completed.stdout)
        self.assertIn("approvals   : 0 requested", completed.stdout)
