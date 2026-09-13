"""Phase 14 hardening: sensitive visual actions go through the SAME
assess -> ConfirmationRequest -> confirmation_required -> resolve_confirmation
pipeline a structured browser_click already uses - never a model-supplied
``confirmed: true`` replay flag.

Classifying a coordinate needs a page round trip (there is no cached
element the way a structured ref already has one), so AgentSession's
assessment step is async for these two tools only
(ToolRegistry.needs_async_assessment/assess_async) - everything after
that point (refusal, the confirmation prompt, execution) is the exact
same code path every other tool already goes through.

Run with:
    QT_QPA_PLATFORM=offscreen python -m unittest tests.test_visual_confirmation_flow -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("PYBROWSER_DATA_DIR", tempfile.mkdtemp(prefix="pybrowser-visual-confirm-tests-"))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.agent.config import AgentConfig, ContextLimits  # noqa: E402
from app.agent.session import AgentSession, AgentState  # noqa: E402
from app.agent.tools import TOOL_SCHEMAS  # noqa: E402
from app.browser.controller import BrowserController  # noqa: E402
from app.browser.tab_manager import TabManager  # noqa: E402
from tests.fake_claude import ScriptedClaude, calls, says  # noqa: E402
from tests.fixture_server import FixtureServer  # noqa: E402
from tests.qt_profile import shared_profile  # noqa: E402

_app: QApplication | None = None
_server: FixtureServer | None = None
_profile = None

NORMAL_BUTTON = {"x": 110, "y": 60}
BUY_BUTTON = {"x": 110, "y": 160}
TEXT_FIELD = {"x": 115, "y": 615}
PASSWORD_FIELD = {"x": 115, "y": 665}


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


class VisualConfirmationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _server
        self.tabs = TabManager(_profile, self.server.base)
        self.tabs.resize(1200, 900)
        self.tabs.show()
        self.browser = BrowserController(self.tabs)
        self.browser.open_tab().wait()
        result = self.browser.navigate(self.server.url("/visual")).wait()
        self.assertTrue(result.ok, result.error)

        self.confirmations: list = []
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
        # provider defaults to Anthropic (vision-capable) - see AgentConfig.
        config = AgentConfig(limits=ContextLimits())
        self.session = AgentSession(self.browser, fake, config)
        self.session.confirmation_required.connect(self.confirmations.append)
        self.fake = fake
        return fake

    def relabel_buy_button(self) -> None:
        """Simulates the page changing what is at the buy-button's
        coordinates - the fixture for target-drift tests (see
        tests/fixture_server.py's VISUAL page)."""
        structure = self.browser.get_page_structure().wait().data["structure"]
        ref = next(e.ref for e in structure.buttons if e.name.startswith("Relabel"))
        result = self.browser.click(ref).wait()
        self.assertTrue(result.ok, result.error)


class HarmlessVisualClickTests(VisualConfirmationTestCase):
    def test_a_harmless_visual_click_never_asks_for_confirmation(self) -> None:
        self.start([calls("browser_visual_click", NORMAL_BUTTON), says("Done.")])
        done = []
        self.session.finished.connect(lambda: done.append(True))
        self.session.send("Click the hello button.")
        self.assertTrue(pump(lambda: bool(done)))
        self.assertEqual(self.confirmations, [])


class SensitiveVisualClickConfirmationTests(VisualConfirmationTestCase):
    def test_a_sensitive_visual_click_asks_the_same_kind_of_confirmation_a_structured_click_would(
        self,
    ) -> None:
        self.start([calls("browser_visual_click", BUY_BUTTON), says("Bought.")])
        self.session.send("Buy the thing.")
        self.assertTrue(pump(lambda: bool(self.confirmations)))

        request = self.confirmations[0]
        self.assertEqual(self.session.state, AgentState.AWAITING_CONFIRMATION)
        self.assertIn("buy now", request.description.lower())
        self.assertIn("spend money", " ".join(request.reasons))
        self.assertIn("Py wants to", request.prompt)
        self.session.resolve_confirmation(True)
        self.assertTrue(pump(lambda: self.session.state == AgentState.IDLE))
        result = json.loads(self.fake.tool_results()[-1].split("\n")[0])
        self.assertTrue(result["ok"])

    def test_declining_a_sensitive_visual_click_never_performs_it(self) -> None:
        self.start([calls("browser_visual_click", BUY_BUTTON), says("Understood.")])
        self.session.send("Buy the thing.")
        self.assertTrue(pump(lambda: bool(self.confirmations)))
        self.session.resolve_confirmation(False)
        self.assertTrue(pump(lambda: self.session.state == AgentState.IDLE))
        result = json.loads(self.fake.tool_results()[-1])
        self.assertEqual(result["error"]["code"], "USER_DECLINED")

    def test_typing_into_a_password_field_asks_for_confirmation_too(self) -> None:
        self.start([
            calls("browser_visual_focus", PASSWORD_FIELD),
            calls("browser_visual_type", {"text": "hunter2"}),
            says("Done."),
        ])
        self.session.send("Fill in the password.")
        self.assertTrue(pump(lambda: bool(self.confirmations)))
        self.assertIn("password", " ".join(self.confirmations[0].reasons).lower())
        self.session.resolve_confirmation(False)
        self.assertTrue(pump(lambda: self.session.state == AgentState.IDLE))

    def test_typing_into_an_ordinary_field_never_asks(self) -> None:
        self.start([
            calls("browser_visual_focus", TEXT_FIELD),
            calls("browser_visual_type", {"text": "hello"}),
            says("Done."),
        ])
        self.session.send("Fill in the notes field.")
        self.assertTrue(pump(lambda: self.session.state == AgentState.IDLE))
        self.assertEqual(self.confirmations, [])


class ApprovalBindingTests(VisualConfirmationTestCase):
    """Approval binds to the specific target assessed, not to "whatever is
    at that coordinate whenever this finally executes" - and it is a
    server-side, one-shot answer to one specific ToolCall, never a value
    the model can set or replay itself."""

    def test_approval_is_invalidated_if_the_page_changes_the_target_first(self) -> None:
        self.start([calls("browser_visual_click", BUY_BUTTON), says("Bought.")])
        self.session.send("Buy the thing.")
        self.assertTrue(pump(lambda: bool(self.confirmations)))

        # The page changes what is at that exact point while the approval
        # is still outstanding - a real render update, not a test artefact.
        self.relabel_buy_button()

        self.session.resolve_confirmation(True)
        self.assertTrue(pump(lambda: self.session.state == AgentState.IDLE))
        result = json.loads(self.fake.tool_results()[-1])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "TARGET_CHANGED")

    def test_approval_still_works_normally_when_the_target_has_not_changed(self) -> None:
        """The drift check does not false-positive on an unchanged page -
        only an actual difference invalidates the approval."""
        self.start([calls("browser_visual_click", BUY_BUTTON), says("Bought.")])
        self.session.send("Buy the thing.")
        self.assertTrue(pump(lambda: bool(self.confirmations)))
        self.session.resolve_confirmation(True)
        self.assertTrue(pump(lambda: self.session.state == AgentState.IDLE))
        result = json.loads(self.fake.tool_results()[-1].split("\n")[0])
        self.assertTrue(result["ok"])

    def test_no_confirmed_flag_exists_on_the_visual_click_schema_any_more(self) -> None:
        """The old model-supplied 'confirmed: true' replay mechanism is
        gone - approval can only come from the real confirmation flow,
        never from an argument the model sets itself."""
        schema = next(s for s in TOOL_SCHEMAS if s["name"] == "browser_visual_click")
        self.assertNotIn("confirmed", schema["input_schema"]["properties"])
        schema = next(s for s in TOOL_SCHEMAS if s["name"] == "browser_visual_type")
        self.assertNotIn("confirmed", schema["input_schema"]["properties"])

    def test_resolving_with_no_confirmation_outstanding_is_a_harmless_no_op(self) -> None:
        """A stray resolve_confirmation call (e.g. a duplicate UI event)
        cannot be replayed against a later, unrelated call - there is
        nothing outstanding for it to apply to."""
        self.start([calls("browser_visual_click", NORMAL_BUTTON), says("Done.")])
        done = []
        self.session.finished.connect(lambda: done.append(True))
        self.session.send("Click the hello button.")
        self.assertTrue(pump(lambda: bool(done)))
        # No exception, no effect - nothing was awaiting an answer.
        self.session.resolve_confirmation(True)


if __name__ == "__main__":
    unittest.main()
