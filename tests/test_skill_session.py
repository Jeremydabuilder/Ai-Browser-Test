"""AgentSession.set_tool_allowlist end to end: a Skill's tool scope
actually changes what the model can do in a real (scripted) task, and
approvals still apply to whatever remains allowed. See
app/agent/session.py and app/agent/skills.py.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_skill_session -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-skill-session-tests-"))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.config import AgentConfig, ContextLimits  # noqa: E402
from app.agent.session import AgentSession  # noqa: E402
from app.browser.controller import BrowserController  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from tests.fake_claude import ScriptedClaude, calls, says  # noqa: E402
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


def pump(predicate, timeout_ms: int = 15000) -> bool:
    expired = [False]
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: expired.__setitem__(0, True))
    timer.start(timeout_ms)
    while not predicate() and not expired[0]:
        _app.processEvents()
    timer.stop()
    return predicate()


class SetToolAllowlistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _server
        self.tabs = TabManager(_profile, self.server.base)
        self.tabs.resize(900, 700)
        self.browser = BrowserController(self.tabs)
        self.browser.open_tab().wait()
        self.browser.navigate(self.server.base).wait()
        self.session: AgentSession | None = None

    def tearDown(self) -> None:
        if self.session is not None:
            self.session.shutdown()
        for tab in self.tabs.tabs():
            tab.page.deleteLater()
        self.tabs.deleteLater()
        _app.processEvents()

    def start(self, script: list) -> ScriptedClaude:
        fake = ScriptedClaude(script)
        self.session = AgentSession(self.browser, fake, AgentConfig(limits=ContextLimits()))
        self.fake = fake
        return fake

    def run_task(self, message: str, timeout_ms: int = 20000) -> bool:
        done = []
        self.session.finished.connect(lambda: done.append(True))
        self.assertTrue(self.session.send(message))
        return pump(lambda: bool(done), timeout_ms)

    def test_a_disallowed_tool_call_is_refused_as_unknown(self) -> None:
        self.start([calls("browser_click", {"ref": "s1:e0"}), says("could not click it")])
        self.session.set_tool_allowlist(frozenset({"browser_get_page_text"}))
        self.assertTrue(self.run_task("Click something."))
        result = self.fake.tool_results()[0]
        self.assertIn("UNKNOWN_TOOL", result)

    def test_an_allowed_tool_call_still_runs(self) -> None:
        self.start([calls("browser_get_page_text"), says("read it")])
        self.session.set_tool_allowlist(frozenset({"browser_get_page_text"}))
        self.assertTrue(self.run_task("Read the page."))
        self.assertIn("Fixture Home", self.fake.tool_results()[0])

    def test_the_model_is_never_even_offered_a_disallowed_tool(self) -> None:
        self.start([says("ok")])
        self.session.set_tool_allowlist(frozenset({"browser_get_page_text"}))
        self.run_task("hi")
        names = {tool["name"] for tool in self.fake.requests[0]["tools"]}
        self.assertEqual(names, {"browser_get_page_text"})

    def test_clearing_the_allowlist_restores_every_tool(self) -> None:
        self.start([says("ok")])
        self.session.set_tool_allowlist(frozenset({"browser_get_page_text"}))
        self.session.set_tool_allowlist(None)
        self.run_task("hi")
        names = {tool["name"] for tool in self.fake.requests[0]["tools"]}
        self.assertIn("browser_click", names)
        self.assertIn("browser_navigate", names)

    def test_setting_the_allowlist_is_refused_while_busy(self) -> None:
        import threading

        gate = threading.Event()
        fake = self.start([calls("browser_get_page_text"), says("done")])
        fake.delay_event = gate
        self.session.send("go")
        self.assertTrue(pump(lambda: self.session.busy, 5000))
        self.session.set_tool_allowlist(frozenset({"browser_navigate"}))
        # Still unrestricted - the call above was a no-op while busy.
        self.assertIsNone(self.session._tools._allowed_tools)
        gate.set()
        self.assertTrue(pump(lambda: not self.session.busy, 10000))

    def test_approval_is_still_required_for_a_sensitive_allowed_tool(self) -> None:
        """The allowlist narrows which tools exist; it never widens what a
        still-allowed tool is permitted to do without confirmation."""
        confirmations = []
        self.start([calls("browser_navigate", {"url": "https://example.com/setup.exe"}),
                    says("waiting")])
        self.session.set_tool_allowlist(frozenset({"browser_navigate"}))
        self.session.confirmation_required.connect(confirmations.append)
        self.session.send("download the installer")
        self.assertTrue(pump(lambda: bool(confirmations), 10000))


if __name__ == "__main__":
    unittest.main()
